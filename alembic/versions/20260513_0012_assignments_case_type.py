"""add case_type column to case_assignments + backfill from Study_Groundtruth

Snapshot the mod-study bucket on the assignment row at write time so the
audit trail survives later re-tagging of Study_Groundtruth.case_type.
Parallel to the existing is_complex snapshot on the same row.

Nullable. Backfill UPDATE joins on study_iuid. Idempotent: only updates
rows whose case_type is still NULL.

Revision ID: 20260513_0012
Revises: 20260513_0011
Create Date: 2026-05-13
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260513_0012"
down_revision: Union[str, Sequence[str], None] = "20260513_0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "rad_incubation"


def upgrade() -> None:
    op.add_column(
        "case_assignments",
        sa.Column("case_type", sa.String(20), nullable=True),
        schema=SCHEMA,
    )
    op.execute(
        """
        UPDATE rad_incubation.case_assignments AS a
        SET case_type = sg.case_type
        FROM rad_incubation."Study_Groundtruth" AS sg
        WHERE a.study_iuid = sg.study_iuid
          AND a.case_type IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column("case_assignments", "case_type", schema=SCHEMA)
