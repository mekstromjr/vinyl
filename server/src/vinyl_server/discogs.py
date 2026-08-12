"""Discogs client + tracklist normalization.

The client is thin httpx; the interesting logic is pure functions below it —
``normalize_tracklist`` and ``map_positions_to_sides`` — which turn Discogs'
loosely-conventional position strings into the per-side mapping the schema
stores. Pure so they are trivially testable against fixture JSON.

Rate limiting: authenticated requests get 60/min. We track the
``X-Discogs-Ratelimit-Remaining`` response header and sleep just enough when it
runs low, rather than pacing every call — inventory is bursty (a search then a
bind), not streaming.
"""

from __future__ import annotations

import re
import time
from typing import Any

import httpx

_BASE = "https://api.discogs.com"
_UA = "meklab-vinyl/0.1 +https://gitlab.meklab.net/meklab/vinyl"


class DiscogsError(RuntimeError):
    pass


class DiscogsClient:
    def __init__(self, token: str, timeout: float = 15.0) -> None:
        self._client = httpx.Client(
            base_url=_BASE,
            timeout=timeout,
            headers={
                "User-Agent": _UA,
                "Authorization": f"Discogs token={token}",
            },
        )

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        resp = self._client.get(path, params=params)
        remaining = resp.headers.get("X-Discogs-Ratelimit-Remaining")
        if remaining is not None and int(remaining) <= 2:
            time.sleep(2.0)  # let the 60/min window breathe before the next call
        if resp.status_code == 429:
            raise DiscogsError("Discogs rate limit hit; retry shortly")
        if resp.status_code != 200:
            raise DiscogsError(f"Discogs {path}: HTTP {resp.status_code}")
        return resp.json()

    def search(
        self,
        query: str | None = None,
        catno: str | None = None,
        artist: str | None = None,
        per_page: int = 15,
    ) -> list[dict]:
        """Vinyl-format release search. Returns the raw result list, trimmed."""
        params: dict[str, Any] = {"type": "release", "format": "Vinyl", "per_page": per_page}
        if query:
            params["q"] = query
        if catno:
            params["catno"] = catno
        if artist:
            params["artist"] = artist
        data = self._get("/database/search", params)
        return [
            {
                "discogs_release_id": r.get("id"),
                "title": r.get("title"),
                "year": r.get("year"),
                "label": (r.get("label") or [None])[0],
                "catno": r.get("catno"),
                "country": r.get("country"),
                "format": r.get("format"),
                "thumb": r.get("thumb"),
            }
            for r in data.get("results", [])
        ]

    def get_release(self, release_id: int) -> dict:
        """The full raw release document — cached verbatim in release.discogs_raw."""
        return self._get(f"/releases/{release_id}")


# --------------------------------------------------------------------------- #
# Pure normalization helpers
# --------------------------------------------------------------------------- #

def parse_duration(text: str | None) -> int | None:
    """``"3:45" -> 225``. Discogs durations are freeform; None when unparseable."""
    if not text:
        return None
    text = text.strip()
    m = re.fullmatch(r"(?:(\d+):)?(\d+):(\d{2})", text)
    if m is None:
        return None
    hours = int(m.group(1) or 0)
    return hours * 3600 + int(m.group(2)) * 60 + int(m.group(3))


def normalize_tracklist(raw: dict) -> list[dict]:
    """Extract the schema's tracklist shape from a raw Discogs release.

    Skips non-track rows (headings, index tracks without positions).
    """
    out = []
    for t in raw.get("tracklist", []):
        if t.get("type_") not in (None, "track"):
            continue
        position = (t.get("position") or "").strip()
        if not position:
            continue
        out.append(
            {
                "position": position,
                "title": t.get("title", ""),
                "duration_sec": parse_duration(t.get("duration")),
            }
        )
    return out


_LETTER_POS_RE = re.compile(r"^([A-Za-z])\d*[a-z]?$")   # A1, B2, C, D10a
_DISC_TRACK_RE = re.compile(r"^(\d+)[-.](\d+)$")        # 1-1, 2.3


def map_positions_to_sides(
    tracklist: list[dict], disc_count: int
) -> dict[tuple[int, str], dict]:
    """Assign normalized tracks to (disc, side) using position conventions.

    Handles the two conventions that cover nearly all vinyl listings:

    * Letter sides: A1/B2/C1/D3 — letter index maps pairwise to discs
      (A,B -> disc 1; C,D -> disc 2; ...).
    * Disc-track numerics: 1-1/2-3 — names the disc but NOT the side, so those
      map to a disc with side unknown and are returned under side ``"?"`` for
      the caller to leave unmapped (human fixes it in the UI; the mapping is
      editable precisely because this is genuinely ambiguous).

    Returns ``{(disc, side): {"positions": [...], "runtime_sec": int|None}}``.
    ``runtime_sec`` is only set when EVERY track on the side has a duration —
    a partial sum would silently understate the expected runtime and trip the
    QC duration-delta check on perfectly good captures.
    """
    grouped: dict[tuple[int, str], list[dict]] = {}
    for track in tracklist:
        pos = track["position"]
        m = _LETTER_POS_RE.match(pos)
        if m:
            letter = m.group(1).upper()
            idx = ord(letter) - ord("A")
            disc, side = idx // 2 + 1, "AB"[idx % 2]
        else:
            m = _DISC_TRACK_RE.match(pos)
            if m:
                disc, side = int(m.group(1)), "?"
            else:
                continue  # unrecognized convention; leave for the human
        if disc > disc_count:
            continue  # listing disagrees with physical inventory; trust the shelf
        grouped.setdefault((disc, side), []).append(track)

    out: dict[tuple[int, str], dict] = {}
    for key, tracks in grouped.items():
        durations = [t["duration_sec"] for t in tracks]
        out[key] = {
            "positions": [t["position"] for t in tracks],
            "runtime_sec": sum(durations) if all(d is not None for d in durations) else None,
        }
    return out
