"""Drop study_id column from grading_jobs.

The column was being populated from Study_Groundtruth.study_id, which
conflated two distinct ID spaces — the GT pool's internal PK and the
candidate-side runtime study_id sent in /grade_case payloads. The
candidate-side value is preserved inside grading_jobs.raw_payload
(report.study_id) and can be extracted on demand via
raw_payload->'report'->>'study_id'. The candidate↔GT join key remains
study_iuid.

Revision ID: 20260506_0010
Revises: 20260505_0009
Create Date: 2026-05-06
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260506_0010"
down_revision: Union[str, Sequence[str], None] = "20260505_0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "rad_incubation"


def upgrade() -> None:
    op.execute(
        f"ALTER TABLE {SCHEMA}.grading_jobs DROP COLUMN IF EXISTS study_id;"
    )


def downgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE {SCHEMA}.grading_jobs
            ADD COLUMN IF NOT EXISTS study_id integer;
        """
    )
