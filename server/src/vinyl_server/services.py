"""Domain operations shared by the JSON API and the server-rendered UI.

The API routers and the UI routes are two skins over the same operations; the
logic lives here exactly once. Functions take their collaborators (session,
discogs client, s3 client) explicitly — no globals — so both callers and tests
wire them the same way.
"""

from __future__ import annotations

import contextlib

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session, joinedload
from vinyl_shared.ids import format_accession_id, format_side_id
from vinyl_shared.states import AccessionStatus, AssetKind, ConditionGrade, Speed

from vinyl_server.db.models import Accession, Asset, DiscSide, Release
from vinyl_server.musicbrainz import lookup_release_mbid
from vinyl_server.s3 import object_key


def get_accession(session: Session, accession_id: str) -> Accession:
    acc = session.get(
        Accession, accession_id.upper(), options=[joinedload(Accession.release)]
    )
    if acc is None:
        raise HTTPException(404, f"no such accession: {accession_id}")
    return acc


def make_sides(accession_id: str, discs: range, speed: Speed) -> list[DiscSide]:
    return [
        DiscSide(
            side_id=format_side_id(accession_id, disc, side),
            accession_id=accession_id,
            disc=disc,
            side=side,
            speed_rpm=speed,
        )
        for disc in discs
        for side in ("A", "B")
    ]


def create_accession(
    session: Session,
    *,
    collection: str,
    disc_count: int,
    speed_rpm: Speed,
    has_jacket: bool,
    has_inner_sleeve: bool,
    condition_grade: ConditionGrade | None = None,
    condition_notes: str | None,
) -> Accession:
    # Row-locking allocation: two concurrent sessions can never mint one number.
    row = session.execute(
        text(
            "UPDATE collection SET next_number = next_number + 1 "
            "WHERE prefix = :p RETURNING next_number - 1"
        ),
        {"p": collection},
    ).first()
    if row is None:
        raise HTTPException(422, f"no such collection: {collection}")
    accession_id = format_accession_id(collection, row[0])

    acc = Accession(
        accession_id=accession_id,
        collection=collection,
        number=row[0],
        disc_count=disc_count,
        has_jacket=has_jacket,
        has_inner_sleeve=has_inner_sleeve,
        condition_grade=condition_grade,
        condition_notes=condition_notes,
        status=AccessionStatus.INVENTORIED,
    )
    session.add(acc)
    session.add_all(make_sides(accession_id, range(1, disc_count + 1), speed_rpm))
    session.flush()
    return get_accession(session, accession_id)


def bind_release(
    session: Session,
    discogs_client,
    acc: Accession,
    discogs_release_id: int,
    *,
    musicbrainz_enabled: bool,
) -> Accession:
    from vinyl_server.discogs import map_positions_to_sides, normalize_tracklist

    raw = discogs_client.get_release(discogs_release_id)
    tracklist = normalize_tracklist(raw)

    release = session.get(Release, discogs_release_id)
    if release is None:
        release = Release(
            discogs_release_id=discogs_release_id,
            artist=", ".join(a.get("name", "") for a in raw.get("artists", [])) or "Unknown",
            title=raw.get("title", "Unknown"),
            label=(raw.get("labels") or [{}])[0].get("name"),
            catalog_number=(raw.get("labels") or [{}])[0].get("catno"),
            country=raw.get("country"),
            year=raw.get("year") or None,
            tracklist=tracklist,
            discogs_raw=raw,
        )
        if musicbrainz_enabled:
            release.musicbrainz_release_id = lookup_release_mbid(discogs_release_id)
        session.add(release)
        session.flush()

    # Assign the relationship, not the FK column: responses serialize
    # acc.release, and a bare release_id assignment leaves that stale-None.
    acc.release = release
    acc.status = AccessionStatus.IDENTIFIED

    # Best-effort side mapping; "?" sides (disc known, side ambiguous) are left
    # for the human — the mapping is editable precisely because of them.
    mapping = map_positions_to_sides(tracklist, acc.disc_count)
    for side in acc.sides:
        entry = mapping.get((side.disc, side.side))
        if entry:
            side.track_positions = entry["positions"]
            side.expected_runtime_sec = entry["runtime_sec"]
    session.flush()
    return acc


def save_photo(
    session: Session,
    s3_client,
    s3_bucket: str,
    acc: Accession,
    *,
    kind: AssetKind,
    disc: int | None,
    side: str | None,
    content: bytes,
    content_type: str | None,
) -> Asset:
    side_id = None
    if disc is not None and side is not None and side != "":
        side_id = format_side_id(acc.accession_id, disc, side.upper())
        if session.get(DiscSide, side_id) is None:
            raise HTTPException(422, f"no such side: {side_id}")
    elif kind == AssetKind.RUNOUT:
        raise HTTPException(422, "runout photos must name a disc and side")

    key = object_key(acc.accession_id, kind.value, disc, side, content_type)
    s3_client.put_object(
        Bucket=s3_bucket,
        Key=key,
        Body=content,
        ContentType=content_type or "application/octet-stream",
    )
    asset = Asset(
        accession_id=acc.accession_id,
        side_id=side_id,
        kind=kind,
        s3_bucket=s3_bucket,
        s3_key=key,
        content_type=content_type,
        bytes=len(content) or None,
    )
    session.add(asset)
    session.flush()
    return asset


def delete_asset(session: Session, s3_client, asset: Asset) -> None:
    """Remove a photo/document: S3 object AND row. Unlike accession deletion
    (which leaves objects behind), a single-asset delete means "this upload was
    a mistake" — nothing should reference it and nothing should keep it."""
    # The row is the authority; a failed S3 delete leaves a harmless orphan
    # object findable under the accession prefix.
    with contextlib.suppress(Exception):
        s3_client.delete_object(Bucket=asset.s3_bucket, Key=asset.s3_key)
    session.delete(asset)
    session.flush()


def side_grid(session: Session, accession_id: str) -> list[dict]:
    rows = session.execute(
        text(
            "SELECT v.side_id, v.disc, v.side, v.speed_rpm, v.expected_runtime_sec, "
            "v.state, v.qc_verdict, v.captured_at, v.current_capture_id, c.bytes "
            "FROM v_side_status v "
            "LEFT JOIN capture c ON c.id = v.current_capture_id "
            "WHERE v.accession_id = :a ORDER BY v.disc, v.side"
        ),
        {"a": accession_id.upper()},
    )
    return [dict(r._mapping) for r in rows]
