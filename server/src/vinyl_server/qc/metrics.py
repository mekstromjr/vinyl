"""Metric extraction: two streamed ffmpeg decodes, numpy accumulation.

Nothing here loads the full file into memory — a 96k/24 side decodes to
~1.2 GB of float32, so everything streams in chunks through accumulators.

Pass 1 (full-band stereo): peaks, clipping, DC, per-channel RMS, L/R
correlation (mono-fold candidate detection), 100 ms peak envelope (the
waveform + the locked-groove sweep input), music onset/end, lead-in noise
floor, dropout detection.

Pass 2 (3 kHz high-passed mono): click rate via short-window crest factor.
"""

from __future__ import annotations

import math
import subprocess
from dataclasses import dataclass, field

import numpy as np

CHUNK_FRAMES = 96000  # 1 s of stereo f32 at a time


def _dbfs(x: float) -> float:
    return -120.0 if x <= 0 else max(-120.0, 20.0 * math.log10(x))


@dataclass
class StereoStats:
    sample_rate: int
    frames: int = 0
    peak: float = 0.0
    clipped: int = 0
    dc_sum: np.ndarray = field(default_factory=lambda: np.zeros(2, dtype=np.float64))
    sq_sum: np.ndarray = field(default_factory=lambda: np.zeros(2, dtype=np.float64))
    lr_prod_sum: float = 0.0
    envelope_100ms: list[float] = field(default_factory=list)

    def feed(self, block: np.ndarray) -> None:  # block: (n, 2) float32
        self.frames += len(block)
        absb = np.abs(block)
        self.peak = max(self.peak, float(absb.max(initial=0.0)))
        # 24-bit full scale minus ~256 counts, in float terms
        self.clipped += int((absb >= (1.0 - 2**-16)).sum())
        self.dc_sum += block.sum(axis=0)
        self.sq_sum += (block.astype(np.float64) ** 2).sum(axis=0)
        self.lr_prod_sum += float((block[:, 0].astype(np.float64) * block[:, 1]).sum())
        hop = self.sample_rate // 10
        for i in range(0, len(block) - hop + 1, hop):
            self.envelope_100ms.append(float(absb[i : i + hop].max()))

    def summary(self) -> dict:
        n = max(self.frames, 1)
        rms = np.sqrt(self.sq_sum / n)
        dc = self.dc_sum / n
        denom = float(rms[0] * rms[1]) * n
        phase_corr = self.lr_prod_sum / denom if denom > 0 else 0.0
        return {
            "duration_sec": round(self.frames / self.sample_rate, 2),
            "peak_dbfs": round(_dbfs(self.peak), 2),
            "clipped_samples": self.clipped,
            "dc_offset": [round(float(d), 6) for d in dc],
            "rms_dbfs": [round(_dbfs(float(r)), 2) for r in rms],
            "channel_imbalance_db": round(
                abs(_dbfs(float(rms[0])) - _dbfs(float(rms[1]))), 2
            ),
            "phase_correlation": round(phase_corr, 3),
        }


def decode_stereo(path: str, sample_rate: int, stats: StereoStats) -> None:
    proc = subprocess.Popen(
        ["ffmpeg", "-v", "quiet", "-i", path, "-f", "f32le", "-ac", "2", "-"],
        stdout=subprocess.PIPE,
    )
    nbytes = CHUNK_FRAMES * 2 * 4
    while True:
        raw = proc.stdout.read(nbytes)
        if not raw:
            break
        block = np.frombuffer(raw[: len(raw) - len(raw) % 8], dtype=np.float32)
        stats.feed(block.reshape(-1, 2))
    proc.wait()


def click_rate(path: str, duration_sec: float) -> dict:
    """Clicks/minute: 3 kHz high-pass, 5 ms window crest factor.

    A click is a broadband transient; after removing everything the music
    mostly lives in, a 5 ms window whose peak is far above the running HF
    floor is a surface fault.
    """
    proc = subprocess.Popen(
        ["ffmpeg", "-v", "quiet", "-i", path, "-af", "highpass=f=3000",
         "-f", "f32le", "-ac", "1", "-ar", "48000", "-"],
        stdout=subprocess.PIPE,
    )
    win = 240  # 5 ms at 48k
    clicks = 0
    floors: list[float] = []
    carry = np.empty(0, dtype=np.float32)
    while True:
        raw = proc.stdout.read(48000 * 4)
        if not raw:
            break
        x = np.concatenate([carry, np.frombuffer(raw, dtype=np.float32)])
        usable = len(x) - len(x) % win
        carry = x[usable:]
        if not usable:
            continue
        frames = np.abs(x[:usable].reshape(-1, win))
        peaks = frames.max(axis=1)
        rms_floor = float(np.sqrt((frames**2).mean()))
        floors.append(rms_floor)
        running_floor = float(np.median(floors[-60:]))
        if running_floor > 0:
            clicks += int((peaks > max(10 * running_floor, 0.003)).sum())
    proc.wait()
    minutes = max(duration_sec / 60, 1e-6)
    return {"clicks": clicks, "clicks_per_min": round(clicks / minutes, 1)}


def music_bounds(envelope_100ms: list[float], level: float = 0.05) -> tuple[float, float]:
    """(onset_sec, end_sec) of music via 3 s smoothed mean over the envelope."""
    env = np.asarray(envelope_100ms, dtype=np.float64)
    if len(env) < 60:
        return 0.0, len(env) / 10
    smooth = np.convolve(env, np.ones(30) / 30, mode="same")
    above = np.nonzero(smooth > level)[0]
    if len(above) == 0:
        return 0.0, len(env) / 10
    return round(above[0] / 10, 1), round(above[-1] / 10, 1)


def lead_in_noise_floor(envelope_100ms: list[float], onset_sec: float) -> float | None:
    """RMS-ish floor of the pre-music groove: the cross-collection 'how
    trashed is this disc' number. None when the needle dropped straight in."""
    onset = int(onset_sec * 10)
    lead = envelope_100ms[5 : max(onset - 5, 0)]
    if len(lead) < 20:  # under 2 s of lead-in: not measurable
        return None
    return round(_dbfs(float(np.median(np.asarray(lead)))), 2)


def dropouts(envelope_100ms: list[float], onset_sec: float, end_sec: float) -> list[float]:
    """Sudden near-silence inside the music region (>=0.5 s below -60 dBFS)."""
    env = np.asarray(envelope_100ms[int(onset_sec * 10) : int(end_sec * 10)])
    quiet = env < 0.001
    out, run = [], 0
    for i, q in enumerate(quiet):
        run = run + 1 if q else 0
        if run == 5:
            out.append(round(onset_sec + (i - 4) / 10, 1))
    return out
