"""Presence reporting: payload shape, self-healing, and never breaking capture."""

from __future__ import annotations

import pytest
from vinyl_capture.apiclient import ApiError
from vinyl_capture.heartbeat import Heartbeat


class FakeApi:
    def __init__(self, heartbeat_error: Exception | None = None):
        self.registrations: list[tuple[str, dict]] = []
        self.beats: list[tuple[str, dict]] = []
        self.heartbeat_error = heartbeat_error

    def register_station(self, station_id, **fields):
        self.registrations.append((station_id, fields))
        return {}

    def station_heartbeat(self, station_id, **fields):
        if self.heartbeat_error is not None:
            err, self.heartbeat_error = self.heartbeat_error, None
            raise err
        self.beats.append((station_id, fields))
        return {}


class FakeStation:
    def __init__(self, phase="idle", side=None):
        self.phase = phase
        self.side = side


class FakeUploader:
    def __init__(self, staged=0, blow_up=False):
        self._staged = staged
        self._blow_up = blow_up

    def status(self):
        if self._blow_up:
            raise RuntimeError("uploader is having a day")
        return {"staged": self._staged, "current": None}


class Settings:
    alsa_device = "hw:USB"
    station_id = "test-station"
    station_name = "Test Station"


def _hb(api, station=None, uploader=None):
    return Heartbeat(Settings(), api, station or FakeStation(), uploader=uploader)


def test_beat_payload_is_small(monkeypatch):
    """The station's own status() carries review.summary.envelope_1s — a
    per-second array for a whole side. Shipping that every tick would push
    megabytes of waveform for a status line."""
    station = FakeStation(phase="recording", side={"side_id": "GM-0001-D1-A", "speed_rpm": 33})
    hb = _hb(FakeApi(), station, FakeUploader(staged=3))
    beat = hb.beat()
    assert beat == {
        "phase": "recording",
        "current_side_id": "GM-0001-D1-A",
        "pending_uploads": 3,
    }
    assert "envelope_1s" not in repr(beat)


def test_registers_before_first_beat():
    api = FakeApi()
    hb = _hb(api)
    hb.tick()
    assert len(api.registrations) == 1
    assert api.beats == []
    hb.tick()
    assert len(api.registrations) == 1
    assert len(api.beats) == 1


def test_reregisters_when_server_forgot_the_station():
    """A 404 means the row was deleted while we kept running. Heartbeating into
    the void until a human notices is the failure this prevents."""
    api = FakeApi(heartbeat_error=ApiError("404: no such station: test-station"))
    hb = _hb(api)
    hb.tick()                      # initial registration
    hb.tick()                      # heartbeat -> 404 -> re-register
    assert len(api.registrations) == 2


def test_non_404_errors_propagate():
    """A 500 is not 'the station is gone', so it must not trigger a re-register
    loop — the thread logs and retries on the next tick instead."""
    api = FakeApi(heartbeat_error=ApiError("500: database on fire"))
    hb = _hb(api)
    hb.tick()
    with pytest.raises(ApiError):
        hb.tick()
    assert len(api.registrations) == 1


def test_uploader_failure_does_not_break_presence():
    hb = _hb(FakeApi(), uploader=FakeUploader(blow_up=True))
    assert hb.beat()["pending_uploads"] is None


def test_registration_reports_capabilities(monkeypatch):
    api = FakeApi()
    hb = _hb(api)
    reg = hb.registration()
    assert reg["display_name"] == "Test Station"
    assert reg["capabilities"]["alsa_device"] == "hw:USB"
    assert "can_capture_vinyl" in reg["capabilities"]
