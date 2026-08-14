"""Uploader drain rules with a fake API and fake S3 — the self-healing contract."""

from __future__ import annotations

import pytest
from vinyl_capture.uploader import Uploader


class FakeApi:
    def __init__(self, rows):
        self.rows = rows
        self.uploaded = []
        self.errors = []

    def capture(self, cid):
        return self.rows[cid]

    def mark_uploaded(self, cid, bucket, key):
        self.uploaded.append((cid, bucket, key))
        self.rows[cid]["status"] = "uploaded"

    def report_upload_error(self, cid, error):
        self.errors.append((cid, error))


class FakeS3:
    def __init__(self, fail=False):
        self.objects = {}
        self.fail = fail

    def put_object(self, Bucket, Key, Body, ContentType=None):
        if self.fail:
            raise RuntimeError("garage unreachable")
        self.objects[Key] = Body.read()


class FakeStation:
    phase = "idle"


class Cfg:
    s3_bucket = "vinyl"

    def __init__(self, staging):
        self.staging_dir = staging


@pytest.fixture
def stage(tmp_path, monkeypatch):
    # flac -t always passes in these tests; the corrupt case overrides this.
    monkeypatch.setattr("vinyl_capture.uploader.flac_verify", lambda p: True)

    def make(cid="c1", status="captured"):
        f = tmp_path / f"GM-0001-D1-A__{cid}.flac"
        f.write_bytes(b"flacdata")
        api = FakeApi({cid: {"status": status}})
        up = Uploader(Cfg(tmp_path), api, FakeStation())
        up._s3 = FakeS3()
        return f, api, up

    return make


def test_happy_path_uploads_and_cleans(stage):
    f, api, up = stage()
    up._drain()
    assert api.uploaded == [("c1", "vinyl", "masters/GM-0001-D1-A/c1.flac")]
    assert not f.exists()


def test_already_uploaded_row_just_cleans_local(stage):
    """Crash between mark_uploaded and local delete self-heals."""
    f, api, up = stage(status="uploaded")
    up._drain()
    assert api.uploaded == []
    assert not f.exists()


def test_failed_row_leaves_file_alone(stage):
    f, api, up = stage(status="failed")
    up._drain()
    assert f.exists()  # manual-review territory, never silently discarded


def test_s3_failure_reports_and_retains(stage):
    f, api, up = stage()
    up._s3 = FakeS3(fail=True)
    up._drain()
    assert f.exists()
    assert api.errors and "garage unreachable" in api.errors[0][1]
    assert api.uploaded == []


def test_corrupt_flac_never_uploads(stage, monkeypatch):
    f, api, up = stage()
    monkeypatch.setattr("vinyl_capture.uploader.flac_verify", lambda p: False)
    up._drain()
    assert f.exists()
    assert api.uploaded == []
    assert "corrupt" in api.errors[0][1]


def test_never_touches_disk_while_recording(stage):
    f, api, up = stage()
    up.station.phase = "recording"
    up._drain()
    assert f.exists()
    assert api.uploaded == []


def test_unrecognized_files_left_alone(tmp_path):
    stray = tmp_path / "not-a-capture.flac"
    stray.write_bytes(b"x")
    up = Uploader(Cfg(tmp_path), FakeApi({}), FakeStation())
    up._drain()
    assert stray.exists()
