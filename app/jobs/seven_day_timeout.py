"""Background job: fire the 7-day-timeout terminal for rads whose incubation
window has elapsed without completing 80 cases.

Scheduled from main.py via APScheduler. Safe to re-run; the checkpoint service
is idempotent on (rad_id, kind).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import CheckpointKind, RadState, RadStatus
from app.services.checkpoint import fire_checkpoint

logger = logging.getLogger(__name__)


async def run_seven_day_sweep() -> int:
    settings = get_settings()
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.incubation_days)

    async with SessionLocal() as session:
        rads = (
            await session.execute(
                select(RadState).where(
                    RadState.status == RadStatus.in_progress,
                    RadState.incubation_started_at.is_not(None),
                    RadState.incubation_started_at <= cutoff,
                )
            )
        ).scalars().all()

        fired = 0
        for rad in rads:
            async with session.begin():
                event = await fire_checkpoint(
                    session, rad.rad_id, CheckpointKind.terminal_7_days
                )
            if event is not None:
                fired += 1
                logger.info(
                    "fired 7-day terminal for rad=%s cases=%s grade=%s",
                    rad.rad_id, event.cases_evaluated, event.overall_grade,
                )
        return fired
