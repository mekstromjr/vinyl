"""MusicBrainz release lookup via Discogs URL relationship.

MB indexes Discogs release URLs as URL relationships, so
``GET /ws/2/url?resource=https://www.discogs.com/release/N`` is a deterministic
join between the two databases — no name matching. Best-effort: many pressings
have no MB entry, and that's fine (the column is nullable).

MB asks for max 1 req/sec and a meaningful User-Agent; we do one request per
bind, so a simple courtesy sleep on the client suffices.
"""

from __future__ import annotations

import uuid

import httpx

_UA = "meklab-vinyl/0.1 (michael.ekstrom@me.com)"


def lookup_release_mbid(discogs_release_id: int, timeout: float = 10.0) -> uuid.UUID | None:
    """MBID of the release linked to this Discogs release, or None."""
    resource = f"https://www.discogs.com/release/{discogs_release_id}"
    try:
        resp = httpx.get(
            "https://musicbrainz.org/ws/2/url",
            params={"resource": resource, "inc": "release-rels", "fmt": "json"},
            headers={"User-Agent": _UA},
            timeout=timeout,
        )
        if resp.status_code != 200:
            return None
        for rel in resp.json().get("relations", []):
            release = rel.get("release")
            if release and release.get("id"):
                return uuid.UUID(release["id"])
    except (httpx.HTTPError, ValueError):
        return None
    return None
