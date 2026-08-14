"""Analyzer + verdict tests against synthesized PCM — no hardware needed."""

from __future__ import annotations

import array
import math

from vinyl_capture.analysis import StreamStats, dbfs, judge, stop_after_sec

FULL = 2**31 - 1


def _pcm(samples_lr: list[tuple[int, int]]) -> bytes:
    flat = array.array("i")
    for left, right in samples_lr:
        flat.append(left)
        flat.append(right)
    return flat.tobytes()


def _sine(n: int, amplitude: float) -> list[tuple[int, int]]:
    a = int(amplitude * FULL)
    return [
        (int(a * math.sin(2 * math.pi * 440 * i / 96000)),) * 2 for i in range(n)
    ]


def test_peak_and_duration():
    st = StreamStats(sample_rate=96000)
    st.feed(_pcm(_sine(9600, 0.5)), now=0.0)
    s = st.summary()
    assert s["duration_sec"] == 0.1
    assert -6.5 < s["peak_dbfs_l"] < -5.5  # 0.5 linear = -6.02 dBFS
    assert s["clipped_samples"] == 0


def test_clipping_detected():
    st = StreamStats(sample_rate=96000)
    st.feed(_pcm([(FULL, FULL)] * 100 + _sine(1000, 0.3)), now=0.0)
    assert st.summary()["clipped_samples"] >= 200  # both channels


def test_dc_offset_detected():
    st = StreamStats(sample_rate=96000)
    offset = int(0.05 * FULL)
    st.feed(_pcm([(offset, offset)] * 9600), now=0.0)
    s = st.summary()
    assert abs(s["dc_offset_l"] - 0.05) < 0.001


def test_peak_hold_updates_and_decays():
    st = StreamStats(sample_rate=96000)
    st.feed(_pcm(_sine(960, 0.8)), now=0.0)
    high = st.hold_l
    st.feed(_pcm(_sine(960, 0.1)), now=1.0)
    assert st.hold_l == high  # held within HOLD_SEC
    st.feed(_pcm(_sine(960, 0.1)), now=5.0)
    assert st.hold_l < high  # decayed after HOLD_SEC


def test_dbfs_floor():
    assert dbfs(0.0) == -120.0
    assert abs(dbfs(1.0)) < 1e-9


def _summary(**over):
    base = {
        "duration_sec": 1200.0, "peak_dbfs_l": -6.0, "peak_dbfs_r": -6.2,
        "clipped_samples": 0, "dc_offset_l": 0.0001, "dc_offset_r": -0.0001,
    }
    return base | over


def test_verdict_passes_clean_capture():
    v = judge(_summary(), xruns=0, expected_runtime_sec=1165)
    assert v.passed


def test_verdict_fails_on_any_xrun():
    v = judge(_summary(), xruns=1, expected_runtime_sec=None)
    assert not v.passed
    assert "xrun" in v.reasons[0]


def test_verdict_fails_hot_gain_and_dc():
    v = judge(_summary(clipped_samples=5000), xruns=0, expected_runtime_sec=None)
    assert not v.passed and "gain too hot" in v.reasons[0]
    v = judge(_summary(dc_offset_l=0.02), xruns=0, expected_runtime_sec=None)
    assert not v.passed and "DC offset" in v.reasons[0]


def test_verdict_fails_short_capture_but_tolerates_overrun():
    v = judge(_summary(duration_sec=900), xruns=0, expected_runtime_sec=1165)
    assert not v.passed and "short of expected" in v.reasons[0]
    # Over-capture (runout groove tail) is fine by design.
    v = judge(_summary(duration_sec=1400), xruns=0, expected_runtime_sec=1165)
    assert v.passed


def test_stop_timer():
    kw = dict(buffer_sec=180, hard_cap_33=2100, hard_cap_45=900)
    assert stop_after_sec(1165, 33, **kw) == 1345
    assert stop_after_sec(None, 33, **kw) == 2100
    assert stop_after_sec(None, 45, **kw) == 900
    assert stop_after_sec(3000, 33, **kw) == 2100  # never past the hard cap
