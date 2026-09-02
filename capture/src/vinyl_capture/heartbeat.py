"""Station presence: register once, then heartbeat.

Runs in a daemon thread beside the uploader. Its whole job is to keep the
cluster's view of this station current, which makes it strictly optional to
capturing — so every failure path here degrades to "try again next tick" and
none of them can interrupt a recording.

The payload is deliberately small. ``Station.status()`` carries
``review.summary.envelope_1s``, a per-second amplitude array for a whole side;
posting that on every tick would ship megabytes of waveform for a status line
that needs a phase and a side id.
"""

from __future__ import annotations

import logging
import threading

from vinyl_capture import identity
from vinyl_capture.apiclient import ApiError, VinylApi
from vinyl_capture.config import Settings
from vinyl_capture.station import Station

log = logging.getLogger(__name__)

#: Comfortably inside the server's 180 s online window, so a single missed beat
#: does not flap the station offline.
INTERVAL_SEC = 30.0


class Heartbeat:
    def __init__(
        self,
        settings: Settings,
        api: VinylApi,
        station: Station,
        uploader=None,
        interval_sec: float = INTERVAL_SEC,
    ) -> None:
        self.settings = settings
        self.api = api
        self.station = station
        self.uploader = uploader
        self.interval_sec = interval_sec
        self.station_id = identity.station_id(settings.station_id or None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._registered = False

    # ------------------------------------------------------------- payloads --
    def registration(self) -> dict:
        return {
            "display_name": self.settings.station_name or self.station_id,
            "hostname": identity.hostname(),
            "capabilities": identity.capabilities(self.settings.alsa_device),
            "agent_version": identity.agent_version(),
        }

    def beat(self) -> dict:
        pending = None
        if self.uploader is not None:
            try:
                pending = self.uploader.status().get("staged")
            except Exception:  # uploader internals must never break presence
                pending = None
        side = self.station.side or {}
        return {
            "phase": self.station.phase,
            "current_side_id": side.get("side_id"),
            "pending_uploads": pending,
        }

    # -------------------------------------------------------------- one tick --
    def register(self) -> None:
        self.api.register_station(self.station_id, **self.registration())
        self._registered = True
        log.info("registered station %s", self.station_id)

    def tick(self) -> None:
        """One presence update. Re-registers if the server has never seen us —
        or has forgotten us, which a 404 on heartbeat means."""
        if not self._registered:
            self.register()
            return
        try:
            self.api.station_heartbeat(self.station_id, **self.beat())
        except ApiError as exc:
            if "404" in str(exc):
                # Row deleted while we were running: re-announce rather than
                # heartbeating into the void until someone notices.
                log.warning("station %s unknown to the server — re-registering", self.station_id)
                self.register()
            else:
                raise

    # ---------------------------------------------------------------- thread --
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except ApiError as exc:
                # The cluster being briefly unreachable is ordinary; the
                # recorder does not care and neither should this.
                log.warning("heartbeat failed (will retry): %s", exc)
            except Exception:
                log.exception("unexpected heartbeat error (will retry)")
            self._stop.wait(self.interval_sec)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="heartbeat", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
