"""accession.condition_grade: five-stop condition scale (mint..poor)

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-18

Nullable on purpose: pre-existing accessions stay ungraded rather than being
backfilled with a guess.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "accession", sa.Column("condition_grade", sa.String(12), nullable=True)
    )
    op.create_check_constraint(
        "condition_grade_valid",
        "accession",
        "condition_grade IN ('mint', 'excellent', 'good', 'fair', 'poor')",
    )


def downgrade() -> None:
    op.drop_constraint("condition_grade_valid", "accession")
    op.drop_column("accession", "condition_grade")
