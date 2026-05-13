"""add report_id column to Study_Groundtruth

Source-system report identifier from upstream (ClickHouse Int32). Distinct
from the rad-submitted report_id in app.schemas.Report — this one tags the
pool row's origin report.

Nullable. No backfill — existing rows stay NULL until populated from source.

Revision ID: 20260513_0011
Revises: 20260506_0010
Create Date: 2026-05-13
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260513_0011"
down_revision: Union[str, Sequence[str], None] = "20260506_0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "rad_incubation"


def upgrade() -> None:
    op.add_column(
        "Study_Groundtruth",
        sa.Column("report_id", sa.Integer(), nullable=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("Study_Groundtruth", "report_id", schema=SCHEMA)
