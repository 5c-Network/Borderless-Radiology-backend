"""rad_slot_status feature: add other_details columns to rad_state and
create rad_slot_status table.

Idempotent: prod was patched directly via DBeaver, so this migration uses
IF NOT EXISTS / CREATE OR REPLACE so it's a no-op there but actually
provisions fresh environments (staging, CI, local).

Schema delta:
  rad_incubation.rad_state
    + other_details              text NULL
    + other_details_updated_at   timestamptz NULL
    + trigger that stamps other_details_updated_at when other_details
      is set or changed (skips no-op writes via IS DISTINCT FROM)

  rad_incubation.rad_slot_status (new table)
    rad_fk                text
    slot_date             text
    slot_name             text
    start_hour            text
    end_hour              text
    committed_hours       text
    committed_days        text
    total_active_minutes  text
    compliance_status     text   -- 'present' | 'absent'
    rad_availability      text   -- 'full' | 'partial' | 'nil'
    last_updated_at       timestamptz default now()
    PRIMARY KEY (rad_fk, slot_date, start_hour)

Revision ID: 20260505_0008
Revises: 20260502_0007
Create Date: 2026-05-05
"""
from typing import Sequence, Union

from alembic import op

revision: str = "20260505_0008"
down_revision: Union[str, Sequence[str], None] = "20260502_0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "rad_incubation"


def upgrade() -> None:
    # --- rad_state additions ------------------------------------------------
    op.execute(
        f"""
        ALTER TABLE {SCHEMA}.rad_state
            ADD COLUMN IF NOT EXISTS other_details text NULL;
        """
    )
    op.execute(
        f"""
        ALTER TABLE {SCHEMA}.rad_state
            ADD COLUMN IF NOT EXISTS other_details_updated_at timestamptz NULL;
        """
    )

    # Trigger: stamp other_details_updated_at when other_details is
    # set/changed. IS DISTINCT FROM ignores no-op writes (same JSON re-saved).
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.trg_set_other_details_updated_at()
        RETURNS trigger AS $$
        BEGIN
            IF (TG_OP = 'INSERT' AND NEW.other_details IS NOT NULL)
               OR (TG_OP = 'UPDATE'
                   AND NEW.other_details IS DISTINCT FROM OLD.other_details
                   AND NEW.other_details IS NOT NULL) THEN
                NEW.other_details_updated_at := now();
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        f"""
        DROP TRIGGER IF EXISTS set_other_details_updated_at
            ON {SCHEMA}.rad_state;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER set_other_details_updated_at
            BEFORE INSERT OR UPDATE ON {SCHEMA}.rad_state
            FOR EACH ROW
            EXECUTE FUNCTION {SCHEMA}.trg_set_other_details_updated_at();
        """
    )

    # --- rad_slot_status table ---------------------------------------------
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA}.rad_slot_status (
            rad_fk                text NOT NULL,
            slot_date             text NOT NULL,
            slot_name             text NOT NULL,
            start_hour            text NOT NULL,
            end_hour              text NOT NULL,
            committed_hours       text NOT NULL,
            committed_days        text NOT NULL,
            total_active_minutes  text NOT NULL,
            compliance_status     text NOT NULL,
            rad_availability      text NOT NULL,
            created_at            timestamptz NOT NULL DEFAULT now(),
            last_updated_at       timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT rad_slot_status_pkey
                PRIMARY KEY (rad_fk, slot_date, start_hour)
        );
        """
    )
    # If the table pre-existed without created_at (e.g., DBeaver-applied
    # earlier schema), patch it in idempotently.
    op.execute(
        f"""
        ALTER TABLE {SCHEMA}.rad_slot_status
            ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();
        """
    )
    op.execute(
        f"""
        CREATE INDEX IF NOT EXISTS ix_rad_slot_status_slot_date
            ON {SCHEMA}.rad_slot_status (slot_date);
        """
    )


def downgrade() -> None:
    # Reverse-order, defensive drops.
    op.execute(f"DROP TABLE IF EXISTS {SCHEMA}.rad_slot_status;")
    op.execute(
        f"DROP TRIGGER IF EXISTS set_other_details_updated_at ON {SCHEMA}.rad_state;"
    )
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.trg_set_other_details_updated_at();")
    op.execute(
        f"ALTER TABLE {SCHEMA}.rad_state DROP COLUMN IF EXISTS other_details_updated_at;"
    )
    op.execute(
        f"ALTER TABLE {SCHEMA}.rad_state DROP COLUMN IF EXISTS other_details;"
    )
