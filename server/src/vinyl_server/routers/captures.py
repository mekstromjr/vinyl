"""Capture lifecycle API — the endpoints the laptop worker drives.

The state machine lives in vinyl_shared.states (CAPTURE_TRANSITIONS); these
endpoints enforce it, so an out-of-order worker call is a 409, never silent
corruption. 'Which file is the master' stays a database fact: starting a new
attempt demotes the previous current capture in the same transaction that
creates the new one, so the partial unique index can never see two.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from vinyl_shared.states import CaptureStatus, SideStatus, Speed, can_transition

from vinyl_server.db.models import Capture, DiscSide
from vinyl_server.db.session import get_session
from vinyl_server.schemas import (
    CaptureCreate,
    CaptureFinish,
    CaptureOut,
    CaptureUploaded,
    UploadError,
)

router = APIRouter(prefix="/api/captures", tags=["captures"])


def _get_capture(session: Session, capture_id: uuid.UUID) -> Capture:
    cap = session.get(Capture, capture_id)
    if cap is None:
        raise HTTPException(404, f"no such capture: {capture_id}")
    return cap


@router.post("", response_model=CaptureOut, status_code=201)
def start_capture(body: CaptureCreate, session: Session = Depends(get_session)):
    side = session.get(DiscSide, body.side_id.upper())
    if side is None:
        raise HTTPException(404, f"no such side: {body.side_id}")
    if side.status == SideStatus.SKIPPED:
        raise HTTPException(409, f"{side.side_id} is marked skipped: {side.skip_reason}")
    if side.speed_rpm == Speed.RPM_78:
        raise HTTPException(409, "78 RPM sides are phase two — set this record aside")

    previous = session.scalars(
        select(Capture).where(Capture.side_id == side.side_id, Capture.is_current)
    ).first()
    if previous is not None:
        if previous.status == CaptureStatus.RECORDING:
            raise HTTPException(409, f"a capture of {side.side_id} is already recording")
        previous.is_current = False
        session.flush()  # release the partial unique index before the new insert

    cap = Capture(
        side_id=side.side_id,
        is_current=True,
        supersedes=previous.id if previous else None,
        status=CaptureStatus.RECORDING,
    )
    session.add(cap)
    session.flush()
    return cap


@router.get("/{capture_id}", response_model=CaptureOut)
def get_capture(capture_id: uuid.UUID, session: Session = Depends(get_session)):
    return _get_capture(session, capture_id)


@router.patch("/{capture_id}", response_model=CaptureOut)
def finish_capture(
    capture_id: uuid.UUID, body: CaptureFinish, session: Session = Depends(get_session)
):
    cap = _get_capture(session, capture_id)
    if not can_transition(cap.status, body.status):
        raise HTTPException(409, f"illegal transition {cap.status} -> {body.status}")
    cap.status = body.status
    for field in (
        "failure_reason", "integrity", "sha256", "bytes", "local_path",
        "sample_rate", "bit_depth", "duration_sec", "finished_at",
    ):
        value = getattr(body, field)
        if value is not None:
            setattr(cap, field, value)
    session.flush()
    return cap


@router.post("/{capture_id}/uploaded", response_model=CaptureOut)
def mark_uploaded(
    capture_id: uuid.UUID,
    body: CaptureUploaded,
    request: Request,
    session: Session = Depends(get_session),
):
    cap = _get_capture(session, capture_id)
    if not can_transition(cap.status, CaptureStatus.UPLOADED):
        raise HTTPException(409, f"illegal transition {cap.status} -> uploaded")
    cap.s3_bucket = body.s3_bucket
    cap.s3_key = body.s3_key
    cap.status = CaptureStatus.UPLOADED
    session.flush()
    # Event-driven QC: one Job per master. Spawn failure never fails the
    # upload — the side stays qc_pending and /qc re-triggers it.
    request.app.state.spawn_qc(cap.id)
    return cap


@router.post("/{capture_id}/qc", response_model=CaptureOut)
def trigger_qc(
    capture_id: uuid.UUID, request: Request, session: Session = Depends(get_session)
):
    """Manual QC (re-)run: retries after spawn failures, and the backfill path
    for masters uploaded before QC existed."""
    cap = _get_capture(session, capture_id)
    if cap.status != CaptureStatus.UPLOADED:
        raise HTTPException(409, f"capture is {cap.status}, not uploaded")
    if not request.app.state.spawn_qc(cap.id):
        raise HTTPException(502, "QC job spawn failed; see server logs")
    return cap


@router.post("/{capture_id}/upload-error", response_model=CaptureOut)
def report_upload_error(
    capture_id: uuid.UUID, body: UploadError, session: Session = Depends(get_session)
):
    """A failed upload is a retry counter, never a lifecycle change: the file is
    fine on the laptop and the worker will try again."""
    cap = _get_capture(session, capture_id)
    cap.upload_attempts += 1
    cap.upload_error = body.error
    session.flush()
    return cap


@router.get("", response_model=list[CaptureOut])
def list_captures(
    status: CaptureStatus | None = None,
    session: Session = Depends(get_session),
):
    """Worker recovery: after a restart, which captured rows still await upload."""
    q = select(Capture).order_by(Capture.started_at)
    if status is not None:
        q = q.where(Capture.status == status)
    return session.scalars(q).all()
