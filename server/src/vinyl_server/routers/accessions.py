"""Accession lifecycle API: thin HTTP skin over vinyl_server.services."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload
from vinyl_shared.states import AccessionStatus, AssetKind, SideStatus, Speed

from vinyl_server import services
from vinyl_server.db.models import Accession, Asset
from vinyl_server.db.session import get_session
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


@router.post("", response_model=AccessionOut, status_code=201)
def create_accession(body: AccessionCreate, session: Session = Depends(get_session)):
    return services.create_accession(
        session,
        collection=body.collection,
        disc_count=body.disc_count,
        speed_rpm=body.speed_rpm,
        has_jacket=body.has_jacket,
        has_inner_sleeve=body.has_inner_sleeve,
        condition_notes=body.condition_notes,
    )


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
    return services.get_accession(session, accession_id)


@router.patch("/{accession_id}", response_model=AccessionOut)
def patch_accession(
    accession_id: str, body: AccessionPatch, session: Session = Depends(get_session)
):
    acc = services.get_accession(session, accession_id)

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
            acc.release = None
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
            services.make_sides(acc.accession_id, range(acc.disc_count + 1, new_count + 1), speed)
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
    session.delete(services.get_accession(session, accession_id))


@router.post("/{accession_id}/bind", response_model=AccessionOut)
def bind_release(
    accession_id: str,
    body: BindRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    acc = services.get_accession(session, accession_id)
    return services.bind_release(
        session,
        request.app.state.discogs,
        acc,
        body.discogs_release_id,
        musicbrainz_enabled=request.app.state.musicbrainz_enabled,
    )


@router.get("/{accession_id}/sides", response_model=list[SideStatusOut])
def side_grid(accession_id: str, session: Session = Depends(get_session)):
    """The status grid — what the capture UI renders after a barcode scan."""
    services.get_accession(session, accession_id)  # 404 before empty-list ambiguity
    return [SideStatusOut(**row) for row in services.side_grid(session, accession_id)]


@router.patch("/{accession_id}/sides/{side_id}", response_model=SideStatusOut)
def patch_side(
    accession_id: str,
    side_id: str,
    body: SidePatch,
    session: Session = Depends(get_session),
):
    from vinyl_server.db.models import DiscSide

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
    acc = services.get_accession(session, accession_id)
    return services.save_photo(
        session,
        request.app.state.s3,
        request.app.state.s3_bucket,
        acc,
        kind=kind,
        disc=disc,
        side=side,
        content=file.file.read(),
        content_type=file.content_type,
    )


@router.get("/{accession_id}/assets", response_model=list[AssetOut])
def list_assets(accession_id: str, session: Session = Depends(get_session)):
    services.get_accession(session, accession_id)
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
