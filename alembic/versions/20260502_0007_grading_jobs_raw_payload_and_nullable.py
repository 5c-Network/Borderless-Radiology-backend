"""grading_jobs: add raw_payload column, make study_id and case_number nullable

The /grade_case endpoint now persists every inbound request immediately on
receipt — including those that fail validation (no assignment, no groundtruth
row). Two schema changes support this:

- raw_payload (JSONB, nullable): full incoming request body, written 1:1 as
  an audit record. Distinct from candidate_snapshot, which keeps its existing
  4-field LLM-input shape.
- study_id and case_number become nullable: validation-failure rows are
  inserted before either lookup runs, so neither column can be populated.

Revision ID: 20260502_0007
Revises: 20260430_0006
Create Date: 2026-05-02
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260502_0007"
down_revision: Union[str, Sequence[str], None] = "20260430_0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "rad_incubation"


def upgrade() -> None:
    op.add_column(
        "grading_jobs",
        sa.Column("raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        schema=SCHEMA,
    )
    op.alter_column(
        "grading_jobs", "study_id", existing_type=sa.Integer(), nullable=True, schema=SCHEMA
    )
    op.alter_column(
        "grading_jobs", "case_number", existing_type=sa.Integer(), nullable=True, schema=SCHEMA
    )


def downgrade() -> None:
    # Reverting nullability requires every existing row to have non-null
    # study_id and case_number. Validation-failure rows inserted by the new
    # /grade_case flow violate this — delete or backfill before downgrading.
    op.alter_column(
        "grading_jobs", "case_number", existing_type=sa.Integer(), nullable=False, schema=SCHEMA
    )
    op.alter_column(
        "grading_jobs", "study_id", existing_type=sa.Integer(), nullable=False, schema=SCHEMA
    )
    op.drop_column("grading_jobs", "raw_payload", schema=SCHEMA)
