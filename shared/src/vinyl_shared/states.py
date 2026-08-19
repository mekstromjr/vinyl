"""Lifecycle states, shared by the capture worker, the QC job, and the server.

The load-bearing distinction in this module is between **recording integrity**
and **quality**, and the test that separates them is:

    *Would redoing the capture change this number?*

Yes — xruns, clipping, DC offset, a truncated file. Those are faults of the
capture chain, they are checked on the laptop in seconds, and a failure means
"redo it now, while the record is still on the platter". A capture that fails
integrity never uploads: ``CaptureStatus.FAILED``.

No — surface noise floor, click rate, groove wear. Those are faults of the
*disc*; recapturing produces the identical result. They are measured in the
cluster, asynchronously, and drive :class:`QCVerdict` plus the
"buy a commercial copy instead" flag.

Upload trouble is deliberately absent from :class:`CaptureStatus`. A failed
upload means the network is down, not that the recording is bad — the FLAC sits
on the laptop and the worker retries. Modelling it as a lifecycle state would
let a Wi-Fi hiccup prompt a pointless re-record.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Final


class Side(StrEnum):
    A = "A"
    B = "B"


class Speed(IntEnum):
    """Playback speed, in RPM. ``RPM_33`` means 33-1/3.

    An ``IntEnum`` (persisted as ``SMALLINT``) rather than the string enums used
    for statuses, because speed is a quantity: it is compared and sorted, and the
    QC job divides by it when checking for platter-speed error.

    Speed is a property of the physical disc, not of the set, and it is never a
    boolean: 78s are filtered out of the phase-one capture queue with
    ``speed != Speed.RPM_78``, not shaped into the schema as a special case.
    """

    RPM_33 = 33
    RPM_45 = 45
    RPM_78 = 78


class ConditionGrade(StrEnum):
    """Physical condition of a set, graded once at inventory time.

    Five stops on a slider, not Goldmine (M/NM/VG+/...): the archive keeps the
    FLAC master either way, so the grade is context for future-Michael, not a
    sale listing. One overall grade per set — media and jacket are not graded
    separately. Free-text ``condition_notes`` remains for the genuinely weird.
    """

    MINT = "mint"            # perfect — looks unplayed
    EXCELLENT = "excellent"  # very light wear
    GOOD = "good"            # regular wear for its age
    FAIR = "fair"            # writing/drawing, seam split, noticeable damage
    POOR = "poor"            # barely holding together, or repaired with tape


class AccessionStatus(StrEnum):
    """Identification state of a set. Distinct from capture progress."""

    INVENTORIED = "inventoried"        # entered; no Discogs release bound yet
    IDENTIFIED = "identified"          # release bound — the normal resting state
    UNIDENTIFIABLE = "unidentifiable"  # tried and gave up; capture may still proceed
    EXCLUDED = "excluded"              # deliberately not digitizing


class SideStatus(StrEnum):
    """The *only* side state that captures cannot express.

    Everything else (captured / uploaded / QC verdict) is derived from the
    current capture row via the ``v_side_status`` view, so there is no second
    copy of the truth to drift out of sync.
    """

    PENDING = "pending"
    SKIPPED = "skipped"


class CaptureStatus(StrEnum):
    """Lifecycle of one capture attempt.

    ``FAILED`` means exactly one thing: this capture is no good, redo it at the
    turntable.
    """

    RECORDING = "recording"  # row exists from the moment recording starts
    CAPTURED = "captured"    # on disk, passed integrity, awaiting upload (retried)
    UPLOADED = "uploaded"    # master is in S3
    FAILED = "failed"        # integrity rejected it, or recording died


class QCVerdict(StrEnum):
    """Authoritative quality verdict, produced in the cluster."""

    PASS = "pass"
    REVIEW = "review"
    RECAPTURE = "recapture"


class AssetKind(StrEnum):
    """Objects stored in the Garage ``vinyl`` bucket for an accession."""

    LABEL = "label"                # centre label photo
    RUNOUT = "runout"              # deadwax runout etchings — always per-SIDE
    JACKET_FRONT = "jacket_front"
    JACKET_BACK = "jacket_back"
    CARD_PDF = "card_pdf"          # the generated 4x6 card
    OTHER = "other"


#: Asset kinds that describe one side of one disc rather than the whole set.
PER_SIDE_ASSET_KINDS: Final[frozenset[AssetKind]] = frozenset({AssetKind.RUNOUT})


#: Legal capture transitions. Anything not listed is a programming error.
CAPTURE_TRANSITIONS: Final[dict[CaptureStatus, frozenset[CaptureStatus]]] = {
    CaptureStatus.RECORDING: frozenset({CaptureStatus.CAPTURED, CaptureStatus.FAILED}),
    # No CAPTURED -> FAILED edge: integrity runs before this status is reached, and
    # upload trouble must never demote a good recording.
    CaptureStatus.CAPTURED: frozenset({CaptureStatus.UPLOADED}),
    CaptureStatus.UPLOADED: frozenset(),
    # Terminal: a failed attempt is superseded by a NEW capture row, never revived,
    # so the history of what was tried stays intact.
    CaptureStatus.FAILED: frozenset(),
}


class InvalidTransition(ValueError):
    def __init__(self, src: CaptureStatus, dst: CaptureStatus) -> None:
        super().__init__(f"illegal capture transition: {src} -> {dst}")
        self.src = src
        self.dst = dst


def can_transition(src: CaptureStatus, dst: CaptureStatus) -> bool:
    return dst in CAPTURE_TRANSITIONS[src]


def ensure_transition(src: CaptureStatus, dst: CaptureStatus) -> CaptureStatus:
    if not can_transition(src, dst):
        raise InvalidTransition(src, dst)
    return dst
