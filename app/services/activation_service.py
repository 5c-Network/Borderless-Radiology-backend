"""Activation-data service. Two modes:

  A) UID lookup — n8n passes study_iuids=...  We return exact matches.
     No assignment tracking. No randomness.

  B) Random pick — no study_iuids.  We pick unused cases for this rad
     (2 on first call, 1 thereafter), record the assignment, and return
     them in activation-data format.

Response format matches the QA spec:
    [ { history, rules, dicomData, for_candidate: true }, ... ]

Complex-case-per-hour quota is deferred; we still persist is_complex on the
assignment so the future logic can read it.
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import CaseAssignment, RadState, RadStatus, StudyGroundtruth
from app.schemas import ActivationDataItem, DicomData

logger = logging.getLogger(__name__)


@dataclass
class ActivationResult:
    items: list[ActivationDataItem]
    cases_completed: int
    rad_status: str | None = None
    message: str | None = None


async def get_activation_data(
    session: AsyncSession,
    *,
    rad_id: str,
    study_iuids: list[str] | None,
) -> ActivationResult:
    if study_iuids:
        return await _mode_uid_lookup(session, rad_id, study_iuids)
    return await _mode_random_pick(session, rad_id)


# ----- Mode A: direct lookup -------------------------------------------------


async def _mode_uid_lookup(
    session: AsyncSession, rad_id: str, study_iuids: list[str]
) -> ActivationResult:
    rows = (
        await session.execute(
            select(StudyGroundtruth).where(StudyGroundtruth.study_iuid.in_(study_iuids))
        )
    ).scalars().all()

    items = [_to_activation_item(r) for r in rows]
    rad = await session.get(RadState, rad_id)
    return ActivationResult(
        items=items,
        cases_completed=(rad.cases_completed if rad else 0),
        rad_status=(rad.status.value if rad else None),
    )


# ----- Mode B: random pick with assignment tracking --------------------------


async def _mode_random_pick(session: AsyncSession, rad_id: str) -> ActivationResult:
    settings = get_settings()

    rad = await _get_or_create_rad(session, rad_id)

    if rad.status in (
        RadStatus.completed_80,
        RadStatus.timed_out_7_days,
        RadStatus.suspended_at_20,
    ):
        return ActivationResult(
            items=[],
            cases_completed=rad.cases_completed,
            rad_status=rad.status.value,
            message=f"rad is {rad.status.value}",
        )

    # All study_iuids this rad has already been given.
    seen = {
        row[0]
        for row in (
            await session.execute(
                select(CaseAssignment.study_iuid).where(CaseAssignment.rad_id == rad_id)
            )
        ).all()
    }

    if rad.cases_completed >= settings.total_pool_cases:
        return ActivationResult(
            items=[],
            cases_completed=rad.cases_completed,
            rad_status=rad.status.value,
            message="pool exhausted for this rad",
        )

    stmt = select(StudyGroundtruth)
    if seen:
        stmt = stmt.where(StudyGroundtruth.study_iuid.notin_(seen))
    unused = list((await session.execute(stmt)).scalars().all())
    if not unused:
        return ActivationResult(
            items=[],
            cases_completed=rad.cases_completed,
            rad_status=rad.status.value,
            message="no unused pool cases available",
        )

    want = 2 if len(seen) == 0 else 1
    want = min(want, len(unused))
    picked = random.sample(unused, k=want)

    next_number = len(seen) + 1
    now = datetime.now(timezone.utc)
    for i, row in enumerate(picked):
        session.add(
            CaseAssignment(
                rad_id=rad_id,
                study_iuid=row.study_iuid,
                study_id=row.study_id,
                case_number=next_number + i,
                is_complex=row.is_complex,
                assigned_at=now,
            )
        )

    if rad.incubation_started_at is None:
        rad.incubation_started_at = now

    await session.flush()

    return ActivationResult(
        items=[_to_activation_item(r) for r in picked],
        cases_completed=rad.cases_completed,
        rad_status=rad.status.value,
    )


# ----- Helpers ---------------------------------------------------------------


async def _get_or_create_rad(session: AsyncSession, rad_id: str) -> RadState:
    rad = await session.get(RadState, rad_id)
    if rad is None:
        rad = RadState(rad_id=rad_id, status=RadStatus.in_progress, cases_completed=0)
        session.add(rad)
        await session.flush()
    return rad


def _to_activation_item(row: StudyGroundtruth) -> ActivationDataItem:
    rules = _safe_json_list(row.rules)
    dicom = _safe_json_dict(row.dicom_metadata)
    # Ensure study_iuid is set on the dicomData block, even if the stored
    # metadata forgot to include it.
    dicom.setdefault("study_iuid", row.study_iuid)

    return ActivationDataItem(
        history=row.history or "",
        rules=rules,
        dicomData=DicomData.model_validate(dicom),
        for_candidate=True,
    )


def _safe_json_list(raw: str | None) -> list[dict]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("could not parse rules JSON; returning []")
        return []
    if isinstance(parsed, list):
        return parsed
    return []


def _safe_json_dict(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("could not parse dicom_metadata JSON; returning {}")
        return {}
    if isinstance(parsed, dict):
        return parsed
    return {}
