"""Work queues: what both UIs (and Michael) actually act on."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from vinyl_server.db.session import get_session

router = APIRouter(prefix="/api/queues", tags=["queues"])


@router.get("/uncaptured")
def uncaptured(include_78s: bool = False, session: Session = Depends(get_session)):
    """Identified accessions with pending sides — the capture backlog.

    78s are excluded by default: phase one cannot capture them (different
    stylus, non-RIAA EQ), so surfacing them in the backlog would just be noise.
    """
    speed_clause = "" if include_78s else "AND v.speed_rpm <> 78"
    rows = session.execute(
        text(
            f"""
            SELECT v.accession_id, v.artist, v.title,
                   count(*) FILTER (WHERE v.state = 'pending') AS pending_sides,
                   count(*) AS total_sides
            FROM v_side_status v
            WHERE v.accession_status = 'identified' {speed_clause}
            GROUP BY v.accession_id, v.artist, v.title
            HAVING count(*) FILTER (WHERE v.state = 'pending') > 0
            ORDER BY v.accession_id
            """
        )
    )
    return [dict(r._mapping) for r in rows]


@router.get("/qc-review")
def qc_review(session: Session = Depends(get_session)):
    """Current captures whose authoritative QC said 'a human should listen'."""
    rows = session.execute(
        text(
            """
            SELECT v.side_id, v.accession_id, v.artist, v.title, v.qc_verdict,
                   v.lead_in_noise_floor_dbfs, v.clicks_per_min, v.captured_at
            FROM v_side_status v
            WHERE v.qc_verdict IN ('review', 'recapture')
            ORDER BY v.captured_at DESC NULLS LAST
            """
        )
    )
    return [dict(r._mapping) for r in rows]
