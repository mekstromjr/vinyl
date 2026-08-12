"""Runtime configuration, entirely from environment variables.

Everything lands via the k8s ExternalSecret / Deployment env; there is no config
file. Missing REQUIRED values fail at startup, loudly, rather than at first use.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

_MISSING = object()


def _env(name: str, default: object = _MISSING) -> str:
    value = os.environ.get(name)
    if value:
        return value
    if default is not _MISSING:
        return default  # type: ignore[return-value]
    raise RuntimeError(f"required environment variable {name} is not set")


@dataclass(frozen=True)
class Settings:
    db_url: str
    api_key: str
    discogs_token: str
    s3_endpoint: str
    s3_region: str
    s3_bucket: str
    s3_access_key: str
    s3_secret_key: str
    musicbrainz_enabled: bool


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings(
        db_url=_env("VINYL_DB_URL"),
        api_key=_env("VINYL_API_KEY"),
        discogs_token=_env("VINYL_DISCOGS_TOKEN"),
        s3_endpoint=_env("VINYL_S3_ENDPOINT", "http://garage-s3.infra.svc.cluster.local:3900"),
        s3_region=_env("VINYL_S3_REGION", "meklab"),
        s3_bucket=_env("VINYL_S3_BUCKET", "vinyl"),
        s3_access_key=_env("VINYL_S3_ACCESS_KEY"),
        s3_secret_key=_env("VINYL_S3_SECRET_KEY"),
        musicbrainz_enabled=_env("VINYL_MUSICBRAINZ_ENABLED", "true").lower() == "true",
    )
