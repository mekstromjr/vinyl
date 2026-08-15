"""Live PCM analysis: the meter and most of the integrity report, in one pass.

The recorder tees raw S32_LE PCM to us while ffmpeg writes the FLAC untouched.
Analyzing the live stream means the retry-or-flip verdict needs no second read
of a half-gigabyte file afterwards — the numbers are already accumulated when
recording stops.

Pure-python on purpose (array + max), sized for a 2009 Core 2 Duo: ~770 KB/s
of PCM, chunked at 100 ms, is a few percent of one core.
"""

from __future__ import annotations

import array
import math
from dataclasses import dataclass, field

#: 24 valid bits arrive left-justified in S32. Anything within 256 counts of
#: full scale is a clipped sample as far as a 24-bit converter is concerned.
_FULL_SCALE = 2**31
_CLIP_THRESHOLD = _FULL_SCALE - 256


def dbfs(linear: float) -> float:
    """0.0..1.0 -> dBFS (floored at -120 so silence stays finite)."""
    if linear <= 0:
        return -120.0
    return max(-120.0, 20.0 * math.log10(linear))


@dataclass
class StreamStats:
    """Accumulators over the whole capture + a decaying view for the meter."""

    sample_rate: int
    channels: int = 2

    frames: int = 0
    clipped_samples: int = 0
    peak_l: int = 0
    peak_r: int = 0
    dc_sum_l: int = 0
    dc_sum_r: int = 0

    #: Most recent chunk peaks (linear 0..1) — the live meter reads these.
    meter_l: float = 0.0
    meter_r: float = 0.0
    #: Held peak (linear) with timestamping left to the UI poll loop.
    hold_l: float = 0.0
    hold_r: float = 0.0

    #: Per-second peak envelope (linear 0..1) — the scrub-bar waveform. ~1300
    #: floats for a 22-minute side: negligible to keep, gold to look at.
    envelope: list[float] = field(default_factory=list)
    _bucket_peak: int = field(default=0, repr=False)
    _bucket_frames: int = field(default=0, repr=False)

    _held_since: float = field(default=0.0, repr=False)

    HOLD_SEC = 3.0

    def feed(self, chunk: bytes, now: float) -> None:
        """Consume one interleaved S32_LE chunk."""
        samples = array.array("i")
        samples.frombytes(chunk[: len(chunk) - (len(chunk) % (4 * self.channels))])
        if not samples:
            return
        left = samples[0::2]
        right = samples[1::2]
        self.frames += len(left)

        peak_l = max(max(left), -min(left))
        peak_r = max(max(right), -min(right))
        self.peak_l = max(self.peak_l, peak_l)
        self.peak_r = max(self.peak_r, peak_r)
        self.clipped_samples += sum(
            1 for s in samples if s >= _CLIP_THRESHOLD or s <= -_CLIP_THRESHOLD
        )
        self.dc_sum_l += sum(left)
        self.dc_sum_r += sum(right)

        # Envelope: one peak value per second of audio.
        self._bucket_peak = max(self._bucket_peak, peak_l, peak_r)
        self._bucket_frames += len(left)
        while self._bucket_frames >= self.sample_rate:
            self.envelope.append(round(self._bucket_peak / _FULL_SCALE, 4))
            self._bucket_frames -= self.sample_rate
            self._bucket_peak = 0

        self.meter_l = peak_l / _FULL_SCALE
        self.meter_r = peak_r / _FULL_SCALE
        if self.meter_l >= self.hold_l or self.meter_r >= self.hold_r or (
            now - self._held_since > self.HOLD_SEC
        ):
            self.hold_l = self.meter_l
            self.hold_r = self.meter_r
            self._held_since = now

    @property
    def duration_sec(self) -> float:
        return self.frames / self.sample_rate

    def summary(self) -> dict:
        n = max(self.frames, 1)
        return {
            "duration_sec": round(self.duration_sec, 2),
            "peak_dbfs_l": round(dbfs(self.peak_l / _FULL_SCALE), 2),
            "peak_dbfs_r": round(dbfs(self.peak_r / _FULL_SCALE), 2),
            "clipped_samples": self.clipped_samples,
            "dc_offset_l": round(self.dc_sum_l / n / _FULL_SCALE, 6),
            "dc_offset_r": round(self.dc_sum_r / n / _FULL_SCALE, 6),
            "envelope_1s": self.envelope
            + ([round(self._bucket_peak / _FULL_SCALE, 4)] if self._bucket_frames else []),
        }


