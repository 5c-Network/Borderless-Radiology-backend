"""add modality_preferred column to rad_state

Stores the rad's modality preference set on the first start-reporting
webhook event. Canonical form: alphabetically sorted, comma-separated
uppercase tokens drawn from {CT, MRI, XRAY, NM}.

Examples: "CT", "MRI", "CT,MRI", "NM,XRAY", "CT,MRI,NM,XRAY".

Nullable. No backfill (testing phase).

Revision ID: 20260428_0003
Revises: 20260428_0002
Create Date: 2026-04-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260428_0003"
down_revision: Union[str, Sequence[str], None] = "20260428_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "rad_incubation"


def upgrade() -> None:
    op.add_column(
        "rad_state",
        sa.Column("modality_preferred", sa.String(32), nullable=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("rad_state", "modality_preferred", schema=SCHEMA)
