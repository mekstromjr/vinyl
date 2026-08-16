"""Configuration from /etc/vinyl-capture/env (systemd EnvironmentFile)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

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
    api_url: str
    api_key: str
    staging_dir: Path
    alsa_device: str
    sample_rate: int
    #: Stop this long after the Discogs expected runtime.
    stop_buffer_sec: int
    #: Hard caps when no expected runtime is known, keyed by RPM.
    hard_cap_sec_33: int
    hard_cap_sec_45: int
    s3_endpoint: str
    s3_region: str
    s3_bucket: str
    s3_access_key: str
    s3_secret_key: str
    #: Home Assistant integration (ticket #6): plug cutoff + phone notify.
    ha_url: str
    ha_token: str
    ha_entity: str
    ha_notify_service: str


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings(
        api_url=_env("VINYL_API_URL", "https://vinyl-api.meklab.net"),
        api_key=_env("VINYL_API_KEY"),
        staging_dir=Path(_env("VINYL_STAGING_DIR", "/var/lib/vinyl-capture/staging")),
        alsa_device=_env("VINYL_ALSA_DEVICE", "hw:USB"),
        sample_rate=int(_env("VINYL_SAMPLE_RATE", "96000")),
        # 360s: the timer is a SAFETY NET behind runout detection now, and a tight
        # buffer truncated GM-0010-D1-B when Discogs undershot the runtime.
        stop_buffer_sec=int(_env("VINYL_STOP_BUFFER_SEC", "360")),
        hard_cap_sec_33=int(_env("VINYL_HARD_CAP_33", str(35 * 60))),
        hard_cap_sec_45=int(_env("VINYL_HARD_CAP_45", str(15 * 60))),
        s3_endpoint=_env("VINYL_S3_ENDPOINT", "https://s3.meklab.net"),
        s3_region=_env("VINYL_S3_REGION", "meklab"),
        s3_bucket=_env("VINYL_S3_BUCKET", "vinyl"),
        s3_access_key=_env("VINYL_S3_ACCESS_KEY"),
        s3_secret_key=_env("VINYL_S3_SECRET_KEY"),
        ha_url=_env("VINYL_HA_URL", ""),
        ha_token=_env("VINYL_HA_TOKEN", ""),
        ha_entity=_env("VINYL_HA_ENTITY", ""),
        ha_notify_service=_env("VINYL_HA_NOTIFY_SERVICE", ""),
    )
