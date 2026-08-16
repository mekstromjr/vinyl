"""Staging-drain uploader: self-healing, never runs during a capture.

Filenames carry the join key (``<side_id>__<capture_id>.flac``), the cluster
row carries the truth. For each staged file: deep-verify once (``flac -t`` —
deliberately deferred out of the retry-or-flip hot path), upload to Garage,
mark the row, delete the file. Any failure leaves the file for the next pass;
the cluster's upload_attempts counter is the visible heartbeat of retries.
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from pathlib import Path

import boto3
from botocore.config import Config

from vinyl_capture.apiclient import ApiError, VinylApi
from vinyl_capture.config import Settings

log = logging.getLogger(__name__)


def make_s3(settings: Settings):
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        # when_required: botocore's flexible-checksum validation (1.36+)
        # aborts reads of multipart objects on Garage — its composite checksum
        # headers don't match botocore's expectation. Integrity is covered by
        # our own end-to-end sha256, recorded at capture time.
        config=Config(
            s3={"addressing_style": "path"},
            # explicit, short: the default socket behavior left a sendall
            # blocked for SIX HOURS on a stalled connection (py-spy showed
            # the uploader thread wedged in ssl.py send)
            connect_timeout=10,
            read_timeout=60,
            retries={"mode": "standard", "max_attempts": 2},
            response_checksum_validation="when_required",
            request_checksum_calculation="when_required",
        ),
    )


def flac_verify(path: Path) -> bool:
    """Full decode test. Result is cached as a sibling marker so a file that
    fails upload three times is only decode-verified once.

    nice+ionice: decoding 600MB on a Core 2 Duo starved the RECORDER once
    (xrun during GM-0010-D1-A) when a capture was armed mid-verify."""
    marker = path.with_suffix(".verified")
    if marker.exists():
        return True
    ok = subprocess.run(
        ["nice", "-n", "19", "ionice", "-c", "3", "flac", "-t", "-s", str(path)],
        capture_output=True,
    ).returncode == 0
    if ok:
        marker.touch()
    return ok


class Uploader:
    #: A part that makes no progress for this long is declared wedged; the
    #: client is torn down (which unblocks the stuck socket) and rebuilt.
    PART_DEADLINE_SEC = 180

    def __init__(self, settings: Settings, api: VinylApi, station) -> None:
        self.settings = settings
        self.api = api
        self.station = station
        self._s3 = None
        self._stop = threading.Event()
        self._part_pool = ThreadPoolExecutor(max_workers=1)
        #: Live status for the kiosk: what is uploading, how far along.
        self.current: dict | None = None
        self.last_error: str | None = None
        self.last_success_at: float | None = None

    def status(self) -> dict:
        staged = sorted(f.name for f in self.settings.staging_dir.glob("*.flac"))
        return {
            "staged": len(staged),
            "staged_files": staged[:20],
            "current": self.current,
            "last_error": self.last_error,
            "last_success_at": self.last_success_at,
        }

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        # Lowest CPU priority for everything this thread does: recording
        # always wins the two cores.
        with contextlib.suppress(OSError):
            os.setpriority(os.PRIO_PROCESS, 0, 19)
        while not self._stop.wait(10):
            if self.station.phase != "idle":
                continue  # NEVER touch the disk while a capture might be running
            try:
                self._drain()
            except Exception:
                log.exception("upload pass failed; will retry")

    def _drain(self) -> None:
        for path in sorted(self.settings.staging_dir.glob("*.flac")):
            if self.station.phase != "idle":
                return
            self._handle(path)

    def _handle(self, path: Path) -> None:
        try:
            side_id, capture_id = path.stem.split("__", 1)
        except ValueError:
            log.warning("unrecognized staging file %s", path.name)
            return

        try:
            row = self.api.capture(capture_id)
        except ApiError as exc:
            log.info("cluster unreachable (%s); leaving %s", exc, path.name)
            return

        if row["status"] == "uploaded":
            # Crash after upload, before local delete: finish the cleanup.
            path.unlink(missing_ok=True)
            path.with_suffix(".verified").unlink(missing_ok=True)
            return
        if row["status"] != "captured":
            log.warning("%s is %s; leaving file for manual review", capture_id, row["status"])
            return

        if not flac_verify(path):
            with contextlib.suppress(ApiError):
                self.api.report_upload_error(
                    capture_id, "flac -t failed: file is corrupt on disk"
                )
            log.error("%s failed flac -t; NOT uploading", path.name)
            return

        key = f"masters/{side_id}/{capture_id}.flac"
        if self._s3 is None:
            self._s3 = make_s3(self.settings)
        try:
            # Manual multipart, 16 MB parts, PHASE-GATED BETWEEN PARTS: an arm
            # mid-upload stops us within one part (~seconds). boto3's
            # upload_file could not be interrupted, and a 600 MB transfer
            # during a capture starved the recorder into an xrun.
            if not self._upload_interruptible(path, key):
                log.info("capture armed mid-upload; %s deferred", path.name)
                return
            self.api.mark_uploaded(capture_id, self.settings.s3_bucket, key)
        except Exception as exc:
            with contextlib.suppress(ApiError):
                self.api.report_upload_error(capture_id, str(exc))
            log.warning("upload of %s failed (%s); will retry", path.name, exc)
            return

        path.unlink(missing_ok=True)
        path.with_suffix(".verified").unlink(missing_ok=True)
        log.info("uploaded %s -> %s", path.name, key)

    def _upload_interruptible(self, path: Path, key: str) -> bool:
        """True = completed; False = yielded to a capture (file retained).

        Every part runs under a hard deadline: if the socket wedges (observed:
        sendall blocked six hours on a stalled connection), the client is
        closed — which raises inside the stuck call — and rebuilt, the MPU is
        abandoned, and the file waits for the next pass."""
        bucket = self.settings.s3_bucket
        part_size = 16 * 1024 * 1024
        total_parts = max(1, (path.stat().st_size + part_size - 1) // part_size)
        mpu = self._s3.create_multipart_upload(
            Bucket=bucket, Key=key, ContentType="audio/flac"
        )
        upload_id = mpu["UploadId"]
        parts = []
        try:
            with path.open("rb") as f:
                n = 1
                while chunk := f.read(part_size):
                    if self.station.phase != "idle":
                        self._s3.abort_multipart_upload(
                            Bucket=bucket, Key=key, UploadId=upload_id
                        )
                        self.current = None
                        return False
                    self.current = {
                        "file": path.name, "part": n, "parts_total": total_parts
                    }
                    future = self._part_pool.submit(
                        self._s3.upload_part,
                        Bucket=bucket, Key=key, UploadId=upload_id,
                        PartNumber=n, Body=chunk,
                    )
                    try:
                        resp = future.result(timeout=self.PART_DEADLINE_SEC)
                    except FuturesTimeout:
                        # Unblock the wedged socket by tearing the client down,
                        # then rebuild for the abort + future passes.
                        with contextlib.suppress(Exception):
                            self._s3.close()
                        self._s3 = make_s3(self.settings)
                        self._part_pool.shutdown(wait=False, cancel_futures=True)
                        self._part_pool = ThreadPoolExecutor(max_workers=1)
                        raise TimeoutError(
                            f"part {n}/{total_parts} made no progress for "
                            f"{self.PART_DEADLINE_SEC}s — connection wedged"
                        ) from None
                    parts.append({"PartNumber": n, "ETag": resp["ETag"]})
                    n += 1
            self._s3.complete_multipart_upload(
                Bucket=bucket, Key=key, UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
            self.current = None
            import time as _time

            self.last_success_at = _time.time()
            self.last_error = None
            return True
        except Exception as exc:
            self.current = None
            self.last_error = str(exc)[:200]
            with contextlib.suppress(Exception):
                self._s3.abort_multipart_upload(
                    Bucket=bucket, Key=key, UploadId=upload_id
                )
            raise

    def stop(self) -> None:
        self._stop.set()
