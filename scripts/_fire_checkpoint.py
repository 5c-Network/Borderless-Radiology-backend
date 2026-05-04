"""Manually fire a checkpoint for testing — bypasses the case-count
auto-trigger so we can validate the Slack + notification PATCH paths
without waiting for the (race-prone) `done_count == N` edge.

Usage:
    python scripts/_fire_checkpoint.py 2105 gate_20
    python scripts/_fire_checkpoint.py 2105 terminal_80
    python scripts/_fire_checkpoint.py 2105 terminal_7_days
"""
from __future__ import annotations

import asyncio
import sys

from dotenv import load_dotenv

load_dotenv()

from app.db import SessionLocal  # noqa: E402
from app.models import CheckpointKind  # noqa: E402
from app.services.checkpoint import fire_checkpoint  # noqa: E402


async def main(rad_id: str, kind_name: str) -> None:
    kind = CheckpointKind(kind_name)
    async with SessionLocal() as session:
        async with session.begin():
            event = await fire_checkpoint(session, rad_id, kind)
    if event is None:
        print(f"[fire] no event created (already exists, or no done grades)")
        return
    print(
        f"[fire] event_id={event.event_id} kind={event.kind.value} "
        f"cases_evaluated={event.cases_evaluated} "
        f"avg_score={float(event.avg_score):.2f} "
        f"overall_grade={event.overall_grade} "
        f"quality_met={event.quality_met} "
        f"callback_status={event.callback_status.value} "
        f"slack_sent={event.slack_sent}"
    )
    if event.callback_last_error:
        print(f"[fire] callback_last_error={event.callback_last_error[:200]}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: _fire_checkpoint.py <rad_id> <gate_20|terminal_80|terminal_7_days>")
        sys.exit(2)
    asyncio.run(main(sys.argv[1], sys.argv[2]))
