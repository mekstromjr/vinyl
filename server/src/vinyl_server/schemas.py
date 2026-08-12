"""Pydantic request/response models for the API."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field
from vinyl_shared.states import AccessionStatus, AssetKind, Speed


class AccessionCreate(BaseModel):
    collection: str = "GM"
    disc_count: int = Field(ge=1, le=20)
    speed_rpm: Speed = Speed.RPM_33
    has_jacket: bool = True
    has_inner_sleeve: bool = True
    condition_notes: str | None = None


class AccessionPatch(BaseModel):
    disc_count: int | None = Field(default=None, ge=1, le=20)
    has_jacket: bool | None = None
    has_inner_sleeve: bool | None = None
    condition_notes: str | None = None
    status: AccessionStatus | None = None


class BindRequest(BaseModel):
    discogs_release_id: int


class SidePatch(BaseModel):
    speed_rpm: Speed | None = None
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


class AccessionOut(BaseModel):
    model_config = {"from_attributes": True}

    accession_id: str
    collection: str
    number: int
    disc_count: int
    has_jacket: bool
    has_inner_sleeve: bool
    condition_notes: str | None
    status: AccessionStatus
    release: ReleaseOut | None
    inventoried_at: datetime


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
