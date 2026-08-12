"""Accession and side identifiers.

These strings are not internal keys that happen to be readable — they are the
*physical* identifiers of the collection. ``GM-0142`` is printed on the 4x6 card
and encoded in its Code 128 barcode; ``GM-0142-D1-A`` is the stem of the FLAC
file holding that side's archival master. They are also the primary keys in
Postgres, so a row, a card, a barcode scan, and a filename all say the same thing.

Format::

    GM-0142-D1-A.flac
    │   │    │  └─ side (A/B)
    │   │    └──── disc within the set (ALWAYS emitted, even for single-disc sets)
    │   └───────── accession number, 4-digit zero-padded
    └───────────── collection prefix

Two rules the rest of the system depends on:

* The accession number identifies a **set** (one jacket), not a disc.
* ``-D1`` is always emitted. There is no special case for single-disc sets
  anywhere in any code path.

The number is sequential and deliberately *meaningless*: it says where a record
lives on the shelf, never what it is. Genre, artist, and decade are queries
against the database, never substrings of an ID — which is what makes regrouping
the collection a query rather than a re-labelling project.
"""

from __future__ import annotations

import re
from typing import Final

#: Accession numbers are zero-padded to 4 digits, so the format tops out here.
#: Exceeding this is a schema migration *and* a new card layout, not an accident
#: to absorb silently — the DB carries a matching CHECK constraint.
MAX_ACCESSION_NUMBER: Final[int] = 9999

#: An accession id: collection prefix + 4-digit number, e.g. ``GM-0142``.
AccessionId = str
#: A disc-side id: accession + disc + side, e.g. ``GM-0142-D1-A``.
SideId = str

_ACCESSION_RE: Final = re.compile(r"^(?P<collection>[A-Z]{2,4})-(?P<number>\d{4})$")
_SIDE_RE: Final = re.compile(
    r"^(?P<accession>[A-Z]{2,4}-\d{4})-D(?P<disc>\d+)-(?P<side>[AB])$"
)


def format_accession_id(collection: str, number: int) -> AccessionId:
    """Build an accession id, e.g. ``('GM', 142) -> 'GM-0142'``."""
    if not re.fullmatch(r"[A-Z]{2,4}", collection):
        raise ValueError(f"collection prefix must be 2-4 uppercase letters: {collection!r}")
    if not 1 <= number <= MAX_ACCESSION_NUMBER:
        raise ValueError(f"accession number out of range 1..{MAX_ACCESSION_NUMBER}: {number}")
    return f"{collection}-{number:04d}"


def parse_accession_id(accession_id: str) -> tuple[str, int]:
    """Split an accession id into ``(collection, number)``."""
    m = _ACCESSION_RE.match(accession_id)
    if m is None:
        raise ValueError(f"not a valid accession id: {accession_id!r}")
    return m["collection"], int(m["number"])


def format_side_id(accession_id: str, disc: int, side: str) -> SideId:
    """Build a side id, e.g. ``('GM-0142', 1, 'A') -> 'GM-0142-D1-A'``.

    ``-D1`` is emitted for single-disc sets too; there is no special case.
    """
    parse_accession_id(accession_id)  # validate, raises on malformed input
    if disc < 1:
        raise ValueError(f"disc must be >= 1: {disc}")
    if side not in ("A", "B"):
        raise ValueError(f"side must be 'A' or 'B': {side!r}")
    return f"{accession_id}-D{disc}-{side}"


def parse_side_id(side_id: str) -> tuple[AccessionId, int, str]:
    """Split a side id into ``(accession_id, disc, side)``.

    Validation matches :func:`format_side_id` exactly — anything this accepts,
    that function would produce. Discs are 1-based, so ``-D0-`` is rejected here
    as well as there.
    """
    m = _SIDE_RE.match(side_id)
    if m is None:
        raise ValueError(f"not a valid side id: {side_id!r}")
    disc = int(m["disc"])
    if disc < 1:
        raise ValueError(f"disc must be >= 1: {side_id!r}")
    return m["accession"], disc, m["side"]
