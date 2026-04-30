"""add 'skipped' value to callback_status enum

Used when a checkpoint fires with no platform PATCH required (currently
gate_20 with overall grade 1 — the rad continues silently).

Revision ID: 20260430_0006
Revises: 20260429_0005
Create Date: 2026-04-30
"""
from typing import Sequence, Union

from alembic import op

revision: str = "20260430_0006"
down_revision: Union[str, Sequence[str], None] = "20260429_0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "rad_incubation"


def upgrade() -> None:
    op.execute(
        f"ALTER TYPE {SCHEMA}.callback_status ADD VALUE IF NOT EXISTS 'skipped'"
    )


def downgrade() -> None:
    # PostgreSQL does not support removing enum values without recreating the
    # type. Leaving the value in place on downgrade is the standard approach.
    pass
