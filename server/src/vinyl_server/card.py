"""4x6 card PDF generation (ticket #4).

One card per SET, printed individually on the HP Envy's 4x6 photo tray and
tucked into the archive sleeve. Design rules encoded here:

* Printed content is STATIC FACTS ONLY — identity and physical description.
  Never capture status or QC verdicts: a stamped status is stale the moment
  the database advances. The one exception is ink the *human* writes: the
  pen capture-log grid records a physical action at the moment it happens,
  so it cannot contradict the system.
* The barcode is Code 128 of the accession id — one barcode per set, never
  per side. Per-side granularity lives in the pen grid, where the human is
  the sensor.
* Vector output, uncompressed content streams (cards are tiny; it keeps the
  text assertable in tests and the barcode crisp at any print resolution).
"""

from __future__ import annotations

import io

from reportlab.graphics.barcode import code128
from reportlab.lib.colors import Color, black, white
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from vinyl_shared.states import Speed

PAGE_W, PAGE_H = 6 * inch, 4 * inch
MARGIN = 0.28 * inch

_MUTED = Color(0.42, 0.42, 0.42)
_RED = Color(0.70, 0.15, 0.12)

#: Sides beyond this many rows of the pen grid shrink to fit; in practice the
#: collection is 1-3 disc sets and this never triggers.
_GRID_COLS = 6


def _speed_label(speeds: set[Speed]) -> str:
    labels = {Speed.RPM_33: "33 1/3", Speed.RPM_45: "45", Speed.RPM_78: "78"}
    return " + ".join(labels[s] for s in sorted(speeds)) + " RPM"


def _truncate(c: canvas.Canvas, text: str, font: str, size: float, max_w: float) -> str:
    if c.stringWidth(text, font, size) <= max_w:
        return text
    while text and c.stringWidth(text + "...", font, size) > max_w:
        text = text[:-1]
    return text + "..."


