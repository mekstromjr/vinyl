"""Machine auth for /api routes.

Every /api route requires ``X-API-Key`` — the static service token shared by the
capture laptop and the QC jobs (from Vault via ExternalSecret).

The Authentik forward-auth headers (``X-authentik-username`` etc.) are
deliberately NOT accepted here: the /api ingress has no forward-auth middleware,
so those headers would be caller-controlled — trusting them would let anyone who
can reach the API impersonate a browser session. Human auth applies to the UI
routes only (ticket #3), which live behind the forward-auth ingress.
"""

from __future__ import annotations

import secrets

from fastapi import Security
from fastapi.exceptions import HTTPException
from fastapi.security import APIKeyHeader

from vinyl_server.config import settings

_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(key: str | None = Security(_header)) -> None:
    # compare_digest, not ==, to keep the comparison constant-time.
    if key is None or not secrets.compare_digest(key, settings().api_key):
        raise HTTPException(status_code=401, detail="missing or invalid API key")
