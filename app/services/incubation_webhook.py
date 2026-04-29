"""Event-driven activation webhook service.

Handles two events on POST /api/v1/incubation/webhook:

  start-reporting   First login. Creates rad_state, stores modality_preferred,
                    sets incubation_started_at, picks 2 unused cases filtered
                    by the rad's modality set.

  case-submitted    After every "Submit & Get Next Case" click. Picks 1
                    unused case for the rad, filtered by stored modality set.
                    Never re-assigns a study already in case_assignments
                    for this rad.

Both handlers are idempotent on the obvious failure modes:
  - Re-fired start-reporting on an existing rad: no writes, returns []
    with message="rad already started".
  - Concurrent case-submitted: the (rad_id, study_iuid) unique constraint
    catches double-pick races; we retry the pick once.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import CaseAssignment, RadState, RadStatus, StudyGroundtruth
from app.schemas import (
    ActivationDataItem,
    IncubationWebhookRequest,
    IncubationWebhookResponse,
    WebhookEvent,
)
from app.services.activation_service import _to_activation_item

logger = logging.getLogger(__name__)


_TERMINAL_STATES = {
    RadStatus.completed_80,
    RadStatus.timed_out_7_days,
    RadStatus.suspended_at_20,
}


@dataclass
class _PickContext:
    rad: RadState
    seen: set[str]
    tokens: list[str]


async def handle_webhook(
    session: AsyncSession, payload: IncubationWebhookRequest
) -> IncubationWebhookResponse:
    rad_id = str(payload.rad_id)
    if payload.event == WebhookEvent.start_reporting:
        return await _handle_start_reporting(session, rad_id, payload.modalities)
    if payload.event == WebhookEvent.case_submitted:
        return await _handle_case_submitted(session, rad_id, payload.modalities)
    # Pydantic enum validation already prevents reaching here.
    raise HTTPException(status_code=400, detail=f"unknown event {payload.event!r}")


# ----- start-reporting -------------------------------------------------------


async def _handle_start_reporting(
    session: AsyncSession, rad_id: str, modalities: list[str]
) -> IncubationWebhookResponse:
    canonical = ",".join(modalities)

    rad = await session.get(RadState, rad_id)
    if rad is not None:
        # Idempotent re-fire. Don't overwrite preference, don't reset times,
        # don't assign new cases. Surface a message so n8n can branch.
        logger.warning(
            "start-reporting received for rad %s which already exists (status=%s); no-op",
            rad_id,
            rad.status.value,
        )
        return IncubationWebhookResponse(
            rad_id=rad_id,
            event=WebhookEvent.start_reporting,
            rad_status=rad.status.value,
            cases_completed=rad.cases_completed,
            cases_assigned_now=0,
            items=[],
            message="rad already started",
        )

    now = datetime.now(timezone.utc)
    rad = RadState(
        rad_id=rad_id,
        status=RadStatus.in_progress,
        cases_completed=0,
        modality_preferred=canonical,
        incubation_started_at=now,
    )
    session.add(rad)
    await session.flush()

    picked = await _pick_unused(session, rad_id=rad_id, tokens=modalities, k=2, seen=set())
    if not picked:
        return IncubationWebhookResponse(
            rad_id=rad_id,
            event=WebhookEvent.start_reporting,
            rad_status=rad.status.value,
            cases_completed=rad.cases_completed,
            cases_assigned_now=0,
            items=[],
            message="no unused pool cases available",
        )

    for i, row in enumerate(picked):
        session.add(
            CaseAssignment(
                rad_id=rad_id,
                study_iuid=row.study_iuid,
                study_id=row.study_id,
                case_number=i + 1,
                is_complex=row.is_complex,
                assigned_at=now,
            )
        )
    await session.flush()

    return IncubationWebhookResponse(
        rad_id=rad_id,
        event=WebhookEvent.start_reporting,
        rad_status=rad.status.value,
        cases_completed=rad.cases_completed,
        cases_assigned_now=len(picked),
        items=[_to_activation_item(r) for r in picked],
        message=None,
    )


# ----- case-submitted --------------------------------------------------------


async def _handle_case_submitted(
    session: AsyncSession, rad_id: str, modalities_from_body: list[str]
) -> IncubationWebhookResponse:
    rad = await session.get(RadState, rad_id)
    if rad is None:
        raise HTTPException(
            status_code=404,
            detail="rad not found; start-reporting must be sent before case-submitted",
        )

    if rad.modality_preferred is None:
        # Defensive: rad row predates this feature.
        raise HTTPException(
            status_code=409,
            detail="rad has no modality_preferred set; cannot pick filtered case",
        )

    body_canonical = ",".join(modalities_from_body)
    if body_canonical != rad.modality_preferred:
        logger.warning(
            "case-submitted modalities %r differ from stored %r for rad %s; using stored",
            body_canonical,
            rad.modality_preferred,
            rad_id,
        )

    if rad.status in _TERMINAL_STATES:
        return IncubationWebhookResponse(
            rad_id=rad_id,
            event=WebhookEvent.case_submitted,
            rad_status=rad.status.value,
            cases_completed=rad.cases_completed,
            cases_assigned_now=0,
            items=[],
            message=f"rad is {rad.status.value}",
        )

    settings = get_settings()
    seen = await _load_seen(session, rad_id)

    if len(seen) >= settings.total_pool_cases:
        return IncubationWebhookResponse(
            rad_id=rad_id,
            event=WebhookEvent.case_submitted,
            rad_status=rad.status.value,
            cases_completed=rad.cases_completed,
            cases_assigned_now=0,
            items=[],
            message="pool exhausted for this rad",
        )

    tokens = rad.modality_preferred.split(",")
    next_number = len(seen) + 1
    now = datetime.now(timezone.utc)

    # Try once; on IntegrityError (concurrent submitter raced us to the same
    # study_iuid), reload seen and retry once.
    for attempt in (1, 2):
        picked = await _pick_unused(session, rad_id=rad_id, tokens=tokens, k=1, seen=seen)
        if not picked:
            return IncubationWebhookResponse(
                rad_id=rad_id,
                event=WebhookEvent.case_submitted,
                rad_status=rad.status.value,
                cases_completed=rad.cases_completed,
                cases_assigned_now=0,
                items=[],
                message="no unused pool cases available",
            )
        row = picked[0]
        session.add(
            CaseAssignment(
                rad_id=rad_id,
                study_iuid=row.study_iuid,
                study_id=row.study_id,
                case_number=next_number,
                is_complex=row.is_complex,
                assigned_at=now,
            )
        )
        try:
            await session.flush()
            break
        except IntegrityError:
            await session.rollback()
            if attempt == 2:
                logger.exception(
                    "case-submitted: lost two races on (rad_id, study_iuid) for %s", rad_id
                )
                raise HTTPException(
                    status_code=500, detail="could not assign case after retry"
                )
            seen = await _load_seen(session, rad_id)
            next_number = len(seen) + 1

    return IncubationWebhookResponse(
        rad_id=rad_id,
        event=WebhookEvent.case_submitted,
        rad_status=rad.status.value,
        cases_completed=rad.cases_completed,
        cases_assigned_now=1,
        items=[_to_activation_item(row)],
        message=None,
    )


# ----- helpers ---------------------------------------------------------------


async def _load_seen(session: AsyncSession, rad_id: str) -> set[str]:
    rows = (
        await session.execute(
            select(CaseAssignment.study_iuid).where(CaseAssignment.rad_id == rad_id)
        )
    ).all()
    return {row[0] for row in rows}


async def _pick_unused(
    session: AsyncSession,
    *,
    rad_id: str,
    tokens: list[str],
    k: int,
    seen: set[str],
) -> list[StudyGroundtruth]:
    stmt = select(StudyGroundtruth).where(StudyGroundtruth.modality.in_(tokens))
    if seen:
        stmt = stmt.where(StudyGroundtruth.study_iuid.notin_(seen))
    # Webhook is the prod path. Test-tagged rows are reachable only via
    # GET /api/v1/activation-data/test (DICOMs pre-loaded on the server).
    stmt = stmt.where(
        (StudyGroundtruth.case_type.is_(None))
        | (StudyGroundtruth.case_type != "test")
    )
    pool = list((await session.execute(stmt)).scalars().all())
    if not pool:
        return []
    k = min(k, len(pool))
    return random.sample(pool, k=k)
