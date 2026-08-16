"""Range-streaming audio route: what makes the scrub bar affordable."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("VINYL_TEST_DB_URL") is None, reason="VINYL_TEST_DB_URL not set"
)

AUDIO = b"fLaC" + bytes(range(256)) * 4  # 1028 fake bytes


def _uploaded_capture(api):
    acc = api.post(
        "/api/accessions", json={"collection": "GM", "disc_count": 1, "speed_rpm": 33}
    ).json()
    side_id = f"{acc['accession_id']}-D1-A"
    cap = api.post("/api/captures", json={"side_id": side_id}).json()
    api.patch(
        f"/api/captures/{cap['id']}",
        json={
            "status": "captured",
            "bytes": len(AUDIO),
            "integrity": {"envelope_1s": [0.1, 0.5, 0.9]},
        },
    )
    key = f"masters/{side_id}/{cap['id']}.flac"
    api.fake_s3.objects[key] = AUDIO
    api.post(
        f"/api/captures/{cap['id']}/uploaded", json={"s3_bucket": "vinyl-test", "s3_key": key}
    )
    return acc["accession_id"], cap["id"]


def test_full_stream(api):
    acc_id, cap_id = _uploaded_capture(api)
    resp = api.get(f"/accessions/{acc_id}/captures/{cap_id}/audio")
    assert resp.status_code == 200
    assert resp.content == AUDIO
    assert resp.headers["accept-ranges"] == "bytes"


def test_range_request_gets_206_and_the_right_bytes(api):
    acc_id, cap_id = _uploaded_capture(api)
    resp = api.get(
        f"/accessions/{acc_id}/captures/{cap_id}/audio", headers={"Range": "bytes=4-7"}
    )
    assert resp.status_code == 206
    assert resp.content == AUDIO[4:8]
    assert resp.headers["content-range"] == f"bytes 4-7/{len(AUDIO)}"


def test_open_ended_range(api):
    acc_id, cap_id = _uploaded_capture(api)
    resp = api.get(
        f"/accessions/{acc_id}/captures/{cap_id}/audio",
        headers={"Range": f"bytes={len(AUDIO) - 4}-"},
    )
    assert resp.status_code == 206
    assert resp.content == AUDIO[-4:]


def test_browser_opening_move_bytes_0_dash(api):
    """Safari/Chrome open audio with an open-ended whole-file range. This must
    stream, not buffer — buffering it OOM-killed the pod on a 487MB master."""
    acc_id, cap_id = _uploaded_capture(api)
    resp = api.get(
        f"/accessions/{acc_id}/captures/{cap_id}/audio", headers={"Range": "bytes=0-"}
    )
    assert resp.status_code == 206
    assert resp.content == AUDIO
    assert resp.headers["content-range"] == f"bytes 0-{len(AUDIO) - 1}/{len(AUDIO)}"


def test_range_beyond_eof_416(api):
    acc_id, cap_id = _uploaded_capture(api)
    resp = api.get(
        f"/accessions/{acc_id}/captures/{cap_id}/audio",
        headers={"Range": f"bytes={len(AUDIO) + 10}-"},
    )
    assert resp.status_code == 416


def test_not_uploaded_yet_404(api):
    acc = api.post(
        "/api/accessions", json={"collection": "GM", "disc_count": 1, "speed_rpm": 33}
    ).json()
    cap = api.post("/api/captures", json={"side_id": f"{acc['accession_id']}-D1-A"}).json()
    resp = api.get(f"/accessions/{acc['accession_id']}/captures/{cap['id']}/audio")
    assert resp.status_code == 404


def test_listen_control_renders_for_uploaded_side(api):
    acc_id, cap_id = _uploaded_capture(api)
    page = api.get(f"/accessions/{acc_id}")
    assert "Listen" in page.text
    assert cap_id in page.text
    assert "data-envelope" in page.text


def test_qc_panel_renders_verdict_and_reasons(api):
    acc_id, cap_id = _uploaded_capture(api)
    import uuid

    from vinyl_server.db.models import Capture
    from vinyl_server.db.session import get_session
    # write a verdict directly (the QC job path is exercised elsewhere)
    override = api.app.dependency_overrides[get_session]
    gen = override()
    session = next(gen)
    cap = session.get(Capture, uuid.UUID(cap_id))
    cap.qc_verdict = "review"
    cap.qc = {"reasons": ["41.0 clicks/min — surface damage"],
              "channel_imbalance_db": 1.0, "dropout_starts_sec": [61.5],
              "mono_fold_candidate": False, "buy_commercial_candidate": True}
    cap.clicks_per_min = 41.0
    session.commit()
    page = api.get(f"/accessions/{acc_id}")
    assert "surface damage" in page.text
    assert "consider a commercial copy" in page.text
    assert "1:01" in page.text  # clickable dropout timestamp
