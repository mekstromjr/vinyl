"""Schema-shape tests.

These run against ``Base.metadata`` rather than a live database, so they need no
Postgres in CI. They guard the structural decisions that are easy to erode in a
later edit — natural keys, the single-current-capture index, nullable release
binding — rather than re-testing SQLAlchemy itself.
"""

from __future__ import annotations

from vinyl_server.db.models import Accession, Asset, Base, Capture, DiscSide


def test_all_tables_present():
    assert set(Base.metadata.tables) == {
        "collection",
        "release",
        "accession",
        "disc_side",
        "capture",
        "asset",
    }


def test_natural_keys_are_the_primary_keys():
    """The printed/scanned/filename string IS the key — no surrogate UUID."""
    assert [c.name for c in Accession.__table__.primary_key] == ["accession_id"]
    assert [c.name for c in DiscSide.__table__.primary_key] == ["side_id"]


def test_release_binding_is_nullable():
    """Inventory produces discs that cannot be matched."""
    assert Accession.__table__.c.release_id.nullable is True


def test_exactly_one_current_capture_per_side_is_enforced_by_an_index():
    idx = {i.name: i for i in Capture.__table__.indexes}["uq_capture_current_per_side"]
    assert idx.unique is True
    assert [c.name for c in idx.columns] == ["side_id"]
    assert idx.dialect_options["postgresql"]["where"] == "is_current"


def test_speed_is_on_the_physical_side_not_the_set():
    assert "speed_rpm" in DiscSide.__table__.c
    assert "speed_rpm" not in Accession.__table__.c
    assert DiscSide.__table__.c.speed_rpm.nullable is False


def test_no_is_78_boolean_anywhere():
    for table in Base.metadata.tables.values():
        assert "is_78" not in table.c


def test_qc_metrics_promoted_out_of_the_blob_are_real_columns():
    """These are sorted and thresholded across the collection, which JSONB does poorly."""
    for column in (
        "lead_in_noise_floor_dbfs",
        "clicks_per_min",
        "peak_dbfs",
        "duration_delta_sec",
    ):
        assert column in Capture.__table__.c


def test_capture_has_no_station_or_operator_columns():
    """One station, one operator — a user picker is friction on a kiosk."""
    assert "station" not in Capture.__table__.c
    assert "captured_by" not in Capture.__table__.c


def test_upload_is_a_retry_loop_not_a_lifecycle_state():
    assert "upload_attempts" in Capture.__table__.c
    assert "upload_error" in Capture.__table__.c


def test_asset_side_is_nullable_for_set_level_photos():
    """A jacket photo belongs to the set; a runout etching belongs to one side."""
    assert Asset.__table__.c.side_id.nullable is True


def _check_names(table):
    return {c.name for c in table.constraints if c.name}


def test_named_constraints_follow_the_naming_convention():
    """Stable names keep Alembic autogenerate diffs clean."""
    assert "ck_accession_id_matches_parts" in _check_names(Accession.__table__)
    assert "ck_disc_side_id_matches_parts" in _check_names(DiscSide.__table__)
    assert "ck_accession_status_agrees_with_release" in _check_names(Accession.__table__)
