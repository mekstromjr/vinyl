"""QC Job entrypoint: python -m vinyl_server.qc  (CAPTURE_ID from env).

Downloads the master, runs the metric passes, writes verdict + promoted
columns + envelope backfill to the capture row, notifies HA on non-pass.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import uuid

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from vinyl_server.db.models import Capture, DiscSide
from vinyl_server.qc import metrics
from vinyl_server.qc.verdict import judge, loop_scan
from vinyl_server.s3 import make_client

log = logging.getLogger("qc")


def run(capture_id: uuid.UUID) -> str:
    engine = create_engine(os.environ["VINYL_DB_URL"])
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as session:
        cap = session.get(Capture, capture_id)
        if cap is None or cap.s3_key is None:
            log.error("capture %s missing or not uploaded", capture_id)
            return "error"
        side = session.get(DiscSide, cap.side_id)

        path = "/scratch/master.flac"
        make_client().download_file(cap.s3_bucket, cap.s3_key, path)
        log.info("downloaded %s (%s bytes)", cap.s3_key, os.path.getsize(path))

        stats = metrics.StereoStats(sample_rate=cap.sample_rate or 96000)
        metrics.decode_stereo(path, stats.sample_rate, stats)
        summary = stats.summary()
        onset, music_end = metrics.music_bounds(stats.envelope_100ms)
        noise_floor = metrics.lead_in_noise_floor(stats.envelope_100ms, onset)
        drops = metrics.dropouts(stats.envelope_100ms, onset, music_end)
        clicks = metrics.click_rate(path, onset, music_end)
        loop = loop_scan(stats.envelope_100ms, int(side.speed_rpm), music_end)

        result = judge(
            summary=summary,
            clicks=clicks,
            noise_floor_dbfs=noise_floor,
            dropout_starts=drops,
            loop=loop,
            expected_runtime_sec=side.expected_runtime_sec,
            music_end_sec=music_end,
        )

        import json as _json

        cap.qc_verdict = result.verdict
        # round-trip through json with numpy-scalar coercion: a leaked np type
        # must degrade to its value, never fail the whole job at commit
        cap.qc = _json.loads(_json.dumps(
            result.report,
            default=lambda o: o.item() if hasattr(o, "item") else str(o),
        ))
        cap.lead_in_noise_floor_dbfs = noise_floor
        cap.clicks_per_min = clicks["clicks_per_min"]
        cap.peak_dbfs = summary["peak_dbfs"]
        if side.expected_runtime_sec:
            cap.duration_delta_sec = float(round(music_end - side.expected_runtime_sec, 1))

        # Envelope backfill (1 s peaks from the 100 ms pass) for captures that
        # predate live envelopes — the waveform appears retroactively.
        integrity = dict(cap.integrity or {})
        if not integrity.get("envelope_1s"):
            env = stats.envelope_100ms
            integrity["envelope_1s"] = [
                round(max(env[i : i + 10]), 4) for i in range(0, len(env) - 9, 10)
            ]
            integrity["envelope_backfilled"] = True
            cap.integrity = integrity

        session.commit()
        log.info("verdict %s: %s", result.verdict, "; ".join(result.reasons) or "clean")

        if result.verdict != "pass":
            _notify(cap.side_id, result)
        return result.verdict


def _notify(side_id: str, result) -> None:
    url, token, service = (
        os.environ.get("VINYL_HA_URL"),
        os.environ.get("VINYL_HA_TOKEN"),
        os.environ.get("VINYL_HA_NOTIFY_SERVICE"),
    )
    if not (url and token and service):
        return
    flag = " Candidate for buying a commercial copy." if result.buy_commercial else ""
    with contextlib.suppress(httpx.HTTPError):
        httpx.post(
            f"{url}/api/services/notify/{service}",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "title": f"{side_id} QC: {result.verdict}",
                "message": "; ".join(result.reasons) + flag,
            },
            timeout=10,
        )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s qc %(message)s")
    verdict = run(uuid.UUID(os.environ["CAPTURE_ID"]))
    sys.exit(0 if verdict in ("pass", "review", "recapture") else 1)


if __name__ == "__main__":
    main()
