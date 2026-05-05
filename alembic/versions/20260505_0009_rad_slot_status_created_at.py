"""Add created_at to rad_slot_status.

Tracks when a slot row was first written. last_updated_at already tracks
the most recent UPSERT; created_at lets us see when the cron originally
scored the slot.

Idempotent (IF NOT EXISTS) so it's a no-op if the column was added via
DBeaver beforehand.

Revision ID: 20260505_0009
Revises: 20260505_0008
Create Date: 2026-05-05
"""
from typing import Sequence, Union

from alembic import op

revision: str = "20260505_0009"
down_revision: Union[str, Sequence[str], None] = "20260505_0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "rad_incubation"


def upgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE {SCHEMA}.rad_slot_status
            ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();
        """
    )


def downgrade() -> None:
    op.execute(
        f"ALTER TABLE {SCHEMA}.rad_slot_status DROP COLUMN IF EXISTS created_at;"
    )
