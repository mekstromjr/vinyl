from __future__ import annotations

import pytest
from vinyl_shared.states import (
    CAPTURE_TRANSITIONS,
    CaptureStatus,
    InvalidTransition,
    Speed,
    can_transition,
    ensure_transition,
)


def test_speed_is_numeric():
    """speed_rpm is a quantity (SMALLINT), not a status string."""
    assert int(Speed.RPM_33) == 33
    assert Speed.RPM_78 > Speed.RPM_45 > Speed.RPM_33


def test_happy_path():
    assert can_transition(CaptureStatus.RECORDING, CaptureStatus.CAPTURED)
    assert can_transition(CaptureStatus.CAPTURED, CaptureStatus.UPLOADED)


def test_upload_trouble_cannot_demote_a_good_recording():
    """A failed upload is a retry loop, never a lifecycle regression."""
    assert not can_transition(CaptureStatus.CAPTURED, CaptureStatus.FAILED)
    assert not can_transition(CaptureStatus.CAPTURED, CaptureStatus.RECORDING)


def test_failed_is_terminal():
    """A failed attempt is superseded by a NEW row, never revived, so the
    history of what was tried stays intact."""
    assert CAPTURE_TRANSITIONS[CaptureStatus.FAILED] == frozenset()


def test_uploaded_is_terminal():
    assert CAPTURE_TRANSITIONS[CaptureStatus.UPLOADED] == frozenset()


def test_every_status_has_a_transition_entry():
    assert set(CAPTURE_TRANSITIONS) == set(CaptureStatus)


def test_ensure_transition_raises_on_illegal():
    with pytest.raises(InvalidTransition):
        ensure_transition(CaptureStatus.UPLOADED, CaptureStatus.RECORDING)


def test_ensure_transition_returns_destination():
    assert (
        ensure_transition(CaptureStatus.RECORDING, CaptureStatus.CAPTURED)
        is CaptureStatus.CAPTURED
    )
