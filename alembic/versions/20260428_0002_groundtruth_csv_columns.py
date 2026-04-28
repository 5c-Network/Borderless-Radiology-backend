"""add CSV-source columns to Study_Groundtruth

Adds five nullable columns sourced from borderless_db.csv:
  - old_study_iuid : legacy IUID from CSV. The new (yotta-pushed) study_iuid
                     is filled in by a separate extraction step; this
                     migration leaves the existing study_iuid column
                     untouched.
  - category       : Critical / Significant / Subtle / Normal
  - observation    : detailed findings text from the GT report
  - impression     : impression / conclusion text from the GT report
  - age            : raw age+sex string (e.g. "050Y_F", "44_F")

Purely additive. Does NOT modify study_iuid or any existing column /
constraint. Safe to apply on a populated table.

Revision ID: 20260428_0002
Revises: 20260424_0001
Create Date: 2026-04-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260428_0002"
down_revision: Union[str, Sequence[str], None] = "20260424_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "rad_incubation"


def upgrade() -> None:
    op.add_column(
        "Study_Groundtruth",
        sa.Column("old_study_iuid", sa.String(255), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "Study_Groundtruth",
        sa.Column("category", sa.String(20), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "Study_Groundtruth",
        sa.Column("observation", sa.Text(), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "Study_Groundtruth",
        sa.Column("impression", sa.Text(), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "Study_Groundtruth",
        sa.Column("age", sa.String(16), nullable=True),
        schema=SCHEMA,
    )

    op.create_index(
        "ix_sg_old_study_iuid",
        "Study_Groundtruth",
        ["old_study_iuid"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_sg_category",
        "Study_Groundtruth",
        ["category"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index("ix_sg_category", table_name="Study_Groundtruth", schema=SCHEMA)
    op.drop_index("ix_sg_old_study_iuid", table_name="Study_Groundtruth", schema=SCHEMA)
    op.drop_column("Study_Groundtruth", "age", schema=SCHEMA)
    op.drop_column("Study_Groundtruth", "impression", schema=SCHEMA)
    op.drop_column("Study_Groundtruth", "observation", schema=SCHEMA)
    op.drop_column("Study_Groundtruth", "category", schema=SCHEMA)
    op.drop_column("Study_Groundtruth", "old_study_iuid", schema=SCHEMA)
