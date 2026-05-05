"""FastAPI app entry. Mounts routers, auto-applies migrations, schedules
crons, and kicks off a one-shot startup backfill so the deployment is
zero-touch (no manual scripts needed after deploy).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
from sqlalchemy import select

from app.api.activation import router as activation_router
from app.api.grading import router as grading_router
from app.api.health import router as health_router
from app.api.incubation_webhook import router as incubation_webhook_router
from app.api.pool import router as pool_router
from app.config import get_settings
from app.db import SessionLocal
from app.jobs.seven_day_timeout import run_seven_day_sweep
from app.jobs.slot_compliance_job import run_slot_compliance_tick
from app.models import RadState
from app.services.rad_commitment import populate_other_details

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)


# ---------- startup helpers ----------


def _run_alembic_upgrade_sync() -> None:
    """Run `alembic upgrade head` via subprocess. We use a separate process
    rather than `alembic.command.upgrade()` because alembic's env.py calls
    asyncio.run() internally, and that interacts badly with FastAPI's
    already-running event loop even when wrapped in asyncio.to_thread.
    Subprocess gives us a clean Python interpreter with its own loop."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"alembic upgrade failed (rc={result.returncode}): "
            f"stdout={result.stdout[-500:]!r} stderr={result.stderr[-500:]!r}"
        )
    # Surface the alembic output for ops visibility.
    if result.stdout.strip():
        for line in result.stdout.strip().splitlines():
            logger.info("alembic: %s", line)


async def _startup_backfill() -> None:
    """One-shot, idempotent backfill. Runs in the background after the app
    is serving traffic so it never blocks readiness.

      1. Fetches other_details from ClickHouse for any rad in rad_state
         with NULL other_details (skips rads already populated).
      2. Runs one deep slot_compliance tick covering the configured
         look-back (default 30 days) so historical slots get scored.

    Both steps are no-ops if their work has already been done. Safe to run
    on every boot.
    """
    settings = get_settings()
    try:
        async with SessionLocal() as session:
            rads = (
                await session.execute(
                    select(RadState).where(RadState.other_details.is_(None))
                )
            ).scalars().all()
            rad_ids = [r.rad_id for r in rads]
        logger.info(
            "startup backfill: %d rads need other_details", len(rad_ids)
        )
        for rad_id in rad_ids:
            outcome = await populate_other_details(rad_id)
            logger.info(
                "startup backfill: rad=%s outcome=%s", rad_id, outcome
            )
    except Exception:  # noqa: BLE001
        logger.exception("startup backfill: other_details step failed")

    # Deep slot_compliance tick — wide lookback so historical slots get
    # scored. Default 720h = 30 days. Set 0 to skip.
    if settings.startup_backfill_lookback_hours > 0:
        try:
            summary = await run_slot_compliance_tick(
                lookback_hours=settings.startup_backfill_lookback_hours
            )
            logger.info("startup backfill: slot_compliance %s", summary)
        except Exception:  # noqa: BLE001
            logger.exception(
                "startup backfill: slot_compliance tick failed"
            )


# ---------- lifespan ----------


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    # 1) Auto-apply migrations. Idempotent. Failure here fails startup —
    # better than serving with a stale schema.
    if settings.auto_migrate_on_startup:
        try:
            await asyncio.to_thread(_run_alembic_upgrade_sync)
            logger.info("alembic: migrations applied to head")
        except Exception:
            logger.exception("alembic upgrade failed; aborting startup")
            raise

    # 2) Schedule crons.
    scheduler: AsyncIOScheduler | None = None
    if settings.seven_day_job_enabled or settings.slot_compliance_job_enabled:
        scheduler = AsyncIOScheduler(timezone=settings.seven_day_job_timezone)
        if settings.seven_day_job_enabled:
            scheduler.add_job(
                run_seven_day_sweep,
                trigger=CronTrigger(
                    hour=settings.seven_day_job_cron_hour,
                    minute=settings.seven_day_job_cron_minute,
                    timezone=settings.seven_day_job_timezone,
                ),
                id="seven_day_sweep",
                max_instances=1,
                coalesce=True,
                replace_existing=True,
            )
            logger.info(
                "7-day sweep scheduled daily at %02d:%02d %s",
                settings.seven_day_job_cron_hour,
                settings.seven_day_job_cron_minute,
                settings.seven_day_job_timezone,
            )
        if settings.slot_compliance_job_enabled:
            scheduler.add_job(
                run_slot_compliance_tick,
                trigger=CronTrigger(
                    hour=settings.slot_compliance_job_cron_hours,
                    minute=0,
                    timezone=settings.slot_compliance_job_timezone,
                ),
                id="slot_compliance_tick",
                max_instances=1,
                coalesce=True,
                replace_existing=True,
            )
            logger.info(
                "slot_compliance scheduled at hours=%s %s (lookback=%dh)",
                settings.slot_compliance_job_cron_hours,
                settings.slot_compliance_job_timezone,
                settings.slot_compliance_lookback_hours,
            )
        scheduler.start()

    # 3) Kick off the startup backfill in the background. Doesn't block
    # readiness. Idempotent on every boot.
    backfill_task: asyncio.Task | None = None
    if settings.startup_backfill_enabled and settings.slot_compliance_job_enabled:
        backfill_task = asyncio.create_task(
            _startup_backfill(), name="startup_backfill"
        )
        logger.info("startup backfill: scheduled in background")

    try:
        yield
    finally:
        if backfill_task is not None and not backfill_task.done():
            backfill_task.cancel()
        if scheduler is not None:
            scheduler.shutdown(wait=False)


app = FastAPI(
    title="Borderless Radiology Grading Backend",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(health_router)
app.include_router(activation_router)
app.include_router(incubation_webhook_router)
app.include_router(grading_router)
app.include_router(pool_router)
