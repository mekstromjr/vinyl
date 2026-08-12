"""Pure-function tests: tracklist normalization and side mapping."""

from __future__ import annotations

from vinyl_server.discogs import map_positions_to_sides, normalize_tracklist, parse_duration


def test_parse_duration():
    assert parse_duration("3:45") == 225
    assert parse_duration("1:02:03") == 3723
    assert parse_duration("12:00") == 720
    assert parse_duration("") is None
    assert parse_duration(None) is None
    assert parse_duration("n/a") is None


def test_normalize_skips_headings_and_positionless_rows():
    raw = {
        "tracklist": [
            {"type_": "heading", "title": "Side One"},
            {"type_": "track", "position": "A1", "title": "Song", "duration": "3:00"},
            {"type_": "track", "position": "", "title": "hidden"},
        ]
    }
    tl = normalize_tracklist(raw)
    assert tl == [{"position": "A1", "title": "Song", "duration_sec": 180}]


def _tl(*positions_durations):
    return [
        {"position": p, "title": f"t{i}", "duration_sec": d}
        for i, (p, d) in enumerate(positions_durations)
    ]


def test_letter_positions_single_disc():
    m = map_positions_to_sides(_tl(("A1", 100), ("A2", 50), ("B1", 200)), disc_count=1)
    assert m[(1, "A")] == {"positions": ["A1", "A2"], "runtime_sec": 150}
    assert m[(1, "B")] == {"positions": ["B1"], "runtime_sec": 200}


def test_letter_positions_two_discs():
    m = map_positions_to_sides(_tl(("A1", 1), ("B1", 2), ("C1", 3), ("D1", 4)), disc_count=2)
    assert set(m) == {(1, "A"), (1, "B"), (2, "A"), (2, "B")}
    assert m[(2, "A")]["positions"] == ["C1"]


def test_runtime_null_when_any_duration_missing():
    """A partial sum would understate expected runtime and trip the QC
    duration-delta check on perfectly good captures."""
    m = map_positions_to_sides(_tl(("A1", 100), ("A2", None)), disc_count=1)
    assert m[(1, "A")]["runtime_sec"] is None
    assert m[(1, "A")]["positions"] == ["A1", "A2"]


def test_numeric_disc_track_positions_leave_side_unknown():
    m = map_positions_to_sides(_tl(("1-1", 10), ("2-1", 20)), disc_count=2)
    assert m[(1, "?")]["positions"] == ["1-1"]
    assert m[(2, "?")]["positions"] == ["2-1"]
    assert (1, "A") not in m


def test_positions_beyond_disc_count_are_dropped():
    """Trust the shelf: physical inventory wins over the listing."""
    m = map_positions_to_sides(_tl(("A1", 1), ("C1", 2)), disc_count=1)
    assert set(m) == {(1, "A")}


def test_bare_letter_sides():
    m = map_positions_to_sides(_tl(("A", 300), ("B", 280)), disc_count=1)
    assert m[(1, "A")]["runtime_sec"] == 300
