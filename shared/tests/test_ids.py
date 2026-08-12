"""The ID format is a documented invariant: it is printed on cards and barcodes."""

from __future__ import annotations

import pytest
from vinyl_shared.ids import (
    MAX_ACCESSION_NUMBER,
    format_accession_id,
    format_side_id,
    parse_accession_id,
    parse_side_id,
)


def test_accession_id_is_zero_padded_to_four_digits():
    assert format_accession_id("GM", 142) == "GM-0142"
    assert format_accession_id("GM", 1) == "GM-0001"
    assert format_accession_id("GM", 9999) == "GM-9999"


def test_single_disc_sets_still_emit_d1():
    """No special case anywhere: -D1 is always present."""
    assert format_side_id("GM-0142", 1, "A") == "GM-0142-D1-A"


def test_side_id_round_trips():
    side_id = format_side_id("GM-0142", 2, "B")
    assert side_id == "GM-0142-D2-B"
    assert parse_side_id(side_id) == ("GM-0142", 2, "B")


def test_accession_id_round_trips():
    assert parse_accession_id(format_accession_id("ABCD", 7)) == ("ABCD", 7)


@pytest.mark.parametrize("number", [0, -1, MAX_ACCESSION_NUMBER + 1])
def test_out_of_range_numbers_rejected(number):
    with pytest.raises(ValueError):
        format_accession_id("GM", number)


@pytest.mark.parametrize("prefix", ["G", "GMGMG", "gm", "G1", ""])
def test_bad_collection_prefixes_rejected(prefix):
    with pytest.raises(ValueError):
        format_accession_id(prefix, 1)


@pytest.mark.parametrize("bad", ["GM-142", "GM-00142", "gm-0142", "GM0142", "GM-0142-D1-A"])
def test_malformed_accession_ids_rejected(bad):
    with pytest.raises(ValueError):
        parse_accession_id(bad)


@pytest.mark.parametrize("bad", ["GM-0142", "GM-0142-D1-C", "GM-0142-1-A", "GM-0142-D0-A"])
def test_malformed_side_ids_rejected(bad):
    with pytest.raises(ValueError):
        parse_side_id(bad)


def test_side_id_rejects_bad_components():
    with pytest.raises(ValueError):
        format_side_id("GM-0142", 0, "A")
    with pytest.raises(ValueError):
        format_side_id("GM-0142", 1, "C")
    with pytest.raises(ValueError):
        format_side_id("not-an-accession", 1, "A")
