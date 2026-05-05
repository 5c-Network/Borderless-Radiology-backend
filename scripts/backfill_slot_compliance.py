"""One-time backfill: re-run the slot_compliance scoring for a wide window.

Calls the same tick logic as the cron, but with a custom lookback so it
re-scores any historical slots that haven't been written yet. Idempotent
because of the ON CONFLICT clause + the existing-keys filter.

Usage:
    python scripts/backfill_slot_compliance.py --lookback-hours 720   # 30 days
    python scripts/backfill_slot_compliance.py --lookback-hours 168   # 1 week

Reads ClickHouse, writes rad_slot_status. Already-scored slots are skipped
(no CH re-query).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_slot_compliance")


async def amain(lookback_hours: int) -> None:
    # Override the cron's default look-back via env so the tick runs over a
    # wider historical window. get_settings() is lru_cached so we set the
    # env BEFORE the first import that triggers settings load.
    os.environ["SLOT_COMPLIANCE_LOOKBACK_HOURS"] = str(lookback_hours)
    from app.jobs.slot_compliance_job import run_slot_compliance_tick

    summary = await run_slot_compliance_tick()
    print(f"backfill summary: {summary}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--lookback-hours", type=int, default=168,
                   help="How far back (in hours, IST) to re-score. Default 168 (1 week).")
    args = p.parse_args()
    try:
        asyncio.run(amain(args.lookback_hours))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
