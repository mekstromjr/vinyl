"""Card PDF tests. Content streams are uncompressed, so text is assertable."""

from __future__ import annotations

import os

import pytest
from vinyl_server.card import render_card
from vinyl_shared.states import Speed


def _render(**overrides):
    args = dict(
        accession_id="GM-0142",
        artist="Bill Evans Trio",
        title="Waltz For Debby",
        label="Riverside Records",
        catalog_number="RLP 9399",
        country="US",
        year=1962,
        disc_count=1,
        speeds={Speed.RPM_33},
        condition_notes="VG+ jacket, light ring wear",
        side_ids=["GM-0142-D1-A", "GM-0142-D1-B"],
    )
    args.update(overrides)
    return render_card(**args)


def test_renders_valid_pdf_with_facts():
    pdf = _render()
    assert pdf.startswith(b"%PDF")
    assert b"GM-0142" in pdf
    assert b"Bill Evans Trio" in pdf
    assert b"Waltz For Debby" in pdf
    assert b"RLP 9399" in pdf
    # pen grid labels, one per side
    assert b"D1-A" in pdf and b"D1-B" in pdf


def test_page_is_4x6_landscape():
    pdf = _render()
    assert b"MediaBox [0 0 432 288]" in pdf or b"MediaBox [ 0 0 432 288 ]" in pdf


def test_unbound_card_still_renders():
    pdf = _render(artist=None, title=None, label=None, catalog_number=None,
                  country=None, year=None)
    assert b"Unidentified" in pdf
    assert b"GM-0142" in pdf


def test_78_banner():
    pdf = _render(speeds={Speed.RPM_78})
    assert b"SET ASIDE" in pdf
    assert b"78 RPM" in pdf


def test_multi_disc_grid():
    sides = [f"GM-0142-D{d}-{s}" for d in (1, 2, 3) for s in ("A", "B")]
    pdf = _render(disc_count=3, side_ids=sides)
    for label in (b"D1-A", b"D2-B", b"D3-A"):
        assert label in pdf


def test_no_status_words_ever_printed():
    """The card carries static facts only — never system state."""
    pdf = _render()
    for forbidden in (b"pending", b"captured", b"uploaded", b"qc_", b"identified"):
        assert forbidden not in pdf


@pytest.mark.skipif(
    os.environ.get("VINYL_TEST_DB_URL") is None, reason="VINYL_TEST_DB_URL not set"
)
def test_card_route(api):
    resp = api.post("/inventory", data={"disc_count": "1", "speed_rpm": "33"},
                    follow_redirects=False)
    acc_id = resp.headers["location"].split("/accessions/")[1].split("?")[0]
    card = api.get(f"/accessions/{acc_id}/card.pdf")
    assert card.status_code == 200
    assert card.headers["content-type"] == "application/pdf"
    assert card.content.startswith(b"%PDF")
    assert acc_id.encode() in card.content


def test_condition_grade_printed_with_notes():
    pdf = _render(condition_grade="poor", condition_notes="taped seams")
    assert b"Poor" in pdf and b"taped seams" in pdf
    # grade alone still prints; neither alone crashes the layout
    assert b"Good" in _render(condition_grade="good", condition_notes=None)
