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
import subprocess
import threading
from pathlib import Path

import boto3
from boto3.s3.transfer import TransferConfig
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
            response_checksum_validation="when_required",
            request_checksum_calculation="when_required",
        ),
    )


def flac_verify(path: Path) -> bool:
    """Full decode test. Result is cached as a sibling marker so a file that
    fails upload three times is only decode-verified once."""
    marker = path.with_suffix(".verified")
    if marker.exists():
        return True
    ok = subprocess.run(
        ["flac", "-t", "-s", str(path)], capture_output=True
    ).returncode == 0
    if ok:
        marker.touch()
    return ok


class Uploader:
    def __init__(self, settings: Settings, api: VinylApi, station) -> None:
        self.settings = settings
        self.api = api
        self.station = station
        self._s3 = None
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
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
            # Multipart with per-part retry: a network blip costs one 16 MB
            # part, not the whole half-gigabyte master.
            self._s3.upload_file(
                str(path), self.settings.s3_bucket, key,
                ExtraArgs={"ContentType": "audio/flac"},
                Config=TransferConfig(
                    multipart_threshold=32 * 1024 * 1024,
                    multipart_chunksize=16 * 1024 * 1024,
                ),
            )
            self.api.mark_uploaded(capture_id, self.settings.s3_bucket, key)
        except Exception as exc:
            with contextlib.suppress(ApiError):
                self.api.report_upload_error(capture_id, str(exc))
            log.warning("upload of %s failed (%s); will retry", path.name, exc)
            return

        path.unlink(missing_ok=True)
        path.with_suffix(".verified").unlink(missing_ok=True)
        log.info("uploaded %s -> %s", path.name, key)

    def stop(self) -> None:
        self._stop.set()
