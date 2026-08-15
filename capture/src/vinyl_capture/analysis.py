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

    Fed one peak value per ~100 ms chunk. A locked groove makes the envelope
    periodic at exactly the rotation period (1.8 s at 33 1/3; 1.33 s at 45):
    the last revolution's envelope correlates near-perfectly with the previous
    one, sustained for revolution after revolution. Real music rarely holds
    that correlation for even two revolutions.

    The caller decides what a trigger MEANS — the runout groove is a locked
    groove by design, so position relative to expected runtime separates
    "scratch, discard" from "side finished".
    """

    def __init__(
        self,
        period_sec: float,
        bucket_sec: float = 0.1,
        threshold: float = 0.96,
        min_level: float = 5e-4,
        trigger_revolutions: int = 10,
    ) -> None:
        self.lag = max(2, round(period_sec / bucket_sec))
        self.threshold = threshold
        self.min_level = min_level
        self.trigger_revolutions = trigger_revolutions
        self.buckets: list[float] = []
        self.matching_buckets = 0
        self.loop_started_at_sec: float | None = None
        self.triggered = False

    @staticmethod
    def _correlate(a: list[float], b: list[float]) -> float:
        n = len(a)
        ma, mb = sum(a) / n, sum(b) / n
        va = sum((x - ma) ** 2 for x in a)
        vb = sum((x - mb) ** 2 for x in b)
        if va < 1e-12 or vb < 1e-12:
            # Flat envelopes: identical loud plateaus count as similar; silence
            # (both flat AND quiet) must not.
            return 1.0 if abs(ma - mb) < 1e-6 else 0.0
        cov = sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True))
        return cov / (va * vb) ** 0.5

    def feed(self, peak: float, at_sec: float) -> bool:
        """Returns True exactly once, when the trigger threshold is crossed."""
        self.buckets.append(peak)
        if len(self.buckets) > 2 * self.lag:
            del self.buckets[: len(self.buckets) - 2 * self.lag]
        if self.triggered or len(self.buckets) < 2 * self.lag:
            return False

        recent = self.buckets[self.lag:]
        previous = self.buckets[: self.lag]
        level = sum(recent) / len(recent)
        similar = (
            level >= self.min_level
            and self._correlate(recent, previous) >= self.threshold
        )
        if similar:
            self.matching_buckets += 1
            if self.loop_started_at_sec is None:
                self.loop_started_at_sec = max(0.0, at_sec - 2 * self.lag * 0.1)
        else:
            self.matching_buckets = 0
            self.loop_started_at_sec = None

        if self.matching_buckets >= self.trigger_revolutions * self.lag:
            self.triggered = True
            return True
        return False

    @property
    def revolutions(self) -> float:
        return self.matching_buckets / self.lag


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
