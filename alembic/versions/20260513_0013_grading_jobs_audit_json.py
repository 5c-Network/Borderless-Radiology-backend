"""add audit_json column to grading_jobs

Free-form JSONB audit trail per grading row. Expected shape (not enforced):
  {
    "report_fk": int,
    "revised_report_fk": int,
    "delta": str,            # llm summary
    "severity": str,         # e.g. "minor"
    "grade": str,            # e.g. "2a"
    "overcall_type": str,    # e.g. "CLIENT_OVERCALL"
    "status": str,           # e.g. "STARTED"
    "type": str,             # "missed" | "overcalled"
    "dispute": Any | null,
    "severity_history": Any
  }

Nullable. No backfill.

Revision ID: 20260513_0013
Revises: 20260513_0012
Create Date: 2026-05-13
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "20260513_0013"
down_revision: Union[str, Sequence[str], None] = "20260513_0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "rad_incubation"


def upgrade() -> None:
    op.add_column(
        "grading_jobs",
        sa.Column("audit_json", JSONB, nullable=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("grading_jobs", "audit_json", schema=SCHEMA)
