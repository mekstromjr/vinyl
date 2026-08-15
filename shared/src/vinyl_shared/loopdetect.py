"""Locked-groove detection, shared by the capture station and the QC job.

Stdlib-only on purpose: vinyl-shared runs on a 2009 laptop and in cluster jobs.
"""

from __future__ import annotations


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
