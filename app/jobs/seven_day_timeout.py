"""Background job: fire the 7-day-timeout terminal for rads whose incubation
window has elapsed without completing 80 cases.

Day 1 = the IST calendar day after the rad's first start-reporting webhook.
Day 7 = last day of incubation. The cron runs daily at 00:00 IST and fires
terminals for any in_progress rad whose Day 7 has ended (i.e. today_IST is
on or after start_date_IST + 8 days).

Suspended rads (status = suspended_at_20) are excluded — once blocked, the
cron leaves them alone. Re-runs are safe; the checkpoint service is
idempotent on (rad_id, kind).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db import SessionLocal
from app.models import CheckpointKind, RadState, RadStatus
from app.services.checkpoint import fire_checkpoint

logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")


async def run_seven_day_sweep() -> int:
    """One session for the lookup, then a fresh session per rad — same
    pattern as run_grading_job. This isolates per-rad failures and avoids
    transaction nesting on the shared session.
    """
    today_ist = datetime.now(_IST).date()

    async with SessionLocal() as session:
        candidates = (
            await session.execute(
                select(RadState).where(
                    RadState.status == RadStatus.in_progress,
                    RadState.incubation_started_at.is_not(None),
                )
            )
        ).scalars().all()

        # Day 1 is the day after the first webhook, so Day 8 = start_date + 8.
        # We fire when today_IST is on or after Day 8.
        eligible_ids = [
            rad.rad_id
            for rad in candidates
            if today_ist
            >= rad.incubation_started_at.astimezone(_IST).date()
            + timedelta(days=8)
        ]

    fired = 0
    for rad_id in eligible_ids:
        async with SessionLocal() as session:
            async with session.begin():
                event = await fire_checkpoint(
                    session, rad_id, CheckpointKind.terminal_7_days
                )
        if event is not None:
            fired += 1
            logger.info(
                "fired 7-day terminal for rad=%s cases=%s grade=%s",
                rad_id, event.cases_evaluated, event.overall_grade,
            )
    return fired