class LockedGrooveDetector:
    """Detects the stylus looping one revolution via envelope self-similarity.

    Field-tuned 2026-08-15 against two real timer-capped masters (their tails
    are pure recorded runout). Measured on that data:

    * runout self-correlation at revolution lag: 0.982-0.996
    * real music: median ~0.2; brief repetitive passages reach ~0.95 but never
      SUSTAIN it across ten revolutions
    * threshold 0.97 sits in the gap; fires ~30 s after music ends on both
      validation records, never during music

    Three lessons the first (never-firing) version taught:

    * check lags of one revolution +/- one bucket — a real platter runs a
      fraction off nominal speed and the pattern drifts past an exact lag
    * a sliding 80%-of-last-10-revolutions vote, not a consecutive counter a
      single boundary artifact can reset
    * arm only after music has been heard (3 s smoothed mean above -26 dBFS)
      so a long lead-in groove — also a locked groove — cannot trigger

    The caller still decides what a trigger MEANS: position relative to
    expected runtime separates "scratch, discard" from "side finished".
    """

    def __init__(
        self,
        period_sec: float,
        bucket_sec: float = 0.1,
        threshold: float = 0.97,
        min_level: float = 2e-3,
        trigger_revolutions: int = 10,
        vote_fraction: float = 0.8,
        music_level: float = 0.05,
    ) -> None:
        self.lag = max(2, round(period_sec / bucket_sec))
        self.lags = (self.lag - 1, self.lag, self.lag + 1)
        self.threshold = threshold
        self.min_level = min_level
        self.window_size = trigger_revolutions * self.lag
        self.votes_needed = int(self.window_size * vote_fraction)
        self.music_level = music_level
        self.buckets: list[float] = []
        self.hits: list[bool] = []
        self.music_seen = False
        self.loop_started_at_sec: float | None = None
        self.triggered = False

    @staticmethod
    def _correlate(a: list[float], b: list[float]) -> float:
        n = len(a)
        ma, mb = sum(a) / n, sum(b) / n
        va = sum((x - ma) ** 2 for x in a)
        vb = sum((x - mb) ** 2 for x in b)
        if va < 1e-12 or vb < 1e-12:
            return 1.0 if abs(ma - mb) < 1e-6 else 0.0
        cov = sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True))
        return cov / (va * vb) ** 0.5

    def feed(self, peak: float, at_sec: float) -> bool:
        """Returns True exactly once, when the vote threshold is crossed."""
        self.buckets.append(peak)
        keep = 2 * self.lags[-1] + 2
        if len(self.buckets) > keep:
            del self.buckets[: len(self.buckets) - keep]
        if self.triggered:
            return False

        if not self.music_seen:
            recent = self.buckets[-30:]
            if len(recent) == 30 and sum(recent) / 30 > self.music_level:
                self.music_seen = True
            return False

        if len(self.buckets) < 2 * self.lags[-1]:
            return False
        window = self.buckets[-self.lag:]
        level = sum(window) / self.lag
        best = max(
            self._correlate(window, self.buckets[-self.lag - lag: -lag])
            for lag in self.lags
            if len(self.buckets) >= self.lag + lag
        )
        self.hits.append(best >= self.threshold and level >= self.min_level)
        if len(self.hits) > self.window_size:
            self.hits.pop(0)

        if len(self.hits) == self.window_size and sum(self.hits) >= self.votes_needed:
            self.triggered = True
            self.loop_started_at_sec = max(0.0, at_sec - self.window_size * 0.1)
            return True
        return False

    @property
    def revolutions(self) -> float:
        return sum(self.hits) / self.lag if self.hits else 0.0


#: Rotation period by nominal RPM (33 means 33-1/3 — 60/33 would be wrong).
ROTATION_PERIOD_SEC = {33: 1.8, 45: 60 / 45, 78: 60 / 78}


@dataclass
class IntegrityVerdict:
    passed: bool
    reasons: list[str]
    report: dict


def judge(
    stats_summary: dict,
    *,
    xruns: int,
    expected_runtime_sec: int | None,
    min_duration_sec: float = 30.0,
    clip_limit: int = 50,
    dc_limit: float = 0.01,
    locked_groove: bool = False,
    loop_started_at_sec: float | None = None,
) -> IntegrityVerdict:
    """Recording-integrity verdict. The test for what belongs here: would
    redoing the capture change this number? (Surface noise wouldn't — that's
    the cluster QC's business.)

    Thresholds are deliberately forgiving: this gate catches captures that are
    MECHANICALLY broken, not records that sound bad.
    """
    reasons = []
    if locked_groove:
        at = ""
        if loop_started_at_sec is not None:
            at = f" at ~{int(loop_started_at_sec // 60)}:{int(loop_started_at_sec % 60):02d}"
        reasons.append(
            f"locked groove{at} — the stylus looped the same revolution; "
            "clean the record and re-try"
        )
    if xruns > 0:
        # Xruns lose samples permanently; the file is unrepairable.
        reasons.append(f"{xruns} ALSA xrun(s) — samples were lost")
    if stats_summary["clipped_samples"] > clip_limit:
        reasons.append(
            f"{stats_summary['clipped_samples']} clipped samples — gain too hot"
        )
    if abs(stats_summary["dc_offset_l"]) > dc_limit or abs(stats_summary["dc_offset_r"]) > dc_limit:
        reasons.append("DC offset — interface problem, not a record problem")
    if stats_summary["duration_sec"] < min_duration_sec:
        reasons.append(f"only {stats_summary['duration_sec']:.0f}s captured")
    if (
        not locked_groove
        and expected_runtime_sec
        and stats_summary["duration_sec"] < expected_runtime_sec - 60
    ):
        reasons.append(
            f"duration {stats_summary['duration_sec']:.0f}s is short of expected "
            f"{expected_runtime_sec}s — stopped early or wrong side?"
        )

    report = dict(
        stats_summary,
        xruns=xruns,
        expected_runtime_sec=expected_runtime_sec,
        loop_started_at_sec=loop_started_at_sec,
    )
    return IntegrityVerdict(passed=not reasons, reasons=reasons, report=report)


def stop_after_sec(
    expected_runtime_sec: int | None, speed_rpm: int, *, buffer_sec: int,
    hard_cap_33: int, hard_cap_45: int,
) -> int:
    """When the auto-stop timer fires, from arm time."""
    cap = hard_cap_45 if speed_rpm == 45 else hard_cap_33
    if expected_runtime_sec:
        return min(expected_runtime_sec + buffer_sec, cap)
    return cap
