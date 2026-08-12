"""End-to-end API tests against a real Postgres (see conftest for why)."""

from __future__ import annotations

import io
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("VINYL_TEST_DB_URL") is None, reason="VINYL_TEST_DB_URL not set"
)

RELEASE_ID = 249504


def _create(api, **overrides):
    body = {"collection": "GM", "disc_count": 1, "speed_rpm": 33} | overrides
    resp = api.post("/api/accessions", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_api_requires_key(api):
    assert api.get("/api/accessions", headers={"X-API-Key": "wrong"}).status_code == 401
    resp = api.get("/api/accessions", headers={"X-API-Key": ""})
    assert resp.status_code == 401
    assert api.get("/healthz").status_code == 200  # probes stay open


def test_create_allocates_sequential_ids_and_sides(api):
    first = _create(api)
    second = _create(api, disc_count=2)
    assert first["accession_id"] == "GM-0001"
    assert second["accession_id"] == "GM-0002"

    grid = api.get(f"/api/accessions/{second['accession_id']}/sides").json()
    assert [s["side_id"] for s in grid] == [
        "GM-0002-D1-A", "GM-0002-D1-B", "GM-0002-D2-A", "GM-0002-D2-B",
    ]
    assert all(s["state"] == "pending" for s in grid)


def test_lookup_is_case_insensitive(api):
    """Barcode scanners and humans both get to be sloppy."""
    _create(api)
    assert api.get("/api/accessions/gm-0001").status_code == 200


def test_bind_release(api):
    acc = _create(api)
    resp = api.post(
        f"/api/accessions/{acc['accession_id']}/bind",
        json={"discogs_release_id": RELEASE_ID},
    )
    assert resp.status_code == 200, resp.text
    bound = resp.json()
    assert bound["status"] == "identified"
    assert bound["release"]["artist"] == "Bill Evans Trio"
    assert bound["release"]["catalog_number"] == "RLP 9399"

    grid = api.get(f"/api/accessions/{acc['accession_id']}/sides").json()
    a_side = next(s for s in grid if s["side_id"].endswith("D1-A"))
    assert a_side["expected_runtime_sec"] == (4 * 60 + 56) + (6 * 60 + 54)


def test_bind_twice_reuses_release_row(api):
    """Duplicate copies are ordinary in an inherited collection."""
    a = _create(api)
    b = _create(api)
    for acc in (a, b):
        assert api.post(
            f"/api/accessions/{acc['accession_id']}/bind",
            json={"discogs_release_id": RELEASE_ID},
        ).status_code == 200


def test_identified_requires_bind(api):
    acc = _create(api)
    resp = api.patch(f"/api/accessions/{acc['accession_id']}", json={"status": "identified"})
    assert resp.status_code == 422


def test_disc_count_grow_and_illegal_shrink(api):
    acc = _create(api, disc_count=1)
    grown = api.patch(f"/api/accessions/{acc['accession_id']}", json={"disc_count": 2})
    assert grown.status_code == 200
    grid = api.get(f"/api/accessions/{acc['accession_id']}/sides").json()
    assert len(grid) == 4

    shrunk = api.patch(f"/api/accessions/{acc['accession_id']}", json={"disc_count": 1})
    assert shrunk.status_code == 200  # no captures yet, legal
    assert len(api.get(f"/api/accessions/{acc['accession_id']}/sides").json()) == 2


def test_skip_side(api):
    acc = _create(api)
    resp = api.patch(
        f"/api/accessions/{acc['accession_id']}/sides/{acc['accession_id']}-D1-B",
        json={"skipped": True, "skip_reason": "cracked"},
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == "skipped"


def test_uncaptured_queue_excludes_78s_by_default(api):
    lp = _create(api)
    seventy_eight = _create(api, speed_rpm=78)
    for acc in (lp, seventy_eight):
        api.post(
            f"/api/accessions/{acc['accession_id']}/bind",
            json={"discogs_release_id": RELEASE_ID},
        )

    queue = api.get("/api/queues/uncaptured").json()
    ids = [row["accession_id"] for row in queue]
    assert lp["accession_id"] in ids
    assert seventy_eight["accession_id"] not in ids

    with_78s = api.get("/api/queues/uncaptured", params={"include_78s": True}).json()
    assert seventy_eight["accession_id"] in [r["accession_id"] for r in with_78s]


def test_photo_upload_and_fetch(api):
    acc = _create(api)
    resp = api.post(
        f"/api/accessions/{acc['accession_id']}/photos",
        files={"file": ("label.jpg", io.BytesIO(b"jpegbytes"), "image/jpeg")},
        data={"kind": "label", "disc": 1, "side": "A"},
    )
    assert resp.status_code == 201, resp.text
    asset = resp.json()
    assert asset["s3_key"].startswith(f"{acc['accession_id']}/label-d1a-")
    assert asset["s3_key"] in api.fake_s3.objects

    listed = api.get(f"/api/accessions/{acc['accession_id']}/assets").json()
    assert len(listed) == 1
    content = api.get(
        f"/api/accessions/{acc['accession_id']}/assets/{asset['id']}/content"
    )
    assert content.status_code == 200
    assert content.content == b"jpegbytes"


def test_runout_photo_must_name_a_side(api):
    acc = _create(api)
    resp = api.post(
        f"/api/accessions/{acc['accession_id']}/photos",
        files={"file": ("r.jpg", io.BytesIO(b"x"), "image/jpeg")},
        data={"kind": "runout"},
    )
    assert resp.status_code == 422


def test_delete_accession(api):
    acc = _create(api)
    assert api.delete(f"/api/accessions/{acc['accession_id']}").status_code == 204
    assert api.get(f"/api/accessions/{acc['accession_id']}").status_code == 404
