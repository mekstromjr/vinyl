"""Discogs live-search proxy for the inventory UI."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from vinyl_server.discogs import DiscogsError

router = APIRouter(prefix="/api/discogs", tags=["discogs"])


@router.get("/search")
def search(
    request: Request,
    q: str | None = None,
    catno: str | None = None,
    artist: str | None = None,
):
    if not any((q, catno, artist)):
        raise HTTPException(422, "provide at least one of: q, catno, artist")
    try:
        return request.app.state.discogs.search(query=q, catno=catno, artist=artist)
    except DiscogsError as exc:
        raise HTTPException(502, str(exc)) from exc
