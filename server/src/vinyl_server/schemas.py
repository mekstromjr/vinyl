"""Pydantic request/response models for the API."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field
from vinyl_shared.states import (
    AccessionStatus,
    AssetKind,
    CaptureStatus,
    ConditionGrade,
    QCVerdict,
    Speed,
)


class AccessionCreate(BaseModel):
    collection: str = "GM"
    disc_count: int = Field(ge=1, le=20)
    speed_rpm: Speed = Speed.RPM_33
    has_jacket: bool = True
    has_inner_sleeve: bool = True
    condition_grade: ConditionGrade | None = None
    condition_notes: str | None = None


class AccessionPatch(BaseModel):
    disc_count: int | None = Field(default=None, ge=1, le=20)
    has_jacket: bool | None = None
    has_inner_sleeve: bool | None = None
    condition_grade: ConditionGrade | None = None
    condition_notes: str | None = None
    status: AccessionStatus | None = None


class BindRequest(BaseModel):
    discogs_release_id: int


class SidePatch(BaseModel):
    speed_rpm: Speed | None = None
    #: Explicit clear: None means "not provided" for every other field here.
    clear_expected_runtime: bool = False
    track_positions: list[str] | None = None
    expected_runtime_sec: int | None = Field(default=None, gt=0)
    skipped: bool | None = None
    skip_reason: str | None = None


class ReleaseOut(BaseModel):
    model_config = {"from_attributes": True}

    discogs_release_id: int
    musicbrainz_release_id: uuid.UUID | None
    artist: str
    title: str
    label: str | None
    catalog_number: str | None
    country: str | None
    year: int | None
    tracklist: list


class SideStatusOut(BaseModel):
    side_id: str
    disc: int
    side: str
    speed_rpm: int
    expected_runtime_sec: int | None
    state: str
    qc_verdict: str | None
    captured_at: datetime | None
    bytes: int | None = None


class AccessionOut(BaseModel):
    model_config = {"from_attributes": True}

    accession_id: str
    collection: str
    number: int
    disc_count: int
    has_jacket: bool
    has_inner_sleeve: bool
    condition_grade: ConditionGrade | None
    condition_notes: str | None
    status: AccessionStatus
    release: ReleaseOut | None
    inventoried_at: datetime


class CaptureCreate(BaseModel):
    side_id: str


class CaptureFinish(BaseModel):
    status: CaptureStatus  # captured | failed
    failure_reason: str | None = None
    integrity: dict | None = None
    sha256: str | None = None
    bytes: int | None = Field(default=None, gt=0)
    local_path: str | None = None
    sample_rate: int | None = None
    bit_depth: int | None = None
    duration_sec: float | None = None
    finished_at: datetime | None = None


class CaptureUploaded(BaseModel):
    s3_bucket: str
    s3_key: str


class UploadError(BaseModel):
    error: str


class CaptureOut(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    side_id: str
    is_current: bool
    supersedes: uuid.UUID | None
    status: CaptureStatus
    failure_reason: str | None
    integrity: dict | None
    qc_verdict: QCVerdict | None
    sha256: str | None
    bytes: int | None
    local_path: str | None
    s3_bucket: str | None
    s3_key: str | None
    sample_rate: int | None
    bit_depth: int | None
    duration_sec: float | None
    upload_attempts: int
    started_at: datetime
    finished_at: datetime | None


class AssetOut(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    accession_id: str
    side_id: str | None
    kind: AssetKind
    s3_key: str
    content_type: str | None
    bytes: int | None
    created_at: datetime


class StationRegister(BaseModel):
    """Sent once when an agent starts. Idempotent: re-registering an existing
    station_id updates it rather than conflicting, so a restart or a redeploy is
    not an error."""

    display_name: str = Field(max_length=200)
    hostname: str | None = Field(default=None, max_length=200)
    #: Probed hardware — ALSA device, optical drive, ffmpeg version. Free-form
    #: because station hardware is not a closed set.
    capabilities: dict | None = None
    agent_version: str | None = Field(default=None, max_length=32)


class StationHeartbeat(BaseModel):
    """Sent periodically while the agent runs.

    Deliberately a small, flat payload rather than the agent's whole
    ``Station.status()``: that blob carries ``review.summary.envelope_1s``, a
    per-second amplitude array for the entire side. Posting it every few seconds
    would ship megabytes of waveform to the server for a status line that only
    needs a phase and a side id.
    """

    phase: str | None = Field(default=None, max_length=20)
    current_side_id: str | None = Field(default=None, max_length=24)
    pending_uploads: int | None = Field(default=None, ge=0)


class StationOut(BaseModel):
    station_id: str
    display_name: str
    hostname: str | None
    capabilities: dict | None
    agent_version: str | None
    phase: str | None
    current_side_id: str | None
    pending_uploads: int | None
    last_seen_at: datetime | None
    registered_at: datetime
    #: Derived at read time from ``last_seen_at``, never stored: an agent that
    #: dies without a clean shutdown cannot leave a stale "online" behind.
    online: bool

    model_config = {"from_attributes": True}
