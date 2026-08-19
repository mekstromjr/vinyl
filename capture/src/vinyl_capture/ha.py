"""Home Assistant calls: turntable plug cutoff + phone notification.

Both are best-effort fire-and-forget on a daemon thread: a spinning turntable
is annoying and a missed notification is inconvenient, but neither is data
loss, and NOTHING here may ever delay or fail a capture.
"""

from __future__ import annotations

import contextlib
import logging
import threading

import httpx

from vinyl_capture.config import Settings

log = logging.getLogger(__name__)


def _post(settings: Settings, path: str, payload: dict) -> None:
    def _call() -> None:
        with contextlib.suppress(httpx.HTTPError):
            resp = httpx.post(
                f"{settings.ha_url}{path}",
                headers={"Authorization": f"Bearer {settings.ha_token}"},
                json=payload,
                timeout=10,
            )
            if resp.status_code >= 400:
                log.warning("HA %s -> %s: %s", path, resp.status_code, resp.text[:200])

    threading.Thread(target=_call, daemon=True).start()


def plug_on(settings: Settings) -> None:
    """Called on barcode scan so the turntable is powered before the record is
    on the platter. HA's turn_on is idempotent — no state query needed."""
    if not (settings.ha_url and settings.ha_token and settings.ha_entity):
        return
    _post(settings, "/api/services/switch/turn_on", {"entity_id": settings.ha_entity})


def plug_off(settings: Settings) -> None:
    if not (settings.ha_url and settings.ha_token and settings.ha_entity):
        return
    _post(settings, "/api/services/switch/turn_off", {"entity_id": settings.ha_entity})


def notify(settings: Settings, title: str, message: str) -> None:
    if not (settings.ha_url and settings.ha_token and settings.ha_notify_service):
        return
    _post(
        settings,
        f"/api/services/notify/{settings.ha_notify_service}",
        {"title": title, "message": message},
    )


def capture_finished_message(review: dict) -> tuple[str, str]:
    """(title, message) for the end-of-side notification."""
    side = review.get("side_id") or "capture"
    if review["status"] == "captured":
        s = review.get("summary", {})
        minutes = round(s.get("duration_sec", 0) / 60)
        return (
            f"{side} captured",
            f"{minutes} min, peaks {s.get('peak_dbfs_l')}/{s.get('peak_dbfs_r')} dBFS. "
            "Turntable powered off. Flip the record or scan the next one; "
            "upload happens automatically.",
        )
    reasons = "; ".join(review.get("reasons") or ["unknown"])
    return (f"{side} FAILED", f"{reasons}. The side needs to be recorded again.")
