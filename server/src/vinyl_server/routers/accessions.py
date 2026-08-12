"""Accession lifecycle: create, read, update, bind, photos, side grid."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy import select, text
from sqlalchemy.orm import Session, joinedload
from vinyl_shared.ids import format_accession_id, format_side_id
from vinyl_shared.states import AccessionStatus, AssetKind, SideStatus, Speed

from vinyl_server.db.models import Accession, Asset, DiscSide, Release
from vinyl_server.db.session import get_session
from vinyl_server.musicbrainz import lookup_release_mbid
from vinyl_server.s3 import object_key
from vinyl_server.schemas import (
    AccessionCreate,
    AccessionOut,
    AccessionPatch,
    AssetOut,
    BindRequest,
    SidePatch,
    SideStatusOut,
)

router = APIRouter(prefix="/api/accessions", tags=["accessions"])


def _get_accession(session: Session, accession_id: str) -> Accession:
    acc = session.get(
        Accession, accession_id.upper(), options=[joinedload(Accession.release)]
    )
    if acc is None:
        raise HTTPException(404, f"no such accession: {accession_id}")
    return acc


def _make_sides(accession_id: str, discs: range, speed: Speed) -> list[DiscSide]:
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


@router.post("", response_model=AccessionOut, status_code=201)
def create_accession(body: AccessionCreate, session: Session = Depends(get_session)):
    # Row-locking allocation: two concurrent sessions can never mint one number.
    row = session.execute(
        text(
            "UPDATE collection SET next_number = next_number + 1 "
            "WHERE prefix = :p RETURNING next_number - 1"
        ),
        {"p": body.collection},
    ).first()
    if row is None:
        raise HTTPException(422, f"no such collection: {body.collection}")
    accession_id = format_accession_id(body.collection, row[0])

    acc = Accession(
        accession_id=accession_id,
        collection=body.collection,
        number=row[0],
        disc_count=body.disc_count,
        has_jacket=body.has_jacket,
        has_inner_sleeve=body.has_inner_sleeve,
        condition_notes=body.condition_notes,
        status=AccessionStatus.INVENTORIED,
    )
    session.add(acc)
    session.add_all(_make_sides(accession_id, range(1, body.disc_count + 1), body.speed_rpm))
    session.flush()
    return _get_accession(session, accession_id)


@router.get("", response_model=list[AccessionOut])
def list_accessions(
    status: AccessionStatus | None = None,
    limit: int = 50,
    offset: int = 0,
    session: Session = Depends(get_session),
):
    q = (
        select(Accession)
        .options(joinedload(Accession.release))
        .order_by(Accession.number.desc())
        .limit(min(limit, 200))
        .offset(offset)
    )
    if status is not None:
        q = q.where(Accession.status == status)
    return session.scalars(q).all()


@router.get("/{accession_id}", response_model=AccessionOut)
def get_accession(accession_id: str, session: Session = Depends(get_session)):
    return _get_accession(session, accession_id)


@router.patch("/{accession_id}", response_model=AccessionOut)
def patch_accession(
    accession_id: str, body: AccessionPatch, session: Session = Depends(get_session)
):
    acc = _get_accession(session, accession_id)

    if body.disc_count is not None and body.disc_count != acc.disc_count:
        _resize_sides(session, acc, body.disc_count)
    for field in ("has_jacket", "has_inner_sleeve", "condition_notes"):
        value = getattr(body, field)
        if value is not None:
            setattr(acc, field, value)
    if body.status is not None:
        if body.status == AccessionStatus.IDENTIFIED and acc.release_id is None:
            raise HTTPException(422, "cannot mark identified without a bound release; use /bind")
        if body.status == AccessionStatus.UNIDENTIFIABLE:
            acc.release_id = None
        acc.status = body.status
    session.flush()
    return acc


def _resize_sides(session: Session, acc: Accession, new_count: int) -> None:
    """Grow or shrink side rows with the disc count. Shrinking is only legal
    while the removed sides have no captures — after that, the disc count is a
    physical fact with recorded evidence and correcting it is a manual affair."""
    if new_count > acc.disc_count:
        speed = acc.sides[0].speed_rpm if acc.sides else Speed.RPM_33
        session.add_all(
            _make_sides(acc.accession_id, range(acc.disc_count + 1, new_count + 1), speed)
        )
    else:
        doomed = [s for s in acc.sides if s.disc > new_count]
        if any(s.captures for s in doomed):
            raise HTTPException(409, "cannot shrink disc_count: captures exist on removed discs")
        for side in doomed:
            session.delete(side)
    acc.disc_count = new_count


@router.delete("/{accession_id}", status_code=204)
def delete_accession(accession_id: str, session: Session = Depends(get_session)):
    """For inventory mistakes. Cascades to sides/captures/assets rows; S3
    objects are left behind (cheap, and the accession prefix makes them findable)."""
    session.delete(_get_accession(session, accession_id))


@router.post("/{accession_id}/bind", response_model=AccessionOut)
def bind_release(
    accession_id: str,
    body: BindRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    from vinyl_server.discogs import map_positions_to_sides, normalize_tracklist

    acc = _get_accession(session, accession_id)
    client = request.app.state.discogs
    raw = client.get_release(body.discogs_release_id)
    tracklist = normalize_tracklist(raw)

    release = session.get(Release, body.discogs_release_id)
    if release is None:
        release = Release(
            discogs_release_id=body.discogs_release_id,
            artist=", ".join(a.get("name", "") for a in raw.get("artists", [])) or "Unknown",
            title=raw.get("title", "Unknown"),
            label=(raw.get("labels") or [{}])[0].get("name"),
            catalog_number=(raw.get("labels") or [{}])[0].get("catno"),
            country=raw.get("country"),
            year=raw.get("year") or None,
            tracklist=tracklist,
            discogs_raw=raw,
        )
        if request.app.state.musicbrainz_enabled:
            release.musicbrainz_release_id = lookup_release_mbid(body.discogs_release_id)
        session.add(release)
        session.flush()

    # Assign the relationship, not the FK column: the response serializes
    # acc.release, and a bare release_id assignment leaves that stale-None.
    acc.release = release
    acc.status = AccessionStatus.IDENTIFIED

    # Best-effort side mapping; "?" sides (disc known, side ambiguous) are left
    # for the human — the mapping is editable via PATCH /sides for exactly this.
    mapping = map_positions_to_sides(tracklist, acc.disc_count)
    for side in acc.sides:
        entry = mapping.get((side.disc, side.side))
        if entry:
            side.track_positions = entry["positions"]
            side.expected_runtime_sec = entry["runtime_sec"]
    session.flush()
    return acc


@router.get("/{accession_id}/sides", response_model=list[SideStatusOut])
def side_grid(accession_id: str, session: Session = Depends(get_session)):
    """The status grid — what the capture UI renders after a barcode scan."""
    _get_accession(session, accession_id)  # 404 before empty-list ambiguity
    rows = session.execute(
        text(
            "SELECT side_id, disc, side, speed_rpm, expected_runtime_sec, state, "
            "qc_verdict, captured_at FROM v_side_status "
            "WHERE accession_id = :a ORDER BY disc, side"
        ),
        {"a": accession_id.upper()},
    )
    return [SideStatusOut(**dict(r._mapping)) for r in rows]


@router.patch("/{accession_id}/sides/{side_id}", response_model=SideStatusOut)
def patch_side(
    accession_id: str,
    side_id: str,
    body: SidePatch,
    session: Session = Depends(get_session),
):
    side = session.get(DiscSide, side_id.upper())
    if side is None or side.accession_id != accession_id.upper():
        raise HTTPException(404, f"no such side: {side_id}")
    if body.speed_rpm is not None:
        side.speed_rpm = body.speed_rpm
    if body.track_positions is not None:
        side.track_positions = body.track_positions
    if body.expected_runtime_sec is not None:
        side.expected_runtime_sec = body.expected_runtime_sec
    if body.skipped is not None:
        side.status = SideStatus.SKIPPED if body.skipped else SideStatus.PENDING
        side.skip_reason = body.skip_reason if body.skipped else None
    session.flush()
    grid = side_grid(accession_id, session)
    return next(s for s in grid if s.side_id == side.side_id)


@router.post("/{accession_id}/photos", response_model=AssetOut, status_code=201)
def upload_photo(
    accession_id: str,
    request: Request,
    file: UploadFile = File(...),
    kind: AssetKind = Form(...),
    disc: int | None = Form(default=None),
    side: str | None = Form(default=None),
    session: Session = Depends(get_session),
):
    acc = _get_accession(session, accession_id)
    side_id = None
    if disc is not None and side is not None:
        side_id = format_side_id(acc.accession_id, disc, side.upper())
        if session.get(DiscSide, side_id) is None:
            raise HTTPException(422, f"no such side: {side_id}")
    elif kind == AssetKind.RUNOUT:
        raise HTTPException(422, "runout photos must name a disc and side")

    key = object_key(acc.accession_id, kind.value, disc, side, file.content_type)
    body = file.file.read()
    request.app.state.s3.put_object(
        Bucket=request.app.state.s3_bucket,
        Key=key,
        Body=body,
        ContentType=file.content_type or "application/octet-stream",
    )
    asset = Asset(
        accession_id=acc.accession_id,
        side_id=side_id,
        kind=kind,
        s3_bucket=request.app.state.s3_bucket,
        s3_key=key,
        content_type=file.content_type,
        bytes=len(body) or None,
    )
    session.add(asset)
    session.flush()
    return asset


@router.get("/{accession_id}/assets", response_model=list[AssetOut])
def list_assets(accession_id: str, session: Session = Depends(get_session)):
    _get_accession(session, accession_id)
    return session.scalars(
        select(Asset).where(Asset.accession_id == accession_id.upper()).order_by(Asset.created_at)
    ).all()


@router.get("/{accession_id}/assets/{asset_id}/content")
def asset_content(
    accession_id: str,
    asset_id: uuid.UUID,
    request: Request,
    session: Session = Depends(get_session),
):
    from fastapi.responses import StreamingResponse

    asset = session.get(Asset, asset_id)
    if asset is None or asset.accession_id != accession_id.upper():
        raise HTTPException(404, "no such asset")
    obj = request.app.state.s3.get_object(Bucket=asset.s3_bucket, Key=asset.s3_key)
    return StreamingResponse(
        obj["Body"], media_type=asset.content_type or "application/octet-stream"
    )