def render_card(
    *,
    accession_id: str,
    artist: str | None,
    title: str | None,
    label: str | None,
    catalog_number: str | None,
    country: str | None,
    year: int | None,
    disc_count: int,
    speeds: set[Speed],
    condition_grade: str | None = None,
    condition_notes: str | None,
    side_ids: list[str],
) -> bytes:
    """Render the 4x6 landscape card; returns PDF bytes."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(PAGE_W, PAGE_H), pageCompression=0)
    c.setTitle(f"{accession_id} card")

    has_78 = Speed.RPM_78 in speeds
    x0, top = MARGIN, PAGE_H - MARGIN

    # --- Header: accession id (left) + Code 128 (right) ----------------------
    c.setFont("Helvetica-Bold", 30)
    c.drawString(x0, top - 0.32 * inch, accession_id)

    # Bar width chosen so the symbol + quiet zones stay ~2.1in — well within
    # what a $20 laser scanner reads, and the Envy prints crisply at 300dpi.
    barcode = code128.Code128(accession_id, barHeight=0.52 * inch, barWidth=0.0135 * inch)
    bc_x = PAGE_W - MARGIN - barcode.width
    barcode.drawOn(c, bc_x, top - 0.55 * inch)
    c.setFont("Courier", 8)
    c.drawCentredString(bc_x + barcode.width / 2, top - 0.68 * inch, accession_id)

    c.setStrokeColor(_MUTED)
    c.setLineWidth(0.7)
    c.line(x0, top - 0.80 * inch, PAGE_W - MARGIN, top - 0.80 * inch)

    # --- Release facts --------------------------------------------------------
    y = top - 1.08 * inch
    text_w = PAGE_W - 2 * MARGIN
    c.setFillColor(black)
    if artist or title:
        c.setFont("Helvetica-Bold", 14)
        c.drawString(x0, y, _truncate(c, artist or "Unknown artist", "Helvetica-Bold", 14, text_w))
        y -= 0.24 * inch
        c.setFont("Helvetica", 13)
        c.drawString(x0, y, _truncate(c, title or "", "Helvetica", 13, text_w))
        y -= 0.22 * inch
        pressing = " · ".join(
            str(p) for p in (label, catalog_number, country, year) if p
        )
        if pressing:
            c.setFillColor(_MUTED)
            c.setFont("Helvetica", 9.5)
            c.drawString(x0, y, _truncate(c, pressing, "Helvetica", 9.5, text_w))
            y -= 0.20 * inch
    else:
        c.setFont("Helvetica-Oblique", 13)
        c.setFillColor(_MUTED)
        c.drawString(x0, y, "Unidentified — see label/runout photos in the system")
        y -= 0.26 * inch

    # Physical facts line.
    c.setFillColor(black)
    c.setFont("Helvetica", 10)
    plural = "s" if disc_count != 1 else ""
    c.drawString(x0, y, f"{disc_count} disc{plural} · {_speed_label(speeds)}")
    y -= 0.20 * inch

    if condition_grade or condition_notes:
        cond = " — ".join(
            p for p in (condition_grade and condition_grade.capitalize(), condition_notes) if p
        )
        c.setFillColor(_MUTED)
        c.setFont("Helvetica", 8.5)
        c.drawString(x0, y, _truncate(c, cond, "Helvetica", 8.5, text_w))
        y -= 0.18 * inch

    # --- 78 banner ------------------------------------------------------------
    if has_78:
        c.setFillColor(_RED)
        c.rect(x0, y - 0.16 * inch, text_w, 0.26 * inch, stroke=0, fill=1)
        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 10.5)
        c.drawCentredString(
            PAGE_W / 2, y - 0.085 * inch, "78 RPM — SET ASIDE (phase two: different stylus + EQ)"
        )
        y -= 0.34 * inch

    # --- Pen capture-log grid -------------------------------------------------
    grid_top = max(y - 0.10 * inch, 1.18 * inch)
    c.setFillColor(black)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(x0, grid_top, "CAPTURE LOG")

    box = 0.17 * inch
    rows_needed = (len(side_ids) + _GRID_COLS - 1) // _GRID_COLS
    if rows_needed > 2:  # very large sets: shrink boxes rather than overflow
        box = 0.12 * inch
    gx, gy = x0, grid_top - 0.30 * inch
    col_w = (PAGE_W - 2 * MARGIN) / _GRID_COLS
    lowest = gy - box
    for i, side_id in enumerate(side_ids):
        col, row = i % _GRID_COLS, i // _GRID_COLS
        bx = gx + col * col_w
        by = gy - row * (box + 0.16 * inch)
        lowest = min(lowest, by - box)
        c.setStrokeColor(black)
        c.setLineWidth(1)
        c.rect(bx, by - box, box, box, stroke=1, fill=0)
        c.setFillColor(black)
        c.setFont("Helvetica", 8)
        # 'GM-0142-D1-A' -> 'D1-A'
        c.drawString(bx + box + 0.05 * inch, by - box + 0.035 * inch, side_id.split("-", 2)[2])

    # Caption BELOW the grid (it overlapped the heading when inline). Skipped
    # if an unusually tall grid leaves no room above the notes rule.
    caption_y = lowest - 0.14 * inch
    if caption_y > MARGIN + 0.36 * inch:
        c.setFillColor(_MUTED)
        c.setFont("Helvetica", 6.5)
        c.drawString(x0, caption_y, "handwritten, advisory — system of record is vinyl.meklab.net")

    # --- Field-notes rule -----------------------------------------------------
    c.setStrokeColor(_MUTED)
    c.setLineWidth(0.5)
    c.line(x0, MARGIN + 0.28 * inch, PAGE_W - MARGIN, MARGIN + 0.28 * inch)
    c.setFillColor(_MUTED)
    c.setFont("Helvetica", 6.5)
    c.drawString(x0, MARGIN + 0.16 * inch, "notes (pen): warps, skips, sleeve contents...")

    c.showPage()
    c.save()
    return buf.getvalue()
