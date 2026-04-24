import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

SCHEMA = "rad_incubation"


class RadStatus(str, enum.Enum):
    in_progress = "in_progress"
    completed_80 = "completed_80"
    timed_out_7_days = "timed_out_7_days"
    suspended_at_20 = "suspended_at_20"


class GradingStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    done = "done"
    error = "error"


class CheckpointKind(str, enum.Enum):
    gate_20 = "gate_20"
    terminal_80 = "terminal_80"
    terminal_7_days = "terminal_7_days"


class CallbackStatus(str, enum.Enum):
    pending = "pending"
    sent = "sent"
    failed = "failed"


class StudyGroundtruth(Base):
    """Master ground-truth pool. Every rad in incubation reads from this table.

    Matches the DDL provided by product:
        CREATE TABLE "rad_incubation"."Study_Groundtruth" (...)

    Our additions (for incubation):
        - is_complex             : complex-case tag (drives the future quota rule)
        - main_pathologies       : pre-classified by the LLM at pool ingestion
        - incidental_findings    : pre-classified by the LLM at pool ingestion
        - classified_at          : timestamp of the pre-classification run
    """

    __tablename__ = "Study_Groundtruth"
    __table_args__ = (
        UniqueConstraint("study_iuid", name="study_groundtruth_study_iuid_key"),
        {"schema": SCHEMA},
    )

    study_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    study_iuid: Mapped[str] = mapped_column(String(255), nullable=False)
    modstudy: Mapped[str] = mapped_column(String(225), nullable=False)
    groundtruth_pathology: Mapped[str] = mapped_column(Text, nullable=False)

    modality: Mapped[str | None] = mapped_column(String(225))
    history: Mapped[str | None] = mapped_column(Text)
    dicom_metadata: Mapped[str | None] = mapped_column(Text)
    rules: Mapped[str | None] = mapped_column(Text)

    # Our additions
    is_complex: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    main_pathologies: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    incidental_findings: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    classified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        server_default=func.timezone("Asia/Kolkata", func.now()),
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        server_default=func.timezone("Asia/Kolkata", func.now()),
        onupdate=func.timezone("Asia/Kolkata", func.now()),
    )


class RadState(Base):
    """Per-rad incubation state. One row per rad being incubated."""

    __tablename__ = "rad_state"
    __table_args__ = {"schema": SCHEMA}

    rad_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    incubation_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[RadStatus] = mapped_column(
        Enum(RadStatus, name="rad_status", schema=SCHEMA),
        nullable=False,
        default=RadStatus.in_progress,
    )
    cases_completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    assignments: Mapped[list["CaseAssignment"]] = relationship(back_populates="rad")
    gradings: Mapped[list["GradingJob"]] = relationship(back_populates="rad")


class CaseAssignment(Base):
    """Which study_iuid was assigned to which rad, at what case_number in her sequence."""

    __tablename__ = "case_assignments"
    __table_args__ = (
        UniqueConstraint("rad_id", "study_iuid", name="uq_rad_study_iuid"),
        Index("ix_assignments_rad_case_number", "rad_id", "case_number"),
        ForeignKeyConstraint(
            ["rad_id"], [f"{SCHEMA}.rad_state.rad_id"], ondelete="CASCADE"
        ),
        {"schema": SCHEMA},
    )

    assignment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    rad_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    study_iuid: Mapped[str] = mapped_column(String(255), nullable=False)
    study_id: Mapped[int] = mapped_column(Integer, nullable=False)
    case_number: Mapped[int] = mapped_column(Integer, nullable=False)
    is_complex: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    rad: Mapped[RadState] = relationship(back_populates="assignments")


class GradingJob(Base):
    """One row per graded report. The audit log."""

    __tablename__ = "grading_jobs"
    __table_args__ = (
        UniqueConstraint("rad_id", "study_iuid", name="uq_grading_rad_study_iuid"),
        Index("ix_grading_rad_status", "rad_id", "status"),
        Index("ix_grading_rad_case_number", "rad_id", "case_number"),
        ForeignKeyConstraint(
            ["rad_id"], [f"{SCHEMA}.rad_state.rad_id"], ondelete="CASCADE"
        ),
        {"schema": SCHEMA},
    )

    grading_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    rad_id: Mapped[str] = mapped_column(String(64), nullable=False)
    study_iuid: Mapped[str] = mapped_column(String(255), nullable=False)
    study_id: Mapped[int] = mapped_column(Integer, nullable=False)
    case_number: Mapped[int] = mapped_column(Integer, nullable=False)

    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[GradingStatus] = mapped_column(
        Enum(GradingStatus, name="grading_status", schema=SCHEMA),
        nullable=False,
        default=GradingStatus.queued,
    )

    grade: Mapped[str | None] = mapped_column(String(4))  # "1", "2A", "2B", "3A", "3B"
    score_10pt: Mapped[float | None] = mapped_column(Numeric(3, 1))
    critical_miss: Mapped[bool | None] = mapped_column(Boolean)
    overcall_detected: Mapped[bool | None] = mapped_column(Boolean)
    related_to_primary_indication: Mapped[bool | None] = mapped_column(Boolean)

    llm_raw_json: Mapped[dict | None] = mapped_column(JSONB)
    llm_rationale: Mapped[str | None] = mapped_column(Text)
    llm_model: Mapped[str | None] = mapped_column(String(64))

    ground_truth_snapshot: Mapped[dict | None] = mapped_column(JSONB)
    candidate_snapshot: Mapped[dict | None] = mapped_column(JSONB)

    error_message: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    graded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    rad: Mapped[RadState] = relationship(back_populates="gradings")


class CheckpointEvent(Base):
    """Fired once per rad per threshold (20, 80, 7-day)."""

    __tablename__ = "checkpoint_events"
    __table_args__ = (
        UniqueConstraint("rad_id", "kind", name="uq_checkpoint_rad_kind"),
        {"schema": SCHEMA},
    )

    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    rad_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    kind: Mapped[CheckpointKind] = mapped_column(
        Enum(CheckpointKind, name="checkpoint_kind", schema=SCHEMA), nullable=False
    )
    cases_evaluated: Mapped[int] = mapped_column(Integer, nullable=False)
    avg_score: Mapped[float] = mapped_column(Numeric(4, 2), nullable=False)
    overall_grade: Mapped[str] = mapped_column(String(4), nullable=False)
    quality_met: Mapped[bool] = mapped_column(Boolean, nullable=False)
    grade_counts: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")

    callback_status: Mapped[CallbackStatus] = mapped_column(
        Enum(CallbackStatus, name="callback_status", schema=SCHEMA),
        nullable=False,
        default=CallbackStatus.pending,
    )
    callback_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    callback_last_error: Mapped[str | None] = mapped_column(Text)
    callback_payload: Mapped[dict | None] = mapped_column(JSONB)

    slack_sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    slack_last_error: Mapped[str | None] = mapped_column(Text)

    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
