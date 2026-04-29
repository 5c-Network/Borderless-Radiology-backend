"""Upload test-only cases into rad_incubation."Study_Groundtruth".

INSERT-only. Existing rows are never touched.

Why a separate script:
  - The regular upload_groundtruth_csv.py upserts on study_id, which means
    a CSV containing existing study_ids would mutate production rows.
  - This script uses INSERT ... ON CONFLICT (study_id) DO NOTHING, so a
    test CSV can never reclassify a row already in the DB.
  - case_type is FORCED to 'test' in the SQL itself — the CSV's case_type
    cell is ignored. There is no code path here that writes any other
    value.

Skipped rows (logged, not written):
  - study_id already exists in DB
  - study_iuid already exists in DB on a different study_id (UNIQUE
    constraint on study_iuid would otherwise raise)
  - missing required field: study_id, modstudy, groundtruth_pathology,
    or study_iuid

Idempotent: re-running on the same CSV inserts nothing the second time.

Usage:
    python scripts/upload_test_cases_csv.py path/to/test-cases.csv
    python scripts/upload_test_cases_csv.py path/to/test-cases.csv --dry-run
    python scripts/upload_test_cases_csv.py path/to/test-cases.csv --batch-size 100
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import sys
from pathlib import Path

from sqlalchemy import text

from app.db import engine

logger = logging.getLogger("upload_test_cases_csv")


# 'test' is hardcoded in the SQL — the CSV's case_type cell is intentionally
# not bound. This script is incapable of writing any other value into
# case_type for either inserted or skipped rows.
INSERT_SQL = text(
    """
    INSERT INTO rad_incubation."Study_Groundtruth" (
        study_id,
        study_iuid,
        old_study_iuid,
        modstudy,
        groundtruth_pathology,
        modality,
        history,
        dicom_metadata,
        rules,
        category,
        observation,
        impression,
        age,
        is_complex,
        case_type
    ) VALUES (
        :study_id,
        :study_iuid,
        :old_study_iuid,
        :modstudy,
        :groundtruth_pathology,
        :modality,
        :history,
        :dicom_metadata,
        :rules,
        :category,
        :observation,
        :impression,
        :age,
        :is_complex,
        'test'
    )
    ON CONFLICT (study_id) DO NOTHING
    """
)


# CSV header → DB column name. Required columns must be present in the CSV
# header; values are validated per-row.
CSV_HEADER_MAP = {
    "study_id": "study_id",
    "study_iuid": "study_iuid",
    "old_study_iuid": "old_study_iuid",
    "modstudy": "modstudy",
    "groundtruth_pathology": "groundtruth_pathology",
    "modality": "modality",
    "history": "history",
    "dicom_metadata": "dicom_metadata",
    "rules": "rules",
    "category": "category",
    "observation": "observation",
    "impression": "impression",
    "age": "age",
    "is_complex": "is_complex",
}

REQUIRED_DB_FIELDS = ("study_id", "study_iuid", "modstudy", "groundtruth_pathology")


def _clean(value: str | None) -> str | None:
    """Empty / '#N/A' → None; otherwise stripped string."""
    if value is None:
        return None
    v = value.strip()
    if v == "" or v == "#N/A":
        return None
    return v


def _to_bool(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in ("true", "t", "1", "yes", "y")


def parse_row(raw: dict[str, str], lineno: int) -> dict[str, object] | None:
    """Build an INSERT parameter dict, or None if the row is unusable."""
    row: dict[str, object | None] = {}
    for csv_key, db_key in CSV_HEADER_MAP.items():
        row[db_key] = _clean(raw.get(csv_key))

    sid = row.get("study_id")
    if not sid:
        logger.warning("line %d: missing study_id, skipping", lineno)
        return None
    try:
        row["study_id"] = int(sid)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        logger.warning("line %d: non-integer study_id=%r, skipping", lineno, sid)
        return None

    for f in REQUIRED_DB_FIELDS:
        if f == "study_id":
            continue
        if not row.get(f):
            logger.warning(
                "line %d: missing required field %s for study_id=%s, skipping",
                lineno,
                f,
                row["study_id"],
            )
            return None

    # Bool conversion for is_complex (CSV carries "TRUE"/"FALSE"/empty).
    row["is_complex"] = _to_bool(row.get("is_complex"))  # type: ignore[arg-type]

    return row  # type: ignore[return-value]


async def _existing_ids_and_iuids(
    conn, study_ids: list[int], study_iuids: list[str]
) -> tuple[set[int], set[str]]:
    """Look up which study_ids and study_iuids are already in the DB.

    Pre-filtering by study_iuid here avoids a UniqueViolation aborting a
    whole batch when a test row carries a study_iuid that already lives
    on a different study_id.
    """
    existing_ids: set[int] = set()
    existing_iuids: set[str] = set()
    if study_ids:
        rows = await conn.execute(
            text(
                'SELECT study_id FROM rad_incubation."Study_Groundtruth" '
                "WHERE study_id = ANY(:ids)"
            ),
            {"ids": study_ids},
        )
        existing_ids = {r[0] for r in rows}
    if study_iuids:
        rows = await conn.execute(
            text(
                'SELECT study_iuid FROM rad_incubation."Study_Groundtruth" '
                "WHERE study_iuid = ANY(:iuids)"
            ),
            {"iuids": study_iuids},
        )
        existing_iuids = {r[0] for r in rows}
    return existing_ids, existing_iuids


async def run(csv_path: Path, dry_run: bool, batch_size: int) -> int:
    if not csv_path.exists():
        logger.error("CSV not found: %s", csv_path)
        return 2

    parsed: list[dict[str, object]] = []
    skipped_missing = 0

    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            logger.error("CSV appears to be empty")
            return 2

        missing_headers = [k for k in CSV_HEADER_MAP if k not in reader.fieldnames]
        if missing_headers:
            logger.error("CSV missing required headers: %s", missing_headers)
            return 2

        for i, raw in enumerate(reader, start=2):  # start=2 → first data row
            row = parse_row(raw, i)
            if row is None:
                skipped_missing += 1
                continue
            parsed.append(row)

    logger.info("parsed %d rows; %d skipped (missing fields)", len(parsed), skipped_missing)

    if not parsed:
        logger.info("nothing to insert")
        return 0

    # Pre-filter against the DB so batch inserts don't trip UNIQUE on study_iuid.
    all_ids = [int(r["study_id"]) for r in parsed]
    all_iuids = [str(r["study_iuid"]) for r in parsed]

    async with engine.begin() as conn:
        existing_ids, existing_iuids = await _existing_ids_and_iuids(
            conn, all_ids, all_iuids
        )

    insertable: list[dict[str, object]] = []
    skipped_existing_id = 0
    skipped_iuid_collision = 0
    for r in parsed:
        if int(r["study_id"]) in existing_ids:  # type: ignore[arg-type]
            skipped_existing_id += 1
            continue
        if str(r["study_iuid"]) in existing_iuids:  # type: ignore[arg-type]
            logger.warning(
                "study_iuid=%s already in DB on a different study_id; skipping study_id=%s",
                r["study_iuid"],
                r["study_id"],
            )
            skipped_iuid_collision += 1
            continue
        insertable.append(r)

    logger.info(
        "after DB pre-check: %d insertable, %d skipped (existing study_id), "
        "%d skipped (study_iuid collision)",
        len(insertable),
        skipped_existing_id,
        skipped_iuid_collision,
    )

    if dry_run:
        logger.info("dry-run: not writing to DB")
        for row in insertable[:3]:
            preview = {
                k: (v[:60] + "…" if isinstance(v, str) and len(v) > 60 else v)
                for k, v in row.items()
            }
            logger.info("would insert: %s", preview)
        return 0

    if not insertable:
        logger.info("nothing new to insert")
        return 0

    inserted = 0
    async with engine.begin() as conn:
        for start in range(0, len(insertable), batch_size):
            batch = insertable[start : start + batch_size]
            await conn.execute(INSERT_SQL, batch)
            inserted += len(batch)
            logger.info("inserted %d / %d", inserted, len(insertable))

    logger.info(
        "done. inserted=%d skipped_existing_id=%d skipped_iuid_collision=%d "
        "skipped_missing_fields=%d",
        inserted,
        skipped_existing_id,
        skipped_iuid_collision,
        skipped_missing,
    )
    return 0


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("csv", type=Path, help="path to test-cases CSV")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="parse + DB pre-check only — do not write",
    )
    p.add_argument("--batch-size", type=int, default=200)
    args = p.parse_args()
    rc = asyncio.run(run(args.csv, args.dry_run, args.batch_size))
    sys.exit(rc)


if __name__ == "__main__":
    main()
