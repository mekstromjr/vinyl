"""Who this station is, and what is plugged into it.

Everything here is best-effort by construction: a station whose hardware probe
raises must still register and still capture. A probe failure is a missing
field, never an exception — losing the station from the cluster's view because
``/proc/asound`` looked unfamiliar would be a worse outcome than an unknown
capability.
"""

from __future__ import annotations

import re
import socket
import subprocess
from pathlib import Path

#: Written by deploy.sh from the deploying checkout. The station holds a plain
#: rsync with no .git, so without this stamp there is no way to tell which code
#: is actually running — and deploys do not necessarily match HEAD.
_VERSION_STAMP = Path("/opt/vinyl-capture/.deployed-version")

#: Matches the server's ck_station_station_id_format check constraint. Enforced
#: here too so a bad hostname fails loudly at startup rather than as an opaque
#: 500 from the database on first registration.
_STATION_ID_RE = re.compile(r"^[a-zA-Z0-9._-]{1,64}$")


def hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return "unknown"


def station_id(configured: str | None = None) -> str:
    """Stable identity for this station.

    Defaults to the hostname rather than a generated UUID: the id shows up in
    the station list and in support conversations, so "<station-host>" beats an
    opaque identifier. It also survives a reinstall without a state file to
    preserve, which a UUID would not.
    """
    raw = (configured or hostname()).strip()
    # Hostnames may be FQDNs; the leading label is the useful part and keeps
    # the id inside the 64-char limit.
    raw = raw.split(".")[0] if raw.count(".") and len(raw) > 64 else raw
    if not _STATION_ID_RE.match(raw):
        raise RuntimeError(
            f"station id {raw!r} is not usable as an identifier — "
            f"set VINYL_STATION_ID to something matching [a-zA-Z0-9._-]{{1,64}}"
        )
    return raw


def agent_version() -> str | None:
    try:
        return _VERSION_STAMP.read_text().strip()[:32] or None
    except OSError:
        return None


def _ffmpeg_version() -> str | None:
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-version"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    first = out.splitlines()[0] if out else ""
    m = re.search(r"ffmpeg version (\S+)", first)
    return m.group(1) if m else None


def _alsa_cards() -> dict[str, str]:
    """Map ALSA card name -> description, as ffmpeg's ``hw:<name>`` resolves it.

    /proc/asound/cards lines look like::

         1 [USB            ]: USB-Audio - Scarlett 2i2 USB
    """
    cards: dict[str, str] = {}
    try:
        text = Path("/proc/asound/cards").read_text()
    except OSError:
        return cards
    for line in text.splitlines():
        m = re.match(r"\s*\d+\s+\[(\S+)\s*\]:\s*(.+)", line)
        if m:
            cards[m.group(1)] = m.group(2).strip()
    return cards


def _optical_drives() -> list[str]:
    try:
        return sorted(str(p) for p in Path("/dev").glob("sr[0-9]*"))
    except OSError:
        return []


def capabilities(alsa_device: str) -> dict:
    """What this station can actually do, probed at registration.

    ``alsa_device`` is the configured capture device (e.g. ``hw:USB``). We report
    whether it is *currently present*, which is the difference between "this
    station cannot capture vinyl" and "someone unplugged the interface" — the
    laptop legitimately has the Scarlett unplugged whenever an optical drive is
    in its place.
    """
    cards = _alsa_cards()
    wanted = alsa_device.split(":", 1)[1].split(",")[0] if ":" in alsa_device else alsa_device
    optical = _optical_drives()
    return {
        "alsa_device": alsa_device,
        "alsa_device_present": wanted in cards,
        "alsa_device_description": cards.get(wanted),
        "alsa_cards": cards,
        "optical_drives": optical,
        "can_capture_vinyl": wanted in cards,
        "can_rip_optical": bool(optical),
        "ffmpeg_version": _ffmpeg_version(),
    }
