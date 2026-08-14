"""Capture endpoint tests: the worker's contract with the database."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("VINYL_TEST_DB_URL") is None, reason="VINYL_TEST_DB_URL not set"
)


def _side(api, **overrides):
    body = {"collection": "GM", "disc_count": 1, "speed_rpm": 33} | overrides
    acc = api.post("/api/accessions", json=body).json()
    return f"{acc['accession_id']}-D1-A", acc


def _start(api, side_id):
    resp = api.post("/api/captures", json={"side_id": side_id})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_start_finish_upload_happy_path(api):
    side_id, _ = _side(api)
    cap = _start(api, side_id)
    assert cap["status"] == "recording"
    assert cap["is_current"] is True

    done = api.patch(
        f"/api/captures/{cap['id']}",
        json={
            "status": "captured",
            "integrity": {"xruns": 0, "clipped_samples": 0},
            "sha256": "a" * 64,
            "bytes": 12345,
            "sample_rate": 96000,
            "bit_depth": 24,
            "duration_sec": 1201.5,
        },
    )
    assert done.status_code == 200
    assert done.json()["status"] == "captured"

    up = api.post(
        f"/api/captures/{cap['id']}/uploaded",
        json={"s3_bucket": "vinyl", "s3_key": f"masters/{side_id}/{cap['id']}.flac"},
    )
    assert up.status_code == 200
    assert up.json()["status"] == "uploaded"

    grid = api.get(f"/api/accessions/{side_id[:7]}/sides").json()
    assert next(s for s in grid if s["side_id"] == side_id)["state"] == "qc_pending"


def test_retry_demotes_previous_current(api):
    side_id, _ = _side(api)
    first = _start(api, side_id)
    api.patch(f"/api/captures/{first['id']}",
              json={"status": "failed", "failure_reason": "xruns"})

    second = _start(api, side_id)
    assert second["supersedes"] == first["id"]
    assert second["is_current"] is True
    assert api.get(f"/api/captures/{first['id']}").json()["is_current"] is False


def test_cannot_start_while_recording(api):
    side_id, _ = _side(api)
    _start(api, side_id)
    resp = api.post("/api/captures", json={"side_id": side_id})
    assert resp.status_code == 409
    assert "already recording" in resp.text


def test_78_and_skipped_sides_refused(api):
    side_id, _ = _side(api, speed_rpm=78)
    resp = api.post("/api/captures", json={"side_id": side_id})
    assert resp.status_code == 409
    assert "phase two" in resp.text

    side_id2, acc = _side(api)
    api.patch(
        f"/api/accessions/{acc['accession_id']}/sides/{side_id2}",
        json={"skipped": True, "skip_reason": "cracked"},
    )
    assert api.post("/api/captures", json={"side_id": side_id2}).status_code == 409


def test_upload_error_is_a_counter_not_a_state(api):
    side_id, _ = _side(api)
    cap = _start(api, side_id)
    api.patch(f"/api/captures/{cap['id']}", json={"status": "captured"})

    for n in (1, 2):
        r = api.post(f"/api/captures/{cap['id']}/upload-error",
                     json={"error": "connection refused"})
        assert r.json()["upload_attempts"] == n
        assert r.json()["status"] == "captured"  # never demoted


def test_illegal_transitions_409(api):
    side_id, _ = _side(api)
    cap = _start(api, side_id)
    # recording -> uploaded skips captured
    up = api.post(
        f"/api/captures/{cap['id']}/uploaded",
        json={"s3_bucket": "b", "s3_key": "k"},
    )
    assert up.status_code == 409
    # captured -> failed is forbidden (upload trouble must not demote)
    api.patch(f"/api/captures/{cap['id']}", json={"status": "captured"})
    resp = api.patch(f"/api/captures/{cap['id']}", json={"status": "failed"})
    assert resp.status_code == 409


def test_worker_recovery_list(api):
    side_id, _ = _side(api)
    cap = _start(api, side_id)
    api.patch(f"/api/captures/{cap['id']}", json={"status": "captured"})
    pending = api.get("/api/captures", params={"status": "captured"}).json()
    assert [c["id"] for c in pending] == [cap["id"]]
