"""ffmpeg recording session: one ALSA reader, FLAC written untouched, PCM teed
to the live analyzer.

The command shape is the zero-DSP guarantee::

    ffmpeg -f alsa -channels 2 -sample_rate 96000 -acodec pcm_s32le -i hw:USB
           -map 0:a -c:a flac -bits_per_raw_sample 24  <staging>.flac
           -map 0:a -f s32le pipe:1

No filters anywhere near the encode path — both outputs are the same untouched
stream, one losslessly packed, one raw for analysis. `-bits_per_raw_sample 24`
records the truth (the Scarlett delivers 24 valid bits left-justified in S32)
so the FLAC is a genuine 24-bit file, not a 32-bit one with zero padding.

Xruns: ffmpeg's ALSA input logs "ALSA buffer xrun" on stderr when the kernel
ring overruns — each one is lost samples, so the stderr watcher counts them
and integrity fails the capture if any occurred.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from pathlib import Path

from vinyl_capture.analysis import LockedGrooveDetector, StreamStats

_XRUN_RE = re.compile(rb"xrun", re.IGNORECASE)

_CHUNK_BYTES = 4 * 2 * 9600  # 100 ms of S32_LE stereo at 96 kHz


class RecordingSession:
    def __init__(
        self,
        *,
        device: str,
        sample_rate: int,
        out_path: Path,
        rotation_period_sec: float | None = None,
        on_loop=None,
        loop_threshold: float = 0.96,
        loop_revolutions: int = 10,
    ) -> None:
        self.out_path = out_path
        self.stats = StreamStats(sample_rate=sample_rate)
        self.xruns = 0
        self.started_at = time.monotonic()
        self.on_loop = on_loop
        self.loop_detector = (
            LockedGrooveDetector(
                rotation_period_sec,
                threshold=loop_threshold,
                trigger_revolutions=loop_revolutions,
            )
            if rotation_period_sec
            else None
        )
        self._proc = subprocess.Popen(
            [
                "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "warning",
                "-f", "alsa", "-channels", "2", "-sample_rate", str(sample_rate),
                "-acodec", "pcm_s32le", "-thread_queue_size", "4096",
                "-i", device,
                "-map", "0:a", "-c:a", "flac", "-bits_per_raw_sample", "24",
                "-y", str(out_path),
                "-map", "0:a", "-f", "s32le", "pipe:1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._pcm_thread = threading.Thread(target=self._read_pcm, daemon=True)
        self._err_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stderr_tail: list[bytes] = []
        self._pcm_thread.start()
        self._err_thread.start()

    def _read_pcm(self) -> None:
        assert self._proc.stdout is not None
        while True:
            chunk = self._proc.stdout.read(_CHUNK_BYTES)
            if not chunk:
                return
            self.stats.feed(chunk, time.monotonic())
            if self.loop_detector is not None:
                peak = max(self.stats.meter_l, self.stats.meter_r)
                if self.loop_detector.feed(peak, self.stats.duration_sec) and self.on_loop:
                    # New thread: stop() joins this reader thread; calling it
                    # from here would deadlock.
                    threading.Thread(target=self.on_loop, daemon=True).start()

    def _read_stderr(self) -> None:
        assert self._proc.stderr is not None
        for line in self._proc.stderr:
            if _XRUN_RE.search(line):
                self.xruns += 1
            self._stderr_tail = (self._stderr_tail + [line.strip()])[-10:]

    @property
    def elapsed_sec(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def running(self) -> bool:
        return self._proc.poll() is None

    def stop(self, timeout: float = 15.0) -> int:
        """Graceful stop: SIGTERM lets ffmpeg finalize the FLAC header
        (STREAMINFO with sample counts) rather than truncating it."""
        if self.running:
            self._proc.terminate()
        try:
            rc = self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            rc = self._proc.wait()
        self._pcm_thread.join(timeout=5)
        self._err_thread.join(timeout=5)
        return rc

    def stderr_tail(self) -> str:
        return "\n".join(line.decode(errors="replace") for line in self._stderr_tail)
