"""initial schema for borderless incubation backend

Creates schema "rad_incubation" and all tables + enums under it.

Revision ID: 20260424_0001
Revises:
Create Date: 2026-04-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260424_0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "rad_incubation"
KOLKATA_NOW = sa.text("(now() AT TIME ZONE 'Asia/Kolkata')")


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"')

    # ---- enums ----
    rad_status = postgresql.ENUM(
        "in_progress",
        "completed_80",
        "timed_out_7_days",
        "suspended_at_20",
        name="rad_status",
        schema=SCHEMA,
    )
    grading_status = postgresql.ENUM(
        "queued", "running", "done", "error",
        name="grading_status", schema=SCHEMA,
    )
    checkpoint_kind = postgresql.ENUM(
        "gate_20", "terminal_80", "terminal_7_days",
        name="checkpoint_kind", schema=SCHEMA,
    )
    callback_status = postgresql.ENUM(
        "pending", "sent", "failed",
        name="callback_status", schema=SCHEMA,
    )
    for e in (rad_status, grading_status, checkpoint_kind, callback_status):
        e.create(op.get_bind(), checkfirst=True)

    # ---- Study_Groundtruth (public contract; exact DDL + our additions) ----
    op.create_table(
        "Study_Groundtruth",
        sa.Column("study_id", sa.Integer, nullable=False),
        sa.Column("study_iuid", sa.String(255), nullable=False),
        sa.Column("modstudy", sa.String(225), nullable=False),
        sa.Column("groundtruth_pathology", sa.Text, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=KOLKATA_NOW,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=KOLKATA_NOW,
        ),
        sa.Column("modality", sa.String(225)),
        sa.Column("history", sa.Text),
        sa.Column("dicom_metadata", sa.Text),
        sa.Column("rules", sa.Text),
        # Incubation additions
        sa.Column("is_complex", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column(
            "main_pathologies",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "incidental_findings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("classified_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("study_id", name="Xray_Groundtruth_pkey"),
        sa.UniqueConstraint("study_iuid", name="study_groundtruth_study_iuid_key"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_sg_is_complex",
        "Study_Groundtruth",
        ["is_complex"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_sg_classified_at",
        "Study_Groundtruth",
        ["classified_at"],
        schema=SCHEMA,
    )

    # ---- rad_state ----
    op.create_table(
        "rad_state",
        sa.Column("rad_id", sa.String(64), primary_key=True),
        sa.Column("incubation_started_at", sa.DateTime(timezone=True)),
        sa.Column(
            "status",
            postgresql.ENUM(name="rad_status", schema=SCHEMA, create_type=False),
            nullable=False,
            server_default="in_progress",
        ),
        sa.Column("cases_completed", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        schema=SCHEMA,
    )
    op.create_index("ix_rad_state_status", "rad_state", ["status"], schema=SCHEMA)
    op.create_index(
        "ix_rad_state_incubation_started_at",
        "rad_state",
        ["incubation_started_at"],
        schema=SCHEMA,
    )

    # ---- case_assignments ----
    op.create_table(
        "case_assignments",
        sa.Column(
            "assignment_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "rad_id",
            sa.String(64),
            sa.ForeignKey(f"{SCHEMA}.rad_state.rad_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("study_iuid", sa.String(255), nullable=False),
        sa.Column("study_id", sa.Integer, nullable=False),
        sa.Column("case_number", sa.Integer, nullable=False),
        sa.Column(
            "is_complex", sa.Boolean, nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "assigned_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("rad_id", "study_iuid", name="uq_rad_study_iuid"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_case_assignments_rad_id", "case_assignments", ["rad_id"], schema=SCHEMA
    )
    op.create_index(
        "ix_assignments_rad_case_number",
        "case_assignments",
        ["rad_id", "case_number"],
        schema=SCHEMA,
    )

    # ---- grading_jobs ----
    op.create_table(
        "grading_jobs",
        sa.Column(
            "grading_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "rad_id",
            sa.String(64),
            sa.ForeignKey(f"{SCHEMA}.rad_state.rad_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("study_iuid", sa.String(255), nullable=False),
        sa.Column("study_id", sa.Integer, nullable=False),
        sa.Column("case_number", sa.Integer, nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(name="grading_status", schema=SCHEMA, create_type=False),
            nullable=False,
            server_default="queued",
        ),
        sa.Column("grade", sa.String(4)),
        sa.Column("score_10pt", sa.Numeric(3, 1)),
        sa.Column("critical_miss", sa.Boolean),
        sa.Column("overcall_detected", sa.Boolean),
        sa.Column("related_to_primary_indication", sa.Boolean),
        sa.Column("llm_raw_json", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("llm_rationale", sa.Text),
        sa.Column("llm_model", sa.String(64)),
        sa.Column("ground_truth_snapshot", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("candidate_snapshot", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("error_message", sa.Text),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("graded_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("rad_id", "study_iuid", name="uq_grading_rad_study_iuid"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_grading_rad_status",
        "grading_jobs",
        ["rad_id", "status"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_grading_rad_case_number",
        "grading_jobs",
        ["rad_id", "case_number"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_grading_study_iuid", "grading_jobs", ["study_iuid"], schema=SCHEMA
    )

    # ---- checkpoint_events ----
    op.create_table(
        "checkpoint_events",
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("rad_id", sa.String(64), nullable=False),
        sa.Column(
            "kind",
            postgresql.ENUM(name="checkpoint_kind", schema=SCHEMA, create_type=False),
            nullable=False,
        ),
        sa.Column("cases_evaluated", sa.Integer, nullable=False),
        sa.Column("avg_score", sa.Numeric(4, 2), nullable=False),
        sa.Column("overall_grade", sa.String(4), nullable=False),
        sa.Column("quality_met", sa.Boolean, nullable=False),
        sa.Column(
            "grade_counts",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("summary", sa.Text, nullable=False, server_default=""),
        sa.Column(
            "callback_status",
            postgresql.ENUM(name="callback_status", schema=SCHEMA, create_type=False),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("callback_attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("callback_last_error", sa.Text),
        sa.Column("callback_payload", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("slack_sent", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("slack_last_error", sa.Text),
        sa.Column(
            "evaluated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("rad_id", "kind", name="uq_checkpoint_rad_kind"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_checkpoint_events_rad_id", "checkpoint_events", ["rad_id"], schema=SCHEMA
    )


def downgrade() -> None:
    op.drop_index("ix_checkpoint_events_rad_id", table_name="checkpoint_events", schema=SCHEMA)
    op.drop_table("checkpoint_events", schema=SCHEMA)

    op.drop_index("ix_grading_study_iuid", table_name="grading_jobs", schema=SCHEMA)
    op.drop_index("ix_grading_rad_case_number", table_name="grading_jobs", schema=SCHEMA)
    op.drop_index("ix_grading_rad_status", table_name="grading_jobs", schema=SCHEMA)
    op.drop_table("grading_jobs", schema=SCHEMA)

    op.drop_index(
        "ix_assignments_rad_case_number", table_name="case_assignments", schema=SCHEMA
    )
    op.drop_index("ix_case_assignments_rad_id", table_name="case_assignments", schema=SCHEMA)
    op.drop_table("case_assignments", schema=SCHEMA)

    op.drop_index("ix_rad_state_incubation_started_at", table_name="rad_state", schema=SCHEMA)
    op.drop_index("ix_rad_state_status", table_name="rad_state", schema=SCHEMA)
    op.drop_table("rad_state", schema=SCHEMA)

    op.drop_index("ix_sg_classified_at", table_name="Study_Groundtruth", schema=SCHEMA)
    op.drop_index("ix_sg_is_complex", table_name="Study_Groundtruth", schema=SCHEMA)
    op.drop_table("Study_Groundtruth", schema=SCHEMA)

    op.execute(f'DROP TYPE IF EXISTS "{SCHEMA}".callback_status')
    op.execute(f'DROP TYPE IF EXISTS "{SCHEMA}".checkpoint_kind')
    op.execute(f'DROP TYPE IF EXISTS "{SCHEMA}".grading_status')
    op.execute(f'DROP TYPE IF EXISTS "{SCHEMA}".rad_status')
