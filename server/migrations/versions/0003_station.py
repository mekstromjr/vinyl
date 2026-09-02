"""station: self-registering capture stations

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-02

Presence and capability only — deliberately no command queue. The cluster can
see which machines can capture and what is attached to them; driving a station
remotely is meklab/home#178 and its transport is undecided, so modelling it
here would pre-empt that.

`phase` is an unconstrained String on purpose. The agent and the server deploy
independently (deploy.sh vs Flux), so a new agent reporting an unfamiliar phase
must not fail its heartbeat and drop the station out of view.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "station",
        sa.Column("station_id", sa.String(64), primary_key=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("hostname", sa.Text(), nullable=True),
        sa.Column("capabilities", JSONB(), nullable=True),
        sa.Column("agent_version", sa.String(32), nullable=True),
        sa.Column("phase", sa.String(20), nullable=True),
        sa.Column("current_side_id", sa.String(24), nullable=True),
        sa.Column("pending_uploads", sa.Integer(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # Bare names: the metadata naming convention prefixes these to
        # ck_station_<name>. Passing an already-prefixed name double-prefixes it
        # and leaves the model and the schema permanently disagreeing.
        sa.CheckConstraint(
            "station_id ~ '^[a-zA-Z0-9._-]{1,64}$'", name="station_id_format"
        ),
        sa.CheckConstraint(
            "pending_uploads IS NULL OR pending_uploads >= 0",
            name="pending_uploads_non_negative",
        ),
    )
    # Same shared trigger the other mutable tables use (created in 0001), so
    # updated_at holds no matter which client writes the row.
    op.execute(
        "CREATE TRIGGER trg_station_updated_at BEFORE UPDATE ON station "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at();"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_station_updated_at ON station;")
    op.drop_table("station")
