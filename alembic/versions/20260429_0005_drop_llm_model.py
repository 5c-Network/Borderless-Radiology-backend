"""drop llm_model column from grading_jobs

The column tracked which Gemini model produced the grade (audit field).
Decided to remove it — model identity isn't needed downstream and
llm_raw_json already captures the full LLM output for audit.

Revision ID: 20260429_0005
Revises: 20260429_0004
Create Date: 2026-04-29
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260429_0005"
down_revision: Union[str, Sequence[str], None] = "20260429_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "rad_incubation"


def upgrade() -> None:
    op.drop_column("grading_jobs", "llm_model", schema=SCHEMA)


def downgrade() -> None:
    op.add_column(
        "grading_jobs",
        sa.Column("llm_model", sa.String(64), nullable=True),
        schema=SCHEMA,
    )
