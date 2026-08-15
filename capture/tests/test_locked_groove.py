"""Locked-groove detector: the GM-0003-D1-B failure mode, field-tuned params."""

from __future__ import annotations

import math
import random

from vinyl_capture.analysis import ROTATION_PERIOD_SEC, LockedGrooveDetector, judge

MUSIC_PREFIX = [0.3] * 40  # 4s of loud signal to arm the music latch


def _det(**kw):
    return LockedGrooveDetector(period_sec=1.8, **kw)


def _feed_seq(det, values):
    t = 0.0
    for v in values:
        t += 0.1
        if det.feed(v, t):
            return t
    return None


def test_looping_envelope_triggers_after_music():
    det = _det()
    revolution = [0.2 + 0.1 * math.sin(i / 3) for i in range(18)]
    fired_at = _feed_seq(det, MUSIC_PREFIX + revolution * 16)
    assert fired_at is not None
    assert det.loop_started_at_sec is not None


def test_off_speed_platter_still_triggers():
    """Real platters drift; the pattern repeats at 17 buckets, not 18."""
    det = _det()
    revolution = [0.2 + 0.1 * math.sin(i / 2.5) for i in range(17)]
    assert _feed_seq(det, MUSIC_PREFIX + revolution * 18) is not None


def test_lead_in_groove_cannot_trigger_before_music():
    """A lead-in IS a locked groove; the music latch keeps it from firing."""
    det = _det()
    quiet_loop = [0.01 + 0.005 * math.sin(i / 3) for i in range(18)]
    assert _feed_seq(det, quiet_loop * 20) is None
    assert det.music_seen is False


def test_music_like_signal_does_not_trigger():
    rng = random.Random(42)
    det = _det()
    music = [0.1 + 0.4 * rng.random() for _ in range(18 * 20)]
    assert _feed_seq(det, MUSIC_PREFIX + music) is None


def test_silence_after_music_does_not_trigger():
    det = _det()
    assert _feed_seq(det, MUSIC_PREFIX + [0.00001] * 18 * 20) is None


def test_single_glitch_does_not_reset_the_vote():
    """The old consecutive counter died to one bad bucket; the vote survives."""
    det = _det()
    revolution = [0.2 + 0.1 * math.sin(i / 3) for i in range(18)]
    looped = (revolution * 16)
    looped[60] = 0.9  # one pop mid-loop
    assert _feed_seq(det, MUSIC_PREFIX + looped) is not None


def test_45_rpm_lags():
    det = LockedGrooveDetector(period_sec=ROTATION_PERIOD_SEC[45])
    assert det.lag == 13
    assert det.lags == (12, 13, 14)


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
    assert len(v.reasons) == 1
    assert "locked groove at ~2:41" in v.reasons[0]
    assert "clean the record" in v.reasons[0]


def test_judge_runout_stop_passes():
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
