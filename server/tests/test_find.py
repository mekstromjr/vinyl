"""Home search + sides size column."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("VINYL_TEST_DB_URL") is None, reason="VINYL_TEST_DB_URL not set"
)

RELEASE_ID = 249504


def _bound_accession(api):
    acc = api.post(
        "/api/accessions",
        json={"collection": "GM", "disc_count": 1, "speed_rpm": 33,
              "condition_notes": "seam split on jacket"},
    ).json()
    api.post(
        f"/api/accessions/{acc['accession_id']}/bind",
        json={"discogs_release_id": RELEASE_ID},
    )
    return acc["accession_id"]


@pytest.mark.parametrize("query", [
    "GM-0001",        # accession id
    "bill evans",     # artist, case-insensitive
    "waltz",          # title
    "riverside",      # label
    "RLP 9399",       # catalog number
    "seam split",     # condition notes
    "1962",           # year
])
def test_find_matches_every_human_field(api, query):
    acc_id = _bound_accession(api)
    resp = api.get("/find", params={"q": query})
    assert resp.status_code == 200
    assert acc_id in resp.text, f"query {query!r} missed"


def test_find_no_match_and_empty(api):
    _bound_accession(api)
    assert "Nothing matches" in api.get("/find", params={"q": "zeppelin"}).text
    assert api.get("/find", params={"q": ""}).text.strip() == ""


def test_home_has_search_box(api):
    resp = api.get("/")
    assert 'hx-get="/find"' in resp.text


def test_sides_table_shows_size_once_captured(api):
    acc_id = _bound_accession(api)
    side_id = f"{acc_id}-D1-A"
    cap = api.post("/api/captures", json={"side_id": side_id}).json()
    api.patch(
        f"/api/captures/{cap['id']}",
        json={"status": "captured", "bytes": 486889419},
    )
    page = api.get(f"/accessions/{acc_id}")
    assert "464 MB" in page.text  # 486889419 / 1048576 rounded
    # API surface carries it too
    grid = api.get(f"/api/accessions/{acc_id}/sides").json()
    assert next(s for s in grid if s["side_id"] == side_id)["bytes"] == 486889419


def test_clear_expected_runtime_flag(api):
    """The truncation self-heal path: a wrong Discogs runtime gets cleared so
    the retry runs open-ended."""
    acc_id = _bound_accession(api)
    side_id = f"{acc_id}-D1-A"
    grid = api.get(f"/api/accessions/{acc_id}/sides").json()
    assert next(s for s in grid if s["side_id"] == side_id)["expected_runtime_sec"]

    resp = api.patch(
        f"/api/accessions/{acc_id}/sides/{side_id}",
        json={"clear_expected_runtime": True},
    )
    assert resp.status_code == 200
    assert resp.json()["expected_runtime_sec"] is None


def test_task_list_surfaces_failed_capture(api):
    """GM-0009 failed silently; the task list must shout it."""
    acc_id = _bound_accession(api)
    side_id = f"{acc_id}-D1-A"
    cap = api.post("/api/captures", json={"side_id": side_id}).json()
    api.patch(f"/api/captures/{cap['id']}",
              json={"status": "failed", "failure_reason": "1 ALSA xrun(s)"})

    resp = api.get("/tasks")
    assert "recapture" in resp.text
    assert side_id in resp.text
    assert "xrun" in resp.text
    # the untouched side shows as a capture task
    assert f"{acc_id}-D1-B" in resp.text

    only_recapture = api.get("/tasks", params={"type": "recapture"})
    assert side_id in only_recapture.text
    assert f"{acc_id}-D1-B" not in only_recapture.text


def test_task_list_identify_and_review(api):
    unbound = api.post("/api/accessions",
                       json={"collection": "GM", "disc_count": 1, "speed_rpm": 33}).json()
    resp = api.get("/tasks", params={"type": "identify"})
    assert unbound["accession_id"] in resp.text
    assert "bind a Discogs release" in resp.text

    bound = _bound_accession(api)
    side_id = f"{bound}-D1-A"
    cap = api.post("/api/captures", json={"side_id": side_id}).json()
    api.patch(f"/api/captures/{cap['id']}", json={"status": "captured", "bytes": 10})
    api.fake_s3.objects["k"] = b"x"
    api.post(f"/api/captures/{cap['id']}/uploaded",
             json={"s3_bucket": "vinyl-test", "s3_key": "k"})
    import uuid as _uuid

    from vinyl_server.db.models import Capture
    from vinyl_server.db.session import get_session
    gen = api.app.dependency_overrides[get_session]()
    session = next(gen)
    c = session.get(Capture, _uuid.UUID(cap["id"]))
    c.qc_verdict = "review"
    c.qc = {"reasons": ["203.6 clicks/min — heavy surface damage"]}
    session.commit()

    resp = api.get("/tasks", params={"type": "review"})
    assert side_id in resp.text
    assert "heavy surface damage" in resp.text


def test_home_hosts_task_list_not_backlog(api):
    home = api.get("/")
    assert 'hx-get="/tasks"' in home.text
    assert "Uncaptured backlog" not in home.text


def test_task_list_shows_uploading(api):
    """12 masters sat on the laptop for 6 hours with nobody the wiser; a
    captured-not-uploaded side is now a visible task."""
    acc_id = _bound_accession(api)
    side_id = f"{acc_id}-D1-A"
    cap = api.post("/api/captures", json={"side_id": side_id}).json()
    api.patch(f"/api/captures/{cap['id']}", json={"status": "captured", "bytes": 10})
    api.post(f"/api/captures/{cap['id']}/upload-error", json={"error": "sendall wedged"})

    resp = api.get("/tasks", params={"type": "uploading"})
    assert side_id in resp.text
    assert "waiting to upload" in resp.text
    assert "sendall wedged" in resp.text
