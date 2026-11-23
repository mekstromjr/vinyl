"""Station presence: registration, heartbeat, and derived liveness."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("VINYL_TEST_DB_URL") is None, reason="VINYL_TEST_DB_URL not set"
)

CAPS = {
    "alsa_device": "hw:USB",
    "alsa_device_present": True,
    "optical_drives": ["/dev/sr0"],
    "can_capture_vinyl": True,
    "can_rip_optical": True,
}


def _register(api, station_id="capture-station-1", **overrides):
    body = {
        "display_name": "<pve-host> (rack)",
        "hostname": "capture-station-1",
        "capabilities": CAPS,
        "agent_version": "v0.10.1-4-g1becc03",
    } | overrides
    resp = api.put(f"/api/stations/{station_id}", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_register_then_list(api):
    got = _register(api)
    assert got["station_id"] == "capture-station-1"
    assert got["capabilities"]["can_rip_optical"] is True
    # Registration is itself proof of life — no waiting a heartbeat to appear up.
    assert got["online"] is True

    listed = api.get("/api/stations").json()
    assert [s["station_id"] for s in listed] == ["capture-station-1"]


def test_registration_is_idempotent(api):
    """The agent re-registers on every start. A restart must update, not 409."""
    _register(api)
    again = _register(api, display_name="<pve-host> (renamed)")
    assert again["display_name"] == "<pve-host> (renamed)"
    assert len(api.get("/api/stations").json()) == 1


def test_heartbeat_updates_state(api):
    _register(api)
    resp = api.post(
        "/api/stations/capture-station-1/heartbeat",
        json={"phase": "recording", "current_side_id": "GM-0001-D1-A", "pending_uploads": 2},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["phase"] == "recording"
    assert body["current_side_id"] == "GM-0001-D1-A"
    assert body["pending_uploads"] == 2
    # Heartbeat must not disturb what registration established.
    assert body["capabilities"]["can_rip_optical"] is True


def test_heartbeat_for_unknown_station_404s(api):
    """Autovivifying here would create a station claiming no hardware, because a
    heartbeat carries no capabilities. The agent re-registers on 404 instead."""
    resp = api.post("/api/stations/ghost/heartbeat", json={"phase": "idle"})
    assert resp.status_code == 404


def test_online_is_derived_not_stored(api, session_factory):
    """An agent that dies without a clean shutdown must not leave a stale
    'online' behind — so liveness is computed from last_seen_at at read time."""
    _register(api)
    from vinyl_server.db.models import Station

    with session_factory() as s:
        station = s.get(Station, "capture-station-1")
        station.last_seen_at = datetime.now(UTC) - timedelta(hours=3)
        s.commit()

    assert api.get("/api/stations/capture-station-1").json()["online"] is False


def test_unknown_phase_is_accepted(api):
    """Agent and server deploy independently, so an unfamiliar phase must not
    fail the heartbeat and drop the station out of view."""
    _register(api)
    resp = api.post(
        "/api/stations/capture-station-1/heartbeat", json={"phase": "some-future-phase"}
    )
    assert resp.status_code == 200
    assert resp.json()["phase"] == "some-future-phase"


def test_requires_api_key(api):
    resp = api.put(
        "/api/stations/capture-station-1",
        json={"display_name": "x"},
        headers={"X-API-Key": "wrong"},
    )
    assert resp.status_code == 401
