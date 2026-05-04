"""Throwaway: inspect rad_id=3638 state before the test run.
Reports rad_state row, assignment count, grading_jobs status counts, and
checkpoint_events. Read-only.
"""
from __future__ import annotations

import asyncio
import os
import sys

import asyncpg
from dotenv import load_dotenv

load_dotenv()

RAD_ID = "3638"


def _to_asyncpg_dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def main() -> None:
    dsn = _to_asyncpg_dsn(os.environ["DATABASE_URL"])
    conn = await asyncpg.connect(dsn)
    try:
        rad = await conn.fetchrow(
            'SELECT rad_id, status, cases_completed, modality_preferred, '
            'incubation_started_at FROM rad_incubation.rad_state WHERE rad_id = $1',
            RAD_ID,
        )
        print("rad_state:", dict(rad) if rad else None)

        a_count = await conn.fetchval(
            'SELECT COUNT(*) FROM rad_incubation.case_assignments WHERE rad_id = $1',
            RAD_ID,
        )
        print(f"case_assignments count: {a_count}")

        g_rows = await conn.fetch(
            'SELECT status, COUNT(*) FROM rad_incubation.grading_jobs '
            'WHERE rad_id = $1 GROUP BY status',
            RAD_ID,
        )
        print("grading_jobs by status:", {r["status"]: r["count"] for r in g_rows})

        cp = await conn.fetch(
            'SELECT kind, callback_status, slack_sent, overall_grade, '
            'quality_met, evaluated_at FROM rad_incubation.checkpoint_events '
            'WHERE rad_id = $1',
            RAD_ID,
        )
        print("checkpoint_events:")
        for r in cp:
            print(" ", dict(r))

        # Pool size for CT/MRI (what's available to assign)
        pool = await conn.fetchval(
            "SELECT COUNT(*) FROM rad_incubation.\"Study_Groundtruth\" "
            "WHERE modality IN ('CT','MRI') "
            "AND (case_type IS NULL OR case_type <> 'test')",
        )
        print(f"available CT/MRI pool size: {pool}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
