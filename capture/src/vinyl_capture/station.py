"""The capture station: one state machine, one capture at a time.

    IDLE -> RECORDING -> REVIEW -> IDLE
                 \\-> (cancel) -> IDLE

Everything durable lives in the cluster; this object holds only the in-flight
session. If the process dies mid-capture, the cluster row stays 'recording'
until the next arm of that side demotes it — and the staging FLAC survives on
disk for manual salvage.
"""

from __future__ import annotations

import contextlib
import hashlib
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from vinyl_capture import ha
from vinyl_capture.analysis import ROTATION_PERIOD_SEC, judge, stop_after_sec
from vinyl_capture.apiclient import VinylApi
from vinyl_capture.config import Settings
from vinyl_capture.recorder import RecordingSession

_RIPPER_PROCESSES = ("dvdbackup", "mkvmerge", "ddrescue")


def dvd_rip_in_flight() -> bool:
    """Same 5400 rpm spindle: refuse to arm while the DVD ripper is working."""
    for name in _RIPPER_PROCESSES:
        if subprocess.run(["pgrep", "-x", name], capture_output=True).returncode == 0:
            return True
    return False


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


class Station:
    def __init__(self, settings: Settings, api: VinylApi) -> None:
        self.settings = settings
        self.api = api
        self.phase = "idle"          # idle | recording | review
        self.side: dict | None = None
        self.capture_id: str | None = None
        self.session: RecordingSession | None = None
        self.review: dict | None = None
        self.stop_deadline: float | None = None
        self._lock = threading.Lock()
        self._stop_reason: str | None = None
        self._timer: threading.Timer | None = None
        self.settings.staging_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ arm --
    def arm(self, side: dict) -> None:
        with self._lock:
            if self.phase != "idle":
                raise RuntimeError(f"station is {self.phase}, not idle")
            if dvd_rip_in_flight():
                raise RuntimeError(
                    "a DVD rip is in progress on this machine — capture would "
                    "fight it for the disk; wait for it to finish"
                )
            cap = self.api.start_capture(side["side_id"])
            out = self.settings.staging_dir / f"{side['side_id']}__{cap['id']}.flac"
            self.session = RecordingSession(
                device=self.settings.alsa_device,
                sample_rate=self.settings.sample_rate,
                out_path=out,
                rotation_period_sec=ROTATION_PERIOD_SEC.get(side["speed_rpm"]),
                on_loop=self._on_loop,
            )
            self.side = side
            self.capture_id = cap["id"]
            self.phase = "recording"
            self.review = None
            self._stop_reason = None

            limit = stop_after_sec(
                side.get("expected_runtime_sec"),
                side["speed_rpm"],
                buffer_sec=self.settings.stop_buffer_sec,
                hard_cap_33=self.settings.hard_cap_sec_33,
                hard_cap_45=self.settings.hard_cap_sec_45,
            )
            self.stop_deadline = time.monotonic() + limit
            self._timer = threading.Timer(limit, self._auto_stop)
            self._timer.daemon = True
            self._timer.start()

    def _auto_stop(self) -> None:
        with contextlib.suppress(RuntimeError):  # operator beat the timer to it
            self.stop(reason="timer")

    def _on_loop(self) -> None:
        """The stylus is looping one revolution. Position decides the meaning:
        mid-record it is a scratch (fail + discard); at/after the expected
        runtime it is the runout groove doing its job (success, ~3 min sooner
        than the timer). Unbound sides can't be distinguished -> keep capture,
        note the periodicity for human review."""
        side = self.side or {}
        expected = side.get("expected_runtime_sec")
        elapsed = self.session.elapsed_sec if self.session else 0
        reason = "locked_groove" if expected and elapsed < expected - 60 else "runout"
        with contextlib.suppress(RuntimeError):
            self.stop(reason=reason)

    # ----------------------------------------------------------------- stop --
    def stop(self, reason: str = "operator") -> dict:
        with self._lock:
            if self.phase != "recording" or self.session is None:
                raise RuntimeError("nothing is recording")
            if self._timer:
                self._timer.cancel()
            self._stop_reason = reason
            session, side = self.session, self.side
            self.session = None

        session.stop()
        ha.plug_off(self.settings)

        summary = session.stats.summary()
        loop = session.loop_detector
        verdict = judge(
            summary,
            xruns=session.xruns,
            expected_runtime_sec=(side or {}).get("expected_runtime_sec"),
            locked_groove=(reason == "locked_groove"),
            loop_started_at_sec=loop.loop_started_at_sec if loop else None,
        )
        sha = _sha256(session.out_path) if session.out_path.exists() else None
        size = session.out_path.stat().st_size if session.out_path.exists() else None

        status = "captured" if verdict.passed else "failed"
        self.api.finish_capture(
            self.capture_id,
            status=status,
            failure_reason="; ".join(verdict.reasons) or None,
            integrity=dict(verdict.report, stop_reason=reason,
                           stderr_tail=session.stderr_tail() or None),
            sha256=sha,
            bytes=size,
            local_path=str(session.out_path),
            sample_rate=self.settings.sample_rate,
            bit_depth=24,
            duration_sec=summary["duration_sec"],
            finished_at=datetime.now(UTC).isoformat(),
        )
        if status == "failed":
            # A rejected capture never uploads; the file is worthless.
            session.out_path.unlink(missing_ok=True)

        with self._lock:
            self.phase = "review"
            self.review = {
                "side_id": (side or {}).get("side_id"),
                "status": status,
                "reasons": verdict.reasons,
                "summary": summary,
                "xruns": session.xruns,
                "stop_reason": reason,
            }
        ha.notify(self.settings, *ha.capture_finished_message(self.review))
        return self.review

    def cancel(self) -> None:
        """Operator abort: needle skated, wrong record, cat on turntable."""
        with self._lock:
            if self.phase != "recording" or self.session is None:
                raise RuntimeError("nothing is recording")
            if self._timer:
                self._timer.cancel()
            session = self.session
            self.session = None
        session.stop()
        ha.plug_off(self.settings)
        session.out_path.unlink(missing_ok=True)
        self.api.finish_capture(
            self.capture_id, status="failed", failure_reason="cancelled by operator",
            finished_at=datetime.now(UTC).isoformat(),
        )
        with self._lock:
            self.phase = "idle"
            self.side = None
            self.capture_id = None

    def acknowledge(self) -> None:
        """Operator has read the review screen; back to idle."""
        with self._lock:
            self.phase = "idle"
            self.side = None
            self.capture_id = None
            self.review = None

    # ---------------------------------------------------------------- status --
    def status(self) -> dict:
        stats = self.session.stats if self.session else None
        return {
            "phase": self.phase,
            "side": self.side,
            "capture_id": self.capture_id,
            "elapsed_sec": round(self.session.elapsed_sec, 1) if self.session else None,
            "remaining_sec": (
                round(self.stop_deadline - time.monotonic())
                if self.phase == "recording" and self.stop_deadline
                else None
            ),
            "meter": (
                {
                    "l": stats.meter_l, "r": stats.meter_r,
                    "hold_l": stats.hold_l, "hold_r": stats.hold_r,
                    "clipped": stats.clipped_samples,
                }
                if stats
                else None
            ),
            "review": self.review,
        }
