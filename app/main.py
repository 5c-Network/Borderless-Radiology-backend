"""FastAPI app entry. Mounts routers and schedules the 7-day sweep."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI

from app.api.activation import router as activation_router
from app.api.grading import router as grading_router
from app.api.health import router as health_router
from app.api.incubation_webhook import router as incubation_webhook_router
from app.api.pool import router as pool_router
from app.config import get_settings
from app.jobs.seven_day_timeout import run_seven_day_sweep

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    scheduler: AsyncIOScheduler | None = None
    if settings.seven_day_job_enabled:
        scheduler = AsyncIOScheduler(timezone="UTC")
        scheduler.add_job(
            run_seven_day_sweep,
            trigger=IntervalTrigger(minutes=settings.seven_day_job_interval_minutes),
            id="seven_day_sweep",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        scheduler.start()
        logger.info(
            "7-day sweep scheduled every %s min", settings.seven_day_job_interval_minutes
        )
    try:
        yield
    finally:
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
