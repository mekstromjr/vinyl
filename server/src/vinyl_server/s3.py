"""Garage S3 access for photo/document assets."""

from __future__ import annotations

import uuid

import boto3
from botocore.config import Config

from vinyl_server.config import settings


def make_client():
    s = settings()
    return boto3.client(
        "s3",
        endpoint_url=s.s3_endpoint,
        region_name=s.s3_region,
        aws_access_key_id=s.s3_access_key,
        aws_secret_access_key=s.s3_secret_key,
        # Garage wants path-style addressing (no per-bucket DNS in-cluster).
        # when_required: botocore's flexible-checksum validation (1.36+)
        # aborts reads of multipart objects on Garage — its composite checksum
        # headers don't match botocore's expectation. Integrity is covered by
        # our own end-to-end sha256, recorded at capture time.
        config=Config(
            s3={"addressing_style": "path"},
            response_checksum_validation="when_required",
            request_checksum_calculation="when_required",
        ),
    )


_EXT = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/heic": "heic",
    "image/webp": "webp",
    "application/pdf": "pdf",
}


def object_key(accession_id: str, kind: str, disc: int | None, side: str | None,
               content_type: str | None) -> str:
    """``GM-0142/runout-d1a-3f2a9c1b.jpg`` — accession-prefixed, self-describing.

    A short random suffix keeps re-uploads (retake of a blurry photo) from
    silently overwriting; the asset table is the authority on which is current.
    """
    ext = _EXT.get(content_type or "", "bin")
    side_part = f"-d{disc}{side.lower()}" if disc and side else ""
    return f"{accession_id}/{kind}{side_part}-{uuid.uuid4().hex[:8]}.{ext}"
