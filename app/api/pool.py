"""Study_Groundtruth management endpoints.

Use these during the one-time pool ingestion:
  1) POST /study-groundtruth        : upsert raw rows from your source data.
  2) POST /study-groundtruth/classify : runs the LLM pool-classifier on any
     row that doesn't yet have main_pathologies / incidental_findings.
  3) GET  /study-groundtruth        : inspect the current pool.

All endpoints are protected by the same Authorization header as the rest of
the API.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import StudyGroundtruth
from app.schemas import StudyGroundtruthIngest, StudyGroundtruthOut
from app.security import require_api_key
from app.services.llm import LLMError, get_llm

router = APIRouter(
    prefix="/api/v1/study-groundtruth",
    tags=["study-groundtruth"],
    dependencies=[Depends(require_api_key)],
)


@router.post("", response_model=list[StudyGroundtruthOut])
async def ingest_rows(
    rows: list[StudyGroundtruthIngest],
    session: AsyncSession = Depends(get_session),
) -> list[StudyGroundtruthOut]:
    for r in rows:
        stmt = (
            pg_insert(StudyGroundtruth)
            .values(
                study_id=r.study_id,
                study_iuid=r.study_iuid,
                modstudy=r.modstudy,
                groundtruth_pathology=r.groundtruth_pathology,
                modality=r.modality,
                history=r.history,
                dicom_metadata=r.dicom_metadata,
                rules=r.rules,
                is_complex=r.is_complex,
            )
            .on_conflict_do_update(
                index_elements=[StudyGroundtruth.study_id],
                set_={
                    "study_iuid": r.study_iuid,
                    "modstudy": r.modstudy,
                    "groundtruth_pathology": r.groundtruth_pathology,
                    "modality": r.modality,
                    "history": r.history,
                    "dicom_metadata": r.dicom_metadata,
                    "rules": r.rules,
                    "is_complex": r.is_complex,
                },
            )
        )
        await session.execute(stmt)
    await session.commit()
    return await _list_rows(session)


@router.post("/classify", response_model=list[StudyGroundtruthOut])
async def classify_rows(
    only_unclassified: bool = True,
    session: AsyncSession = Depends(get_session),
) -> list[StudyGroundtruthOut]:
    stmt = select(StudyGroundtruth)
    if only_unclassified:
        stmt = stmt.where(StudyGroundtruth.classified_at.is_(None))
    rows = list((await session.execute(stmt)).scalars().all())
    if not rows:
        return await _list_rows(session)

    llm = get_llm()
    for row in rows:
        try:
            out = await llm.classify_pool_case(
                study_iuid=row.study_iuid,
                modstudy=row.modstudy,
                modality=row.modality,
                history=row.history,
                groundtruth_pathology=row.groundtruth_pathology,
            )
        except LLMError as e:
            raise HTTPException(
                status_code=502,
                detail=f"LLM classification failed for {row.study_iuid}: {e}",
            ) from e
        row.main_pathologies = out.main_pathologies
        row.incidental_findings = out.incidental_findings
        row.classified_at = datetime.now(timezone.utc)
        await session.flush()

    await session.commit()
    return await _list_rows(session)


@router.get("", response_model=list[StudyGroundtruthOut])
async def list_rows(
    session: AsyncSession = Depends(get_session),
) -> list[StudyGroundtruthOut]:
    return await _list_rows(session)


async def _list_rows(session: AsyncSession) -> list[StudyGroundtruthOut]:
    rows = (
        await session.execute(
            select(StudyGroundtruth).order_by(StudyGroundtruth.study_id.asc())
        )
    ).scalars().all()
    return [
        StudyGroundtruthOut(
            study_id=r.study_id,
            study_iuid=r.study_iuid,
            modstudy=r.modstudy,
            modality=r.modality,
            is_complex=r.is_complex,
            main_pathologies=r.main_pathologies or [],
            incidental_findings=r.incidental_findings or [],
            classified=r.classified_at is not None,
        )
        for r in rows
    ]
