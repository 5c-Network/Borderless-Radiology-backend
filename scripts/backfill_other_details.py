"""One-time backfill: populate rad_state.other_details for every existing rad.

For each rad in rad_state with NULL other_details, fetches the
transform.RadDetails.other_details JSON from ClickHouse and stores it.
Uses the production T1 hook function so behavior matches the live path.

Idempotent: rads already populated are skipped (the hook detects this).

Usage:
    python scripts/backfill_other_details.py             # all rads
    python scripts/backfill_other_details.py --rad-id 2105   # one rad
    python scripts/backfill_other_details.py --dry-run

Read-only against ClickHouse, single-row UPDATE per rad on Postgres.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from sqlalchemy import select

from app.db import SessionLocal
from app.models import RadState
from app.services.rad_commitment import populate_other_details

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_other_details")


async def amain(rad_id: str | None, dry_run: bool) -> None:
    async with SessionLocal() as session:
        if rad_id:
            rads = (
                await session.execute(
                    select(RadState).where(RadState.rad_id == rad_id)
                )
            ).scalars().all()
        else:
            rads = (
                await session.execute(
                    select(RadState).where(RadState.other_details.is_(None))
                )
            ).scalars().all()

    print(f"target rads: {len(rads)}")
    counts = {"ok": 0, "skip": 0, "err": 0, "dryrun": 0}
    for rad in rads:
        if dry_run:
            print(f"  [dry-run] would populate rad_id={rad.rad_id}")
            counts["dryrun"] += 1
            continue
        outcome = await populate_other_details(rad.rad_id)
        if outcome == "ok":
            counts["ok"] += 1
        elif outcome.startswith("skip"):
            counts["skip"] += 1
        else:
            counts["err"] += 1
        print(f"  rad_id={rad.rad_id} -> {outcome}")
    print(f"summary: {counts}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--rad-id", default=None,
                   help="Backfill a single rad. Default: all rads with NULL other_details.")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    try:
        asyncio.run(amain(args.rad_id, args.dry_run))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
