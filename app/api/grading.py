"""Grading endpoints. n8n calls POST /grade_case per submitted report."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import GradingJob
from app.schemas import GradeCaseRequest, GradeCaseResponse, GradeResult
from app.security import require_api_key
from app.services.grader import enqueue_grading, run_grading_job

router = APIRouter(prefix="/api/v1", tags=["grading"], dependencies=[Depends(require_api_key)])


@router.post(
    "/grade_case",
    response_model=GradeCaseResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def grade_case(
    body: GradeCaseRequest,
    session: AsyncSession = Depends(get_session),
) -> GradeCaseResponse:
    try:
        job = await enqueue_grading(
            session,
            rad_id=body.rad_id,
            study_iuid=body.study_iuid,
            candidate=body.candidate_report,
            submitted_at=body.submitted_at,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    await session.commit()

    # Fire-and-forget background worker.
    asyncio.create_task(run_grading_job(str(job.grading_id)))

    return GradeCaseResponse(grading_id=str(job.grading_id), status=job.status.value)


@router.get("/grade_case/{grading_id}", response_model=GradeResult)
async def get_grade(
    grading_id: str,
    session: AsyncSession = Depends(get_session),
) -> GradeResult:
    job = await session.get(GradingJob, grading_id)
    if job is None:
        raise HTTPException(status_code=404, detail="grading_id not found")
    return GradeResult(
        grading_id=str(job.grading_id),
        rad_id=job.rad_id,
        study_iuid=job.study_iuid,
        case_number=job.case_number,
        status=job.status.value,
        grade=job.grade,
        score_10pt=float(job.score_10pt) if job.score_10pt is not None else None,
        critical_miss=job.critical_miss,
        overcall_detected=job.overcall_detected,
        related_to_primary_indication=job.related_to_primary_indication,
        rationale=job.llm_rationale,
        graded_at=job.graded_at,
    )


@router.get("/rad/{rad_id}/grades")
async def list_rad_grades(
    rad_id: str,
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    rows = (
        await session.execute(
            select(GradingJob)
            .where(GradingJob.rad_id == rad_id)
            .order_by(GradingJob.case_number.asc())
        )
    ).scalars().all()
    return [
        {
            "case_number": r.case_number,
            "study_iuid": r.study_iuid,
            "study_id": r.study_id,
            "status": r.status.value,
            "grade": r.grade,
            "score_10pt": float(r.score_10pt) if r.score_10pt is not None else None,
            "critical_miss": r.critical_miss,
            "graded_at": r.graded_at.isoformat() if r.graded_at else None,
        }
        for r in rows
    ]
