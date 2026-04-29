"""add case_type column to Study_Groundtruth

Tags rows that carry pre-loaded DICOMs on the destination server (no
yotta download/anonymise hop needed). Drives the partition between:

    GET /api/v1/activation-data/      -> case_type IS DISTINCT FROM 'test'
    GET /api/v1/activation-data/test  -> case_type = 'test'

Free-form String(20) so future tags ('demo', etc.) don't need another
migration. Nullable. No backfill — every existing row stays in the
default (production) bucket.

Revision ID: 20260429_0004
Revises: 20260428_0003
Create Date: 2026-04-29
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260429_0004"
down_revision: Union[str, Sequence[str], None] = "20260428_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "rad_incubation"


def upgrade() -> None:
    op.add_column(
        "Study_Groundtruth",
        sa.Column("case_type", sa.String(20), nullable=True),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_sg_case_type",
        "Study_Groundtruth",
        ["case_type"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index("ix_sg_case_type", table_name="Study_Groundtruth", schema=SCHEMA)
    op.drop_column("Study_Groundtruth", "case_type", schema=SCHEMA)
