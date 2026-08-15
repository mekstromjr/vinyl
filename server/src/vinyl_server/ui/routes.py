"""Server-rendered inventory UI (iPad-first; Jinja2 + HTMX).

These routes carry NO app-level auth: they are reachable only through the
forward-auth ingress (vinyl.meklab.net), where Authentik has already vouched
for the browser. The /api ingress exposes /api and /healthz only, so none of
this is reachable with just an API key. The username header is display-only.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, text
from sqlalchemy.orm import Session, joinedload
from vinyl_shared.states import AccessionStatus, AssetKind, Speed

from vinyl_server import services
from vinyl_server.db.models import Accession, Asset
from vinyl_server.db.session import get_session
from vinyl_server.discogs import DiscogsError

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))


def _username(request: Request) -> str | None:
    return request.headers.get("x-authentik-username")


def _accession_context(request: Request, session: Session, accession_id: str, **extra) -> dict:
    acc = services.get_accession(session, accession_id)
    grid = services.side_grid(session, accession_id)
    positions_by_side = {s.side_id: s.track_positions for s in acc.sides}
    sides = [dict(row, positions=positions_by_side.get(row["side_id"])) for row in grid]

    # Positions in the bound tracklist that no side claims — the "?"-side
    # numbering conventions land here for the human to resolve.
    unmapped = []
    if acc.release:
        claimed = {p for s in acc.sides for p in (s.track_positions or [])}
        unmapped = [
            t["position"] for t in acc.release.tracklist if t["position"] not in claimed
        ]

    assets = session.scalars(
        select(Asset).where(Asset.accession_id == acc.accession_id).order_by(Asset.created_at)
    ).all()

    # Listen affordance: current captures that have an uploaded master.
    from vinyl_server.db.models import Capture

    cap_ids = [row["current_capture_id"] for row in grid if row.get("current_capture_id")]
    listen: dict[str, dict] = {}
    if cap_ids:
        for cap in session.scalars(select(Capture).where(Capture.id.in_(cap_ids))):
            if cap.s3_key:
                listen[cap.side_id] = {
                    "capture_id": str(cap.id),
                    "envelope": (cap.integrity or {}).get("envelope_1s") or [],
                }
    return {
        "username": _username(request),
        "acc": acc,
        "sides": sides,
        "assets": assets,
        "unmapped_positions": unmapped,
        "listen": listen,
        "has_78": any(s.speed_rpm == Speed.RPM_78 for s in acc.sides),
        **extra,
    }


@router.get("/")
def home(request: Request, session: Session = Depends(get_session)):
    prefix, next_number = session.execute(
        text("SELECT prefix, next_number FROM collection ORDER BY prefix LIMIT 1")
    ).first()
    uncaptured = session.execute(
        text(
            """
            SELECT v.accession_id, v.artist, v.title,
                   count(*) FILTER (WHERE v.state = 'pending') AS pending_sides,
                   count(*) AS total_sides
            FROM v_side_status v
            WHERE v.accession_status = 'identified' AND v.speed_rpm <> 78
            GROUP BY v.accession_id, v.artist, v.title
            HAVING count(*) FILTER (WHERE v.state = 'pending') > 0
            ORDER BY v.accession_id
            """
        )
    ).all()
    recent = session.scalars(
        select(Accession)
        .options(joinedload(Accession.release))
        .order_by(Accession.inventoried_at.desc())
        .limit(15)
    ).all()
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "username": _username(request),
            "next_id": f"{prefix}-{next_number:04d}",
            "uncaptured": uncaptured,
            "recent": recent,
        },
    )


@router.get("/inventory/new")
def new_form(request: Request):
    return templates.TemplateResponse(
        request, "new.html", {"username": _username(request)}
    )


@router.post("/inventory")
def create(
    request: Request,
    disc_count: int = Form(...),
    speed_rpm: int = Form(33),
    has_jacket: str = Form("true"),
    has_inner_sleeve: str = Form("true"),
    condition_notes: str = Form(""),
    session: Session = Depends(get_session),
):
    acc = services.create_accession(
        session,
        collection="GM",
        disc_count=disc_count,
        speed_rpm=Speed(speed_rpm),
        has_jacket=has_jacket == "true",
        has_inner_sleeve=has_inner_sleeve == "true",
        condition_notes=condition_notes.strip() or None,
    )
    return RedirectResponse(f"/accessions/{acc.accession_id}?new=1", status_code=303)


@router.get("/accessions/{accession_id}")
def detail(accession_id: str, request: Request, session: Session = Depends(get_session)):
    ctx = _accession_context(
        request, session, accession_id, just_created=request.query_params.get("new") == "1"
    )
    return templates.TemplateResponse(request, "accession.html", ctx)


@router.get("/accessions/{accession_id}/search")
def search(
    accession_id: str,
    request: Request,
    catno: str = "",
    q: str = "",
    session: Session = Depends(get_session),
):
    acc = services.get_accession(session, accession_id)
    if not catno.strip() and not q.strip():
        results = []
    else:
        try:
            results = request.app.state.discogs.search(
                query=q.strip() or None, catno=catno.strip() or None
            )
        except DiscogsError:
            results = []
    return templates.TemplateResponse(
        request, "partials/search_results.html", {"acc": acc, "results": results}
    )


@router.post("/accessions/{accession_id}/bind")
def bind(
    accession_id: str,
    request: Request,
    discogs_release_id: int = Form(...),
    session: Session = Depends(get_session),
):
    acc = services.get_accession(session, accession_id)
    services.bind_release(
        session,
        request.app.state.discogs,
        acc,
        discogs_release_id,
        musicbrainz_enabled=request.app.state.musicbrainz_enabled,
    )
    # HTMX swaps the whole body with the re-rendered page.
    return templates.TemplateResponse(
        request, "accession.html", _accession_context(request, session, accession_id)
    )


@router.post("/accessions/{accession_id}/photos")
def upload_photo(
    accession_id: str,
    request: Request,
    file: UploadFile = File(...),
    kind: str = Form(...),
    disc: str = Form(""),
    side: str = Form(""),
    session: Session = Depends(get_session),
):
    acc = services.get_accession(session, accession_id)
    services.save_photo(
        session,
        request.app.state.s3,
        request.app.state.s3_bucket,
        acc,
        kind=AssetKind(kind),
        disc=int(disc) if disc else None,
        side=side or None,
        content=file.file.read(),
        content_type=file.content_type,
    )
    session.flush()
    ctx = _accession_context(request, session, accession_id)
    return templates.TemplateResponse(request, "partials/photos.html", ctx)


@router.get("/accessions/{accession_id}/assets/{asset_id}/content")
def asset_content(
    accession_id: str,
    asset_id: uuid.UUID,
    request: Request,
    session: Session = Depends(get_session),
):
    asset = session.get(Asset, asset_id)
    if asset is None or asset.accession_id != accession_id.upper():
        raise HTTPException(404, "no such asset")
    obj = request.app.state.s3.get_object(Bucket=asset.s3_bucket, Key=asset.s3_key)
    return StreamingResponse(
        obj["Body"], media_type=asset.content_type or "application/octet-stream"
    )


@router.post("/accessions/{accession_id}/assets/{asset_id}/delete")
def delete_asset(
    accession_id: str,
    asset_id: uuid.UUID,
    request: Request,
    session: Session = Depends(get_session),
):
    asset = session.get(Asset, asset_id)
    if asset is None or asset.accession_id != accession_id.upper():
        raise HTTPException(404, "no such asset")
    services.delete_asset(session, request.app.state.s3, asset)
    ctx = _accession_context(request, session, accession_id)
    return templates.TemplateResponse(request, "partials/photos.html", ctx)


@router.get("/accessions/{accession_id}/captures/{capture_id}/audio")
def capture_audio(
    accession_id: str,
    capture_id: uuid.UUID,
    request: Request,
    session: Session = Depends(get_session),
):
    """Stream a master from Garage with HTTP Range support.

    Range is what makes the scrub bar usable: seeking fetches byte ranges via
    S3 instead of downloading half a gigabyte to hear ten seconds. Totals come
    from the bytes column recorded at capture time.
    """
    from fastapi.responses import Response

    from vinyl_server.db.models import Capture

    cap = session.get(Capture, capture_id)
    if (
        cap is None
        or cap.side_id.rsplit("-", 2)[0] != accession_id.upper()
        or cap.s3_key is None
    ):
        raise HTTPException(404, "no uploaded audio for that capture")

    total = cap.bytes
    range_header = request.headers.get("range")
    s3 = request.app.state.s3
    common = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": f'inline; filename="{cap.side_id}.flac"',
    }
    if range_header and total:
        try:
            start_s, _, end_s = range_header.split("=", 1)[1].partition("-")
            start = int(start_s) if start_s else 0
            end = min(int(end_s) if end_s else total - 1, total - 1)
        except ValueError as exc:
            raise HTTPException(416, "bad Range header") from exc
        if start >= total:
            raise HTTPException(416, "Range beyond end of file")
        obj = s3.get_object(Bucket=cap.s3_bucket, Key=cap.s3_key,
                            Range=f"bytes={start}-{end}")
        return Response(
            content=obj["Body"].read(),
            status_code=206,
            media_type="audio/flac",
            headers=dict(
                common,
                **{
                    "Content-Range": f"bytes {start}-{end}/{total}",
                    "Content-Length": str(end - start + 1),
                },
            ),
        )
    obj = s3.get_object(Bucket=cap.s3_bucket, Key=cap.s3_key)
    return StreamingResponse(
        obj["Body"], media_type="audio/flac", headers=common
    )


@router.get("/accessions/{accession_id}/card.pdf")
def card_pdf(accession_id: str, session: Session = Depends(get_session)):
    from fastapi.responses import Response

    from vinyl_server.card import render_card

    acc = services.get_accession(session, accession_id)
    pdf = render_card(
        accession_id=acc.accession_id,
        artist=acc.release.artist if acc.release else None,
        title=acc.release.title if acc.release else None,
        label=acc.release.label if acc.release else None,
        catalog_number=acc.release.catalog_number if acc.release else None,
        country=acc.release.country if acc.release else None,
        year=acc.release.year if acc.release else None,
        disc_count=acc.disc_count,
        speeds={s.speed_rpm for s in acc.sides},
        condition_notes=acc.condition_notes,
        side_ids=[s.side_id for s in sorted(acc.sides, key=lambda s: (s.disc, s.side))],
    )
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{acc.accession_id}-card.pdf"'
        },
    )


@router.post("/accessions/{accession_id}/condition")
def save_condition(
    accession_id: str,
    condition_notes: str = Form(""),
    session: Session = Depends(get_session),
):
    acc = services.get_accession(session, accession_id)
    acc.condition_notes = condition_notes.strip() or None
    session.flush()
    return RedirectResponse(f"/accessions/{acc.accession_id}", status_code=303)


@router.post("/accessions/{accession_id}/status")
def set_status(
    accession_id: str,
    status: str = Form(...),
    session: Session = Depends(get_session),
):
    acc = services.get_accession(session, accession_id)
    new_status = AccessionStatus(status)
    if new_status == AccessionStatus.IDENTIFIED:
        raise HTTPException(422, "identified is set by binding, not by hand")
    if new_status == AccessionStatus.UNIDENTIFIABLE:
        acc.release = None
    acc.status = new_status
    session.flush()
    return RedirectResponse(f"/accessions/{acc.accession_id}", status_code=303)


@router.post("/accessions/{accession_id}/delete")
def delete(accession_id: str, session: Session = Depends(get_session)):
    session.delete(services.get_accession(session, accession_id))
    return RedirectResponse("/", status_code=303)
