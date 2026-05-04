"""Manually invoke the 7-day sweep cron job (the one APScheduler runs at
midnight IST). Tests the actual `run_seven_day_sweep` logic — picks up any
in_progress rad whose incubation_started_at is >= 8 days old, fires
terminal_7_days for them.
"""
from __future__ import annotations

import asyncio

from dotenv import load_dotenv

load_dotenv()

from app.jobs.seven_day_timeout import run_seven_day_sweep  # noqa: E402


async def main() -> None:
    fired = await run_seven_day_sweep()
    print(f"[sweep] fired {fired} terminal_7_days checkpoint(s)")


if __name__ == "__main__":
    asyncio.run(main())
