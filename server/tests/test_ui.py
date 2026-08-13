"""UI route tests (same harness as test_api; HTML instead of JSON)."""

from __future__ import annotations

import io
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("VINYL_TEST_DB_URL") is None, reason="VINYL_TEST_DB_URL not set"
)

RELEASE_ID = 249504


def _create_via_ui(api, **overrides):
    form = {
        "disc_count": "1", "speed_rpm": "33", "has_jacket": "true",
        "has_inner_sleeve": "true", "condition_notes": "light ring wear",
    } | overrides
    resp = api.post("/inventory", data=form, follow_redirects=False)
    assert resp.status_code == 303
    return resp.headers["location"].split("/accessions/")[1].split("?")[0]


def test_home_renders_next_id_and_queue(api):
    resp = api.get("/")
    assert resp.status_code == 200
    assert "GM-0001" in resp.text  # next accession number staged
    assert "New record set" in resp.text


def test_ui_needs_no_api_key(api):
    """UI trust comes from the forward-auth ingress, not the machine key."""
    resp = api.get("/", headers={"X-API-Key": "wrong"})
    assert resp.status_code == 200


def test_create_flow_redirects_to_detail_with_big_id(api):
    acc_id = _create_via_ui(api)
    assert acc_id == "GM-0001"
    page = api.get(f"/accessions/{acc_id}", params={"new": "1"})
    assert page.status_code == 200
    assert "GM-0001" in page.text
    assert "big-id" in page.text
    assert "light ring wear" in page.text


def test_78_flag_shown(api):
    acc_id = _create_via_ui(api, speed_rpm="78")
    page = api.get(f"/accessions/{acc_id}")
    assert "set aside for phase two" in page.text


def test_search_partial_and_bind_flow(api):
    acc_id = _create_via_ui(api)
    results = api.get(f"/accessions/{acc_id}/search", params={"q": "waltz"})
    assert results.status_code == 200
    assert "Waltz For Debby" in results.text
    assert str(RELEASE_ID) in results.text

    page = api.post(
        f"/accessions/{acc_id}/bind", data={"discogs_release_id": str(RELEASE_ID)}
    )
    assert page.status_code == 200
    assert "identified" in page.text
    assert "Bill Evans Trio" in page.text
    # side expected runtimes rendered mm:ss
    assert "11:50" in page.text or "19:25" in page.text


def test_photo_upload_returns_updated_partial(api):
    acc_id = _create_via_ui(api)
    resp = api.post(
        f"/accessions/{acc_id}/photos",
        files={"file": ("label.jpg", io.BytesIO(b"jpeg"), "image/jpeg")},
        data={"kind": "label", "disc": "1", "side": "A"},
    )
    assert resp.status_code == 200
    assert "label" in resp.text
    content = api.get(resp.text.split('src="')[1].split('"')[0])
    assert content.status_code == 200
    assert content.content == b"jpeg"


def test_username_header_displayed(api):
    resp = api.get("/", headers={"X-authentik-username": "michael"})
    assert "michael" in resp.text


def test_unidentifiable_and_delete(api):
    acc_id = _create_via_ui(api)
    resp = api.post(
        f"/accessions/{acc_id}/status", data={"status": "unidentifiable"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "unidentifiable" in api.get(f"/accessions/{acc_id}").text

    resp = api.post(f"/accessions/{acc_id}/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert api.get(f"/accessions/{acc_id}").status_code == 404
