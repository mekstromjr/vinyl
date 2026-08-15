"""Locked-groove detector: the GM-0003-D1-B failure mode."""

from __future__ import annotations

import math
import random

from vinyl_capture.analysis import ROTATION_PERIOD_SEC, LockedGrooveDetector, judge


def _det(**kw):
    return LockedGrooveDetector(period_sec=1.8, **kw)


def _feed_seq(det, values):
    t = 0.0
    for v in values:
        t += 0.1
        if det.feed(v, t):
            return t
    return None


def test_looping_envelope_triggers_after_ten_revolutions():
    det = _det()
    revolution = [0.2 + 0.1 * math.sin(i / 3) for i in range(18)]  # one 1.8s pattern
    fired_at = _feed_seq(det, revolution * 14)
    assert fired_at is not None
    # needs 2 revs of warmup + 10 matching revs ≈ 12 revolutions
    assert fired_at <= 13 * 1.8
    assert det.loop_started_at_sec is not None


def test_music_like_signal_does_not_trigger():
    rng = random.Random(42)
    det = _det()
    music = [0.1 + 0.4 * rng.random() for _ in range(18 * 20)]  # 20 "revolutions"
    assert _feed_seq(det, music) is None


def test_silence_does_not_trigger():
    det = _det()
    assert _feed_seq(det, [0.00001] * 18 * 20) is None


def test_slow_fade_flat_sections_do_not_trigger():
    det = _det()
    fade = [max(0.0005, 0.3 - i * 0.0004) for i in range(18 * 20)]
    # monotonic fade: consecutive windows differ slightly in mean; correlation
    # of near-flat unequal windows returns 0 via the flat-guard
    assert _feed_seq(det, fade) is None or det.revolutions < 12


def test_45_rpm_period():
    det = LockedGrooveDetector(period_sec=ROTATION_PERIOD_SEC[45])
    assert det.lag == 13  # 1.333s / 0.1s rounded


def test_judge_locked_groove_fails_with_cleaning_advice():
    v = judge(
        {"duration_sec": 300.0, "clipped_samples": 0, "dc_offset_l": 0.0,
         "dc_offset_r": 0.0, "peak_dbfs_l": -8, "peak_dbfs_r": -8,
         "envelope_1s": []},
        xruns=0,
        expected_runtime_sec=1255,
        locked_groove=True,
        loop_started_at_sec=161.0,
    )
    assert not v.passed
    assert len(v.reasons) == 1  # no stacked "stopped early" noise
    assert "locked groove at ~2:41" in v.reasons[0]
    assert "clean the record" in v.reasons[0]
    assert v.report["loop_started_at_sec"] == 161.0


def test_judge_runout_stop_passes():
    """A loop AT the expected end is the runout — capture succeeds."""
    v = judge(
        {"duration_sec": 1210.0, "clipped_samples": 0, "dc_offset_l": 0.0,
         "dc_offset_r": 0.0, "peak_dbfs_l": -8, "peak_dbfs_r": -8,
         "envelope_1s": []},
        xruns=0,
        expected_runtime_sec=1255,
        locked_groove=False,
        loop_started_at_sec=1205.0,
    )
    assert v.passed
