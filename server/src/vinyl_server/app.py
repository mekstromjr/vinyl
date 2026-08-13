"""FastAPI application factory.

External clients (Discogs, S3) hang off ``app.state`` so tests can substitute
fakes without monkeypatching modules. Every /api route requires the service
API key (see auth.py for why forward-auth headers are deliberately not
accepted there); /healthz is open for probes.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles

from vinyl_server.auth import require_api_key
from vinyl_server.routers import accessions, discogs_search, queues


def create_app(
    discogs_client=None,
    s3_client=None,
    s3_bucket: str | None = None,
    musicbrainz_enabled: bool | None = None,
) -> FastAPI:
    app = FastAPI(title="vinyl", version="0.1.0")

    if discogs_client is None or s3_client is None or s3_bucket is None:
        # Deferred import so tests can build a fully-injected app with no env set.
        from vinyl_server.config import settings
        from vinyl_server.discogs import DiscogsClient
        from vinyl_server.s3 import make_client

        s = settings()
        discogs_client = discogs_client or DiscogsClient(s.discogs_token)
        s3_client = s3_client or make_client()
        s3_bucket = s3_bucket or s.s3_bucket
        if musicbrainz_enabled is None:
            musicbrainz_enabled = s.musicbrainz_enabled

    app.state.discogs = discogs_client
    app.state.s3 = s3_client
    app.state.s3_bucket = s3_bucket
    app.state.musicbrainz_enabled = bool(musicbrainz_enabled)

    guarded = [Depends(require_api_key)]
    app.include_router(accessions.router, dependencies=guarded)
    app.include_router(discogs_search.router, dependencies=guarded)
    app.include_router(queues.router, dependencies=guarded)

    # UI: no app-level auth — reachable only via the forward-auth ingress
    # (the /api ingress routes /api and /healthz exclusively).
    from vinyl_server.ui.routes import router as ui_router

    app.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).resolve().parent / "static")),
        name="static",
    )
    app.include_router(ui_router)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    return app
