from __future__ import annotations

from vinyl_capture.ha import capture_finished_message


def test_captured_message_mentions_plug_and_next_action():
    title, msg = capture_finished_message({
        "side_id": "GM-0142-D1-A",
        "status": "captured",
        "summary": {"duration_sec": 1322, "peak_dbfs_l": -6.4, "peak_dbfs_r": -6.9},
    })
    assert title == "GM-0142-D1-A captured"
    assert "22 min" in msg
    assert "-6.4/-6.9" in msg
    assert "powered off" in msg


def test_failed_message_carries_reasons():
    title, msg = capture_finished_message({
        "side_id": "GM-0142-D1-B",
        "status": "failed",
        "reasons": ["3 ALSA xrun(s) — samples were lost"],
    })
    assert "FAILED" in title
    assert "xrun" in msg
    assert "again" in msg


def test_plug_on_uses_turn_on_and_respects_config(monkeypatch):
    from types import SimpleNamespace

    from vinyl_capture import ha

    calls = []
    monkeypatch.setattr(ha, "_post", lambda s, path, payload: calls.append((path, payload)))
    configured = SimpleNamespace(ha_url="http://ha", ha_token="t", ha_entity="switch.zp_07")
    ha.plug_on(configured)
    assert calls == [("/api/services/switch/turn_on", {"entity_id": "switch.zp_07"})]

    # unconfigured station (no HA) must stay silent, not crash the scan
    ha.plug_on(SimpleNamespace(ha_url=None, ha_token=None, ha_entity=None))
    assert len(calls) == 1
