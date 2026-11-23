"""Station identity and hardware probing."""

from __future__ import annotations

import pytest
from vinyl_capture import identity


def test_station_id_defaults_to_hostname(monkeypatch):
    monkeypatch.setattr(identity, "hostname", lambda: "capture-station-1")
    assert identity.station_id() == "capture-station-1"


def test_configured_id_wins(monkeypatch):
    monkeypatch.setattr(identity, "hostname", lambda: "capture-station-1")
    assert identity.station_id("rack-station") == "rack-station"


def test_unusable_id_fails_loudly():
    """Better a clear startup error than an opaque 500 from the database's
    ck_station_station_id_format constraint on first registration."""
    with pytest.raises(RuntimeError, match="not usable as an identifier"):
        identity.station_id("station with spaces")


def test_capabilities_never_raises_without_proc(monkeypatch, tmp_path):
    """A probe failure must be a missing field, not an exception: losing the
    station from the cluster's view is worse than an unknown capability."""
    monkeypatch.setattr(identity, "_alsa_cards", lambda: {})
    monkeypatch.setattr(identity, "_optical_drives", lambda: [])
    monkeypatch.setattr(identity, "_ffmpeg_version", lambda: None)
    caps = identity.capabilities("hw:USB")
    assert caps["can_capture_vinyl"] is False
    assert caps["can_rip_optical"] is False


def test_capabilities_detects_hardware(monkeypatch):
    monkeypatch.setattr(
        identity, "_alsa_cards",
        lambda: {"PCH": "HDA-Intel", "USB": "USB-Audio - Scarlett 2i2 USB"},
    )
    monkeypatch.setattr(identity, "_optical_drives", lambda: ["/dev/sr0"])
    monkeypatch.setattr(identity, "_ffmpeg_version", lambda: "7.1.5")
    caps = identity.capabilities("hw:USB")
    assert caps["can_capture_vinyl"] is True
    assert caps["alsa_device_description"] == "USB-Audio - Scarlett 2i2 USB"
    assert caps["can_rip_optical"] is True


def test_agent_version_missing_stamp_is_none(monkeypatch, tmp_path):
    monkeypatch.setattr(identity, "_VERSION_STAMP", tmp_path / "nope")
    assert identity.agent_version() is None


def test_agent_version_reads_stamp(monkeypatch, tmp_path):
    stamp = tmp_path / "v"
    stamp.write_text("v0.10.1-4-g1becc03\n")
    monkeypatch.setattr(identity, "_VERSION_STAMP", stamp)
    assert identity.agent_version() == "v0.10.1-4-g1becc03"
