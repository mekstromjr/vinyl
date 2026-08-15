from __future__ import annotations

import array

from vinyl_capture.analysis import StreamStats


def _chunk(value: int, frames: int) -> bytes:
    return array.array("i", [value, -value] * frames).tobytes()


def test_envelope_one_peak_per_second():
    stats = StreamStats(sample_rate=10)  # tiny rate keeps the test readable
    stats.feed(_chunk(2**28, 10), now=0.0)   # second 1: quiet-ish
    stats.feed(_chunk(2**30, 10), now=1.0)   # second 2: loud
    summary = stats.summary()
    env = summary["envelope_1s"]
    assert len(env) == 2
    assert env[1] > env[0] > 0


def test_envelope_partial_second_included_in_summary():
    stats = StreamStats(sample_rate=10)
    stats.feed(_chunk(2**29, 15), now=0.0)  # 1.5 seconds
    env = stats.summary()["envelope_1s"]
    assert len(env) == 2  # full second + trailing partial
