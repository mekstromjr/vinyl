"""initial schema: collection, release, accession, disc_side, capture, asset

Revision ID: 0001
Revises:
Create Date: 2026-08-12

Ticket meklab/vinyl#1.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "collection",
        sa.Column("prefix", sa.String(4), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("next_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.PrimaryKeyConstraint("prefix", name="pk_collection"),
        sa.CheckConstraint("prefix ~ '^[A-Z]{2,4}$'", name="prefix_format"),
        sa.CheckConstraint("next_number >= 1", name="next_number_positive"),
    )

    op.create_table(
        "release",
        # autoincrement=False: this id comes FROM Discogs. Left to its default,
        # SQLAlchemy makes an integer primary key a BIGSERIAL and the database
        # would happily invent release ids of its own.
        sa.Column("discogs_release_id", sa.BigInteger(), nullable=False, autoincrement=False),
        sa.Column("musicbrainz_release_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("artist", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("catalog_number", sa.Text(), nullable=True),
        sa.Column("country", sa.Text(), nullable=True),
        sa.Column("year", sa.SmallInteger(), nullable=True),
        sa.Column("tracklist", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("discogs_raw", postgresql.JSONB(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.PrimaryKeyConstraint("discogs_release_id", name="pk_release"),
        sa.CheckConstraint("year BETWEEN 1880 AND 2100", name="year_plausible"),
    )
    op.create_index("ix_release_catalog_number", "release", ["catalog_number"])
    op.create_index("ix_release_musicbrainz_release_id", "release", ["musicbrainz_release_id"])

    op.create_table(
        "accession",
        sa.Column("accession_id", sa.String(16), nullable=False),
        sa.Column("collection", sa.String(4), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("disc_count", sa.SmallInteger(), nullable=False),
        sa.Column("has_jacket", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("has_inner_sleeve", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("condition_notes", sa.Text(), nullable=True),
        sa.Column("release_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="inventoried"),
        sa.Column(
            "inventoried_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.PrimaryKeyConstraint("accession_id", name="pk_accession"),
        sa.ForeignKeyConstraint(
            ["collection"], ["collection.prefix"], name="fk_accession_collection_collection"
        ),
        sa.ForeignKeyConstraint(
            ["release_id"],
            ["release.discogs_release_id"],
            name="fk_accession_release_id_release",
        ),
        sa.UniqueConstraint("collection", "number", name="uq_accession_collection_number"),
        sa.CheckConstraint("disc_count >= 1", name="disc_count_positive"),
        sa.CheckConstraint("number BETWEEN 1 AND 9999", name="number_in_range"),
        sa.CheckConstraint(
            "accession_id = collection || '-' || lpad(number::text, 4, '0')",
            name="id_matches_parts",
        ),
        sa.CheckConstraint(
            "(status = 'identified' AND release_id IS NOT NULL) "
            "OR (status = 'unidentifiable' AND release_id IS NULL) "
            "OR (status IN ('inventoried', 'excluded'))",
            name="status_agrees_with_release",
        ),
    )
    op.create_index("ix_accession_release_id", "accession", ["release_id"])
    op.create_index("ix_accession_status", "accession", ["status"])

    op.create_table(
        "disc_side",
        sa.Column("side_id", sa.String(24), nullable=False),
        sa.Column("accession_id", sa.String(16), nullable=False),
        sa.Column("disc", sa.SmallInteger(), nullable=False),
        sa.Column("side", sa.String(1), nullable=False),
        sa.Column("speed_rpm", sa.SmallInteger(), nullable=False),
        sa.Column("track_positions", postgresql.JSONB(), nullable=True),
        sa.Column("expected_runtime_sec", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("skip_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.PrimaryKeyConstraint("side_id", name="pk_disc_side"),
        sa.ForeignKeyConstraint(
            ["accession_id"],
            ["accession.accession_id"],
            name="fk_disc_side_accession_id_accession",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("accession_id", "disc", "side", name="uq_disc_side_coordinates"),
        sa.CheckConstraint("disc >= 1", name="disc_positive"),
        sa.CheckConstraint("side IN ('A', 'B')", name="side_is_a_or_b"),
        sa.CheckConstraint("speed_rpm IN (33, 45, 78)", name="speed_known"),
        sa.CheckConstraint(
            "expected_runtime_sec > 0", name="expected_runtime_positive"
        ),
        sa.CheckConstraint(
            "side_id = accession_id || '-D' || disc::text || '-' || side",
            name="id_matches_parts",
        ),
    )
    op.create_index("ix_disc_side_accession_id", "disc_side", ["accession_id"])
    op.create_index(
        "ix_disc_side_queue",
        "disc_side",
        ["speed_rpm", "status"],
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.create_table(
        "capture",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("side_id", sa.String(24), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("supersedes", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="recording"),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("integrity", postgresql.JSONB(), nullable=True),
        sa.Column("qc_verdict", sa.String(20), nullable=True),
        sa.Column("qc", postgresql.JSONB(), nullable=True),
        sa.Column("lead_in_noise_floor_dbfs", postgresql.REAL(), nullable=True),
        sa.Column("clicks_per_min", postgresql.REAL(), nullable=True),
        sa.Column("peak_dbfs", postgresql.REAL(), nullable=True),
        sa.Column("duration_delta_sec", postgresql.REAL(), nullable=True),
        sa.Column("s3_bucket", sa.Text(), nullable=True),
        sa.Column("s3_key", sa.Text(), nullable=True),
        sa.Column("local_path", sa.Text(), nullable=True),
        sa.Column("bytes", sa.BigInteger(), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("sample_rate", sa.Integer(), nullable=True),
        sa.Column("bit_depth", sa.SmallInteger(), nullable=True),
        sa.Column("duration_sec", postgresql.REAL(), nullable=True),
        sa.Column("upload_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("upload_error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.PrimaryKeyConstraint("id", name="pk_capture"),
        sa.ForeignKeyConstraint(
            ["side_id"],
            ["disc_side.side_id"],
            name="fk_capture_side_id_disc_side",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes"], ["capture.id"], name="fk_capture_supersedes_capture"
        ),
        sa.CheckConstraint("upload_attempts >= 0", name="upload_attempts_non_negative"),
        sa.CheckConstraint("bytes > 0", name="bytes_positive"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_format"),
        sa.CheckConstraint(
            "status <> 'uploaded' OR (s3_bucket IS NOT NULL AND s3_key IS NOT NULL)",
            name="uploaded_has_object",
        ),
    )
    op.create_index("ix_capture_side_id", "capture", ["side_id"])
    # Exactly one current capture per side: "which file is the master" enforced by
    # the database rather than by application logic.
    op.create_index(
        "uq_capture_current_per_side",
        "capture",
        ["side_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )
    op.create_index(
        "ix_capture_upload_queue",
        "capture",
        ["started_at"],
        postgresql_where=sa.text("status = 'captured'"),
    )
    op.create_index(
        "ix_capture_qc_queue",
        "capture",
        ["qc_verdict"],
        postgresql_where=sa.text("is_current"),
    )

    op.create_table(
        "asset",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("accession_id", sa.String(16), nullable=False),
        sa.Column("side_id", sa.String(24), nullable=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("s3_bucket", sa.Text(), nullable=False),
        sa.Column("s3_key", sa.Text(), nullable=False),
        sa.Column("content_type", sa.String(128), nullable=True),
        sa.Column("bytes", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.PrimaryKeyConstraint("id", name="pk_asset"),
        sa.ForeignKeyConstraint(
            ["accession_id"],
            ["accession.accession_id"],
            name="fk_asset_accession_id_accession",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["side_id"],
            ["disc_side.side_id"],
            name="fk_asset_side_id_disc_side",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("bytes > 0", name="bytes_positive"),
        sa.CheckConstraint(
            "kind <> 'runout' OR side_id IS NOT NULL", name="runout_names_a_side"
        ),
    )
    op.create_index("ix_asset_accession_id", "asset", ["accession_id"])
    op.create_index("ix_asset_side_id", "asset", ["side_id"])

    # updated_at is maintained DB-side so it holds for every writer: the API, the
    # capture worker, and the QC job.
    op.execute(
        """
        CREATE FUNCTION set_updated_at() RETURNS trigger AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    for table in ("release", "accession", "disc_side", "capture"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION set_updated_at();"
        )

    # The read model both UIs query. Side state is DERIVED from the current
    # capture, so there is no second copy of the truth to drift.
    op.execute(
        """
        CREATE VIEW v_side_status AS
        SELECT
            ds.side_id,
            ds.accession_id,
            ds.disc,
            ds.side,
            ds.speed_rpm,
            ds.expected_runtime_sec,
            a.status        AS accession_status,
            r.artist,
            r.title,
            CASE
                WHEN ds.status = 'skipped'  THEN 'skipped'
                WHEN c.id IS NULL           THEN 'pending'
                WHEN c.status <> 'uploaded' THEN c.status
                WHEN c.qc_verdict IS NULL   THEN 'qc_pending'
                ELSE 'qc_' || c.qc_verdict
            END             AS state,
            c.id            AS current_capture_id,
            c.failure_reason,
            c.qc_verdict,
            c.lead_in_noise_floor_dbfs,
            c.clicks_per_min,
            c.peak_dbfs,
            c.duration_delta_sec,
            c.finished_at   AS captured_at
        FROM disc_side ds
        JOIN accession a  ON a.accession_id = ds.accession_id
        LEFT JOIN release r ON r.discogs_release_id = a.release_id
        LEFT JOIN capture c ON c.side_id = ds.side_id AND c.is_current;
        """
    )

    op.execute(
        "INSERT INTO collection (prefix, name) VALUES ('GM', 'Grandmother''s collection');"
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_side_status;")
    for table in ("release", "accession", "disc_side", "capture"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_updated_at ON {table};")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at();")
    op.drop_table("asset")
    op.drop_table("capture")
    op.drop_table("disc_side")
    op.drop_table("accession")
    op.drop_table("release")
    op.drop_table("collection")
