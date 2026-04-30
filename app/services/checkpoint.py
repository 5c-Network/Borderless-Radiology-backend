"""Computes the 20/80/7-day aggregate for a rad, persists it, fires the
Slack alert and external callback, and updates rad status.

Idempotent: each (rad_id, kind) checkpoint fires exactly once thanks to the
unique constraint on checkpoint_events.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import (
    CallbackStatus,
    CheckpointEvent,
    CheckpointKind,
    GradingJob,
    GradingStatus,
    RadState,
    RadStatus,
)
from app.services.external_callback import (
    send_borderless_qualified,
    send_notice,
)
from app.services.grade_utils import (
    count_grades,
    count_modalities,
    format_modality_breakdown,
    grade_from_avg_score,
    quality_met,
)
from app.services.notification_copy import build_notice
from app.services.slack import build_slack_text, send_slack_alert
from app.services.summary import build_summary

logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")


async def maybe_fire_case_count_checkpoint(
    session: AsyncSession, rad_id: str
) -> CheckpointEvent | None:
    """Called after every successful grade. Fires a checkpoint at case 20 or 80.

    Returns the new CheckpointEvent if one fired, else None.
    """
    settings = get_settings()
    done_count = await _count_done(session, rad_id)

    if done_count == settings.first_checkpoint:
        return await fire_checkpoint(session, rad_id, CheckpointKind.gate_20)
    if done_count == settings.final_checkpoint:
        return await fire_checkpoint(session, rad_id, CheckpointKind.terminal_80)
    return None


async def fire_checkpoint(
    session: AsyncSession,
    rad_id: str,
    kind: CheckpointKind,
) -> CheckpointEvent | None:
    """Idempotently evaluate + fire a checkpoint.

    If one already exists for (rad_id, kind), returns None.
    """
    existing = await session.execute(
        select(CheckpointEvent).where(
            CheckpointEvent.rad_id == rad_id, CheckpointEvent.kind == kind
        )
    )
    if existing.scalar_one_or_none() is not None:
        return None

    grades_rows = await _load_done_grades(session, rad_id)
    if not grades_rows:
        logger.warning("fire_checkpoint called for %s / %s but no graded cases", rad_id, kind)
        return None

    grades = [g.grade for g in grades_rows if g.grade]
    scores = [float(g.score_10pt) for g in grades_rows if g.score_10pt is not None]
    if not scores:
        logger.warning("no scored cases for %s / %s", rad_id, kind)
        return None

    cases_evaluated = len(scores)
    avg = sum(scores) / cases_evaluated
    overall_grade = grade_from_avg_score(avg)
    is_quality_met = quality_met(overall_grade)
    counts = count_grades(grades)
    critical_miss_count = sum(1 for g in grades_rows if g.critical_miss)
    overcall_count = sum(1 for g in grades_rows if g.overcall_detected)
    summary = build_summary(
        cases_evaluated=cases_evaluated,
        avg_score=avg,
        overall_grade=overall_grade,
        grades=grades,
        critical_miss_count=critical_miss_count,
        overcall_count=overcall_count,
    )

    # Modality is snapshotted on every grading_jobs row at queue time
    # (see app/services/grader.py — ground_truth_snapshot["modality"]).
    modalities = [
        (g.ground_truth_snapshot or {}).get("modality") for g in grades_rows
    ]
    modality_counts = count_modalities(modalities)
    modality_str = format_modality_breakdown(modality_counts)

    case_numbers = [g.case_number for g in grades_rows if g.case_number is not None]
    case_min = min(case_numbers) if case_numbers else 1
    case_max = max(case_numbers) if case_numbers else cases_evaluated

    per_case = [
        {
            "case_number": g.case_number,
            "study_iuid": g.study_iuid,
            "study_id": g.study_id,
            "grade": g.grade,
            "score_10pt": float(g.score_10pt) if g.score_10pt is not None else None,
            "critical_miss": bool(g.critical_miss),
        }
        for g in grades_rows
    ]

    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "rad_id": rad_id,
        "kind": kind.value,
        "cases_evaluated": cases_evaluated,
        "case_min": case_min,
        "case_max": case_max,
        "avg_score": round(avg, 2),
        "overall_grade": overall_grade,
        "quality_met": is_quality_met,
        "grade_counts": counts,
        "modality_counts": modality_counts,
        "critical_miss_count": critical_miss_count,
        "overcall_count": overcall_count,
        "summary": summary,
        "per_case": per_case,
        "evaluated_at": now.isoformat(),
    }

    event = CheckpointEvent(
        rad_id=rad_id,
        kind=kind,
        cases_evaluated=cases_evaluated,
        avg_score=round(avg, 2),
        overall_grade=overall_grade,
        quality_met=is_quality_met,
        grade_counts=counts,
        summary=summary,
        callback_payload=payload,
        callback_status=CallbackStatus.pending,
        evaluated_at=now,
    )
    session.add(event)

    # Update rad status per kind.
    rad = await session.get(RadState, rad_id)
    if rad is not None:
        if kind == CheckpointKind.gate_20 and not is_quality_met:
            rad.status = RadStatus.suspended_at_20
        elif kind == CheckpointKind.terminal_80:
            rad.status = RadStatus.completed_80
        elif kind == CheckpointKind.terminal_7_days:
            rad.status = RadStatus.timed_out_7_days

    try:
        await session.flush()
    except IntegrityError:
        # Raced another worker; the unique constraint on (rad_id, kind) wins.
        await session.rollback()
        return None

    # Outbound side effects. These run inside the same transaction but aren't
    # transactional themselves — if they fail we record the error and keep the
    # event row. Retries handled via checkpoint.callback_status = pending.
    slack_text = build_slack_text(
        kind=kind,
        rad_id=rad_id,
        cases_evaluated=cases_evaluated,
        avg_score=avg,
        overall_grade=overall_grade,
        quality_met=is_quality_met,
        summary=summary,
    )
    ok, err = await send_slack_alert(slack_text)
    event.slack_sent = ok
    event.slack_last_error = err

    # Notification + phase-flip routing.
    notice = build_notice(
        kind=kind,
        overall_grade=overall_grade,
        cases_evaluated=cases_evaluated,
        case_min=case_min,
        case_max=case_max,
        avg_score=avg,
        modality_breakdown=modality_str,
    )

    if notice is None:
        # Gate-20 with quality met — silent. No PATCH fires.
        event.callback_status = CallbackStatus.skipped
        event.callback_last_error = None
        event.callback_payload = {**payload, "notice_skipped": True}
    elif (
        kind in (CheckpointKind.terminal_80, CheckpointKind.terminal_7_days)
        and is_quality_met
    ):
        # Endpoint B: phase-flip + INFO eligible in one PATCH.
        event.callback_attempts += 1
        qualified_at = datetime.now(_IST).date()
        ok, err = await send_borderless_qualified(
            rad_id=rad_id, qualified_at=qualified_at, notice=notice
        )
        event.callback_status = (
            CallbackStatus.sent if ok else CallbackStatus.failed
        )
        event.callback_last_error = err
        event.callback_payload = {
            "phase": "BORDERLESS",
            "qualified_at": qualified_at.isoformat(),
            "notice": notice,
        }
    else:
        # Endpoint A: BLOCK notice (gate-20 fail or terminal fail).
        event.callback_attempts += 1
        wire_body = {"notice": notice}
        ok, err = await send_notice(rad_id=rad_id, body=wire_body)
        event.callback_status = (
            CallbackStatus.sent if ok else CallbackStatus.failed
        )
        event.callback_last_error = err
        event.callback_payload = wire_body

    await session.flush()
    return event


async def _count_done(session: AsyncSession, rad_id: str) -> int:
    stmt = select(GradingJob.grading_id).where(
        GradingJob.rad_id == rad_id, GradingJob.status == GradingStatus.done
    )
    result = await session.execute(stmt)
    return len(result.all())


async def _load_done_grades(session: AsyncSession, rad_id: str) -> list[GradingJob]:
    stmt = (
        select(GradingJob)
        .where(GradingJob.rad_id == rad_id, GradingJob.status == GradingStatus.done)
        .order_by(GradingJob.case_number.asc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    return list(rows)
