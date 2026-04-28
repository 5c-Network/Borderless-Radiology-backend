"""LLM pre-classification for Study_Groundtruth rows.

For each row where classified_at IS NULL, asks Gemini to split
groundtruth_pathology into:
  - main_pathologies      : findings the rad MUST detect
  - incidental_findings   : real but secondary findings

Then writes them back along with classified_at.

This script reuses the existing helper:
    app.services.llm.GeminiService.classify_pool_case
which uses the prompts in app/prompts.py
(SYSTEM_PROMPT_POOL_CLASSIFICATION + USER_TEMPLATE_POOL_CLASSIFICATION).

Order of execution on the VM:
    1. alembic upgrade head
    2. python scripts/upload_groundtruth_csv.py <csv>
    3. <run your separate study_iuid extraction>
    4. python scripts/classify_groundtruth_pathologies.py
       python scripts/classify_groundtruth_pathologies.py --limit 5  # smoke test
       python scripts/classify_groundtruth_pathologies.py --dry-run  # call LLM but don't commit

Requires GEMINI_API_KEY in environment / .env.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone

from sqlalchemy import select

from app.db import SessionLocal
from app.models import StudyGroundtruth
from app.services.llm import LLMError, get_llm

logger = logging.getLogger("classify_groundtruth")


async def run(limit: int | None, dry_run: bool) -> int:
    llm = get_llm()  # raises LLMError if GEMINI_API_KEY is missing

    async with SessionLocal() as session:
        stmt = (
            select(StudyGroundtruth)
            .where(StudyGroundtruth.classified_at.is_(None))
            .order_by(StudyGroundtruth.study_id.asc())
        )
        if limit:
            stmt = stmt.limit(limit)

        rows = list((await session.execute(stmt)).scalars().all())
        logger.info("found %d unclassified rows", len(rows))
        if not rows:
            return 0

        ok = 0
        failed = 0

        for row in rows:
            try:
                out = await llm.classify_pool_case(
                    study_iuid=row.study_iuid,
                    modstudy=row.modstudy,
                    modality=row.modality,
                    history=row.history,
                    groundtruth_pathology=row.groundtruth_pathology,
                )
            except LLMError as e:
                logger.error("classify failed for study_id=%s: %s", row.study_id, e)
                failed += 1
                continue

            if dry_run:
                logger.info(
                    "[dry-run] study_id=%s main=%d incidental=%d",
                    row.study_id,
                    len(out.main_pathologies),
                    len(out.incidental_findings),
                )
                continue

            row.main_pathologies = out.main_pathologies
            row.incidental_findings = out.incidental_findings
            row.classified_at = datetime.now(timezone.utc)
            await session.flush()
            ok += 1
            logger.info(
                "classified study_id=%s (%d/%d, failed=%d)",
                row.study_id,
                ok,
                len(rows),
                failed,
            )

        if dry_run:
            logger.info("dry-run: rolling back")
            await session.rollback()
        else:
            await session.commit()

        logger.info("done. classified=%d failed=%d", ok, failed)
        return 0 if failed == 0 else 1


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="classify at most N rows (for smoke testing)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="run the LLM but do not commit changes (rolls back at end)",
    )
    args = p.parse_args()
    rc = asyncio.run(run(args.limit, args.dry_run))
    sys.exit(rc)


if __name__ == "__main__":
    main()
