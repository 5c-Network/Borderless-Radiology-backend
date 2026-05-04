"""Manually invoke maybe_fire_case_count_checkpoint for a rad — same call
the background grading worker makes after every successful grade. Used to
verify the auto-trigger logic when a real grade_case round can't reliably
push count to the boundary (e.g., flaky LLM)."""
from __future__ import annotations

import asyncio
import sys

from dotenv import load_dotenv

load_dotenv()

from app.db import SessionLocal  # noqa: E402
from app.services.checkpoint import maybe_fire_case_count_checkpoint  # noqa: E402


async def main(rad_id: str) -> None:
    async with SessionLocal() as session:
        async with session.begin():
            event = await maybe_fire_case_count_checkpoint(session, rad_id)
    if event is None:
        print("[trigger] no checkpoint fired (already exists, or count < threshold)")
        return
    print(
        f"[trigger] fired kind={event.kind.value} "
        f"cases_evaluated={event.cases_evaluated} "
        f"avg={float(event.avg_score):.2f} "
        f"grade={event.overall_grade} quality_met={event.quality_met} "
        f"callback_status={event.callback_status.value} "
        f"slack_sent={event.slack_sent}"
    )
    if event.callback_last_error:
        print(f"[trigger] callback_last_error={event.callback_last_error[:200]}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: _invoke_count_trigger.py <rad_id>")
        sys.exit(2)
    asyncio.run(main(sys.argv[1]))
