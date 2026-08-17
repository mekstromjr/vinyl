"""QC verdict logic + metric passes over synthetic audio."""

from __future__ import annotations

import math
import shutil
import struct
import wave

import pytest
from vinyl_server.kjob import qc_job_manifest
from vinyl_server.qc.verdict import THRESHOLDS, judge, loop_scan

HAVE_FFMPEG = shutil.which("ffmpeg") is not None

CLEAN_SUMMARY = {
    "duration_sec": 1300.0, "peak_dbfs": -6.5, "clipped_samples": 0,
    "dc_offset": [0.0, 0.0], "rms_dbfs": [-18.0, -18.5],
    "channel_imbalance_db": 0.5, "phase_correlation": 0.4,
}
NO_LOOP = {"scanned": True, "loop_at_sec": None, "mid_record_loop": False}


def _judge(**over):
    args = dict(
        summary=CLEAN_SUMMARY, clicks={"clicks": 10, "clicks_per_min": 0.5},
        noise_floor_dbfs=-52.0, dropout_starts=[], loop=NO_LOOP,
        expected_runtime_sec=1255, music_end_sec=1250.0,
    )
    args.update(over)
    return judge(**args)


def test_clean_side_passes():
    r = _judge()
    assert r.verdict == "pass" and not r.reasons


def test_mid_record_loop_is_recapture():
    r = _judge(loop={"scanned": True, "loop_at_sec": 161.0, "mid_record_loop": True})
    assert r.verdict == "recapture"
    assert "2:41" in r.reasons[0]


def test_short_music_is_recapture():
    r = _judge(music_end_sec=900.0)
    assert r.verdict == "recapture"


def test_vintage_surface_noise_passes_v2():
    """Michael's ears calibrated v2: 90-120 cpm and -28 dBFS lead-ins are
    fine archive records, not review cases."""
    r = _judge(noise_floor_dbfs=-28.0, clicks={"clicks": 2000, "clicks_per_min": 119.0})
    assert r.verdict == "pass"


def test_truly_trashed_disc_still_reviews():
    r = _judge(noise_floor_dbfs=-18.0, clicks={"clicks": 5000, "clicks_per_min": 200.0})
    assert r.verdict == "review"
    assert "heavy surface damage" in "; ".join(r.reasons)


def test_dropouts_still_review():
    r = _judge(dropout_starts=[301.2])
    assert r.verdict == "review"


def test_mono_candidate_noted_not_verdicted():
    r = _judge(summary=dict(CLEAN_SUMMARY, phase_correlation=0.97))
    assert r.verdict == "pass"
    assert r.report["mono_fold_candidate"] is True


def test_report_json_serializable_with_numpy_inputs():
    """The loop-scan sides all crashed on np.bool_/np.float64 in the JSONB
    blob; every report must round-trip through json with numpy inputs."""
    import json

    import numpy as np

    r = _judge(
        music_end_sec=np.float64(1250.0),
        loop={"scanned": True, "loop_at_sec": 1240.0,
              "mid_record_loop": np.bool_(False)},
    )
    json.dumps(r.report, default=lambda o: o.item() if hasattr(o, "item") else str(o))
    assert isinstance(r.report["mono_fold_candidate"], bool)
    assert "buy_commercial_candidate" not in r.report
    assert isinstance(r.report["music_end_sec"], float)


def test_reasons_persisted_in_report():
    """The phone notification is ephemeral; the WHY must live in the blob."""
    r = _judge(noise_floor_dbfs=-20.0)  # above the v2 -25 threshold
    assert r.report["reasons"] and "noise floor" in r.report["reasons"][0]


def test_report_carries_thresholds():
    assert _judge().report["thresholds"] == THRESHOLDS


def test_loop_scan_flags_mid_record_loop():
    rev = [0.2 + 0.1 * math.sin(i / 3) for i in range(18)]
    env = [0.3] * 60 + rev * 30 + [0.25] * 1200
    scan = loop_scan(env, 33, music_end_sec=len(env) / 10)
    assert scan["mid_record_loop"] is True


def test_loop_scan_runout_is_not_mid_record():
    rev = [0.01 + 0.005 * math.sin(i / 3) for i in range(18)]
    env = [0.3] * 1200 + rev * 30
    music_end = 120.0
    scan = loop_scan(env, 33, music_end_sec=music_end)
    assert scan["loop_at_sec"] is not None
    assert scan["mid_record_loop"] is False


def test_qc_job_manifest_shape():
    import uuid

    cid = uuid.uuid4()
    m = qc_job_manifest(cid, "registry.meklab.net/meklab/vinyl:v0.7.0", "prod")
    assert m["kind"] == "Job"
    spec = m["spec"]["template"]["spec"]
    assert spec["containers"][0]["command"][-1] == "vinyl_server.qc"
    envs = {e["name"] for e in spec["containers"][0]["env"]}
    assert {"CAPTURE_ID", "VINYL_DB_URL", "VINYL_S3_ACCESS_KEY"} <= envs
    assert m["spec"]["ttlSecondsAfterFinished"] == 86400


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")
def test_metrics_on_synthetic_wav(tmp_path):
    """Sine + lead-in noise + injected clicks through the real ffmpeg passes."""
    from vinyl_server.qc import metrics

    sr = 48000
    path = tmp_path / "side.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        frames = bytearray()
        for i in range(sr * 5):  # 5s quiet lead-in
            v = int(80 * math.sin(i))  # tiny noise
            frames += struct.pack("<hh", v, v)
        for i in range(sr * 20):  # 20s of tone at healthy level
            v = int(12000 * math.sin(2 * math.pi * 440 * i / sr))
            click = 30000 if i % (sr * 2) == 0 else 0  # a click every 2s
            frames += struct.pack("<hh", min(v + click, 32767), v)
        w.writeframes(bytes(frames))

    stats = metrics.StereoStats(sample_rate=sr)
    metrics.decode_stereo(str(path), sr, stats)
    s = stats.summary()
    assert 24.5 < s["duration_sec"] < 25.5
    assert s["phase_correlation"] > 0.95  # near-identical channels

    onset, end = metrics.music_bounds(stats.envelope_100ms)
    # centered 3s smoothing pulls the edge ~1.5s early by design
    assert 3.0 < onset < 6.5
    floor = metrics.lead_in_noise_floor(stats.envelope_100ms, onset)
    assert floor is not None and floor < -40

    clicks = metrics.click_rate(str(path), onset, end)
    assert clicks["clicks"] >= 5  # ~10 injected
    # a pure tone with no clicks must count ~zero (the first calibration
    # counted musical HF transients; local-median context must not)
    clean = tmp_path / "clean.wav"
    with wave.open(str(clean), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        frames = bytearray()
        for i in range(sr * 20):
            v = int(12000 * math.sin(2 * math.pi * 5000 * i / sr))
            frames += struct.pack("<hh", v, v)
        w.writeframes(bytes(frames))
    quiet = metrics.click_rate(str(clean), 0.0, 20.0)
    assert quiet["clicks_per_min"] < 5
