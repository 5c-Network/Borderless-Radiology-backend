"""Grading orchestration.

- enqueue_grading: called from the /grade_case endpoint. Inserts a queued row
  and returns immediately. Idempotent on (rad_id, study_iuid).
- run_grading_job: the background worker. Fetches GT + candidate, calls the
  LLM, persists the result, and invokes the checkpoint trigger.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import SessionLocal
from app.models import (
    CaseAssignment,
    GradingJob,
    GradingStatus,
    RadState,
    StudyGroundtruth,
)
from app.schemas import CandidateReport
from app.services.checkpoint import maybe_fire_case_count_checkpoint
from app.services.grade_utils import score_from_grade
from app.services.llm import LLMError, get_llm

logger = logging.getLogger(__name__)


async def enqueue_grading(
    session: AsyncSession,
    *,
    rad_id: str,
    study_iuid: str,
    candidate: CandidateReport,
    submitted_at: datetime | None,
) -> GradingJob:
    """Insert a queued row. Idempotent on (rad_id, study_iuid)."""

    existing = await session.execute(
        select(GradingJob).where(
            GradingJob.rad_id == rad_id, GradingJob.study_iuid == study_iuid
        )
    )
    job = existing.scalar_one_or_none()
    if job is not None:
        return job

    # Verify the rad was actually assigned this study.
    assignment = (
        await session.execute(
            select(CaseAssignment).where(
                CaseAssignment.rad_id == rad_id,
                CaseAssignment.study_iuid == study_iuid,
            )
        )
    ).scalar_one_or_none()
    if assignment is None:
        raise ValueError(f"rad {rad_id} was not assigned study {study_iuid}")

    gt = (
        await session.execute(
            select(StudyGroundtruth).where(StudyGroundtruth.study_iuid == study_iuid)
        )
    ).scalar_one_or_none()
    if gt is None:
        raise ValueError(f"study_iuid {study_iuid} not in Study_Groundtruth")

    job = GradingJob(
        rad_id=rad_id,
        study_iuid=study_iuid,
        study_id=gt.study_id,
        case_number=assignment.case_number,
        submitted_at=submitted_at or datetime.now(timezone.utc),
        status=GradingStatus.queued,
        candidate_snapshot={
            "observation": candidate.observation,
            "impression": candidate.impression,
        },
        ground_truth_snapshot={
            "main_pathologies": gt.main_pathologies,
            "incidental_findings": gt.incidental_findings,
            "history": gt.history,
            "groundtruth_pathology": gt.groundtruth_pathology,
            "modality": gt.modality,
            "modstudy": gt.modstudy,
        },
    )
    session.add(job)
    await session.flush()
    return job


async def run_grading_job(grading_id: str) -> None:
    """Background worker. Opens its own DB session."""
    async with SessionLocal() as session:
        async with session.begin():
            job = await session.get(GradingJob, grading_id)
            if job is None:
                logger.error("grading job %s not found", grading_id)
                return
            job.status = GradingStatus.running

        # Call the LLM outside a transaction — no DB locks during HTTP.
        try:
            llm = get_llm()
            gt = job.ground_truth_snapshot or {}
            cand = job.candidate_snapshot or {}
            result = await llm.grade_case(
                study_iuid=job.study_iuid,
                main_pathologies=gt.get("main_pathologies", []),
                incidental_findings=gt.get("incidental_findings", []),
                history=gt.get("history"),
                groundtruth_pathology=gt.get("groundtruth_pathology", ""),
                candidate_observation=cand.get("observation", ""),
                candidate_impression=cand.get("impression", ""),
            )
        except LLMError as e:
            async with session.begin():
                job = await session.get(GradingJob, grading_id)
                if job is not None:
                    job.status = GradingStatus.error
                    job.error_message = str(e)[:2000]
            logger.exception("LLM failure on grading job %s", grading_id)
            return
        except Exception as e:  # noqa: BLE001
            async with session.begin():
                job = await session.get(GradingJob, grading_id)
                if job is not None:
                    job.status = GradingStatus.error
                    job.error_message = f"unexpected: {e}"[:2000]
            logger.exception("unexpected failure on grading job %s", grading_id)
            return

        settings = get_settings()
        async with session.begin():
            job = await session.get(GradingJob, grading_id)
            if job is None:
                return
            job.grade = result.grade
            job.score_10pt = score_from_grade(result.grade)
            job.critical_miss = result.critical_miss
            job.overcall_detected = result.overcall_detected
            job.related_to_primary_indication = result.related_to_primary_indication
            job.llm_raw_json = result.model_dump()
            job.llm_rationale = result.rationale
            job.llm_model = settings.gemini_model
            job.status = GradingStatus.done
            job.graded_at = datetime.now(timezone.utc)

            rad = await session.get(RadState, job.rad_id)
            if rad is not None:
                rad.cases_completed = (rad.cases_completed or 0) + 1

        async with session.begin():
            await maybe_fire_case_count_checkpoint(session, job.rad_id)
