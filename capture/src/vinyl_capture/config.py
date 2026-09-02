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
    #: Station identity reported to the cluster. Empty = derive from hostname.
    station_id: str
    station_name: str
    #: Serve the kiosk HTML at "/". False leaves the JSON control surface intact
    #: and drops only the page — for a station driven from the cluster UI that
    #: nobody stands in front of.
    serve_kiosk: bool


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
        station_id=_env("VINYL_STATION_ID", ""),
        station_name=_env("VINYL_STATION_NAME", ""),
        # Defaults ON: every station today has an operator standing at it, and
        # the local page is the deliberate offline fallback for when the
        # cluster is unreachable. Turning it off is opt-in.
        serve_kiosk=_env("VINYL_SERVE_KIOSK", "1") not in ("0", "false", "no"),
    )
