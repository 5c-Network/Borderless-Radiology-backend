"""Upload borderless_db.csv into rad_incubation."Study_Groundtruth".

One-shot ingestion. Re-runs are safe (upsert on study_id).

Order of execution on the VM:
    1. alembic upgrade head           (apply 20260428_0002 first)
    2. python scripts/upload_groundtruth_csv.py /path/to/borderless_db.csv
    3. <run your separate study_iuid extraction>
    4. python scripts/classify_groundtruth_pathologies.py

Notes:
  - `study_iuid` is taken directly from the CSV's `study_iuid` column.
    `old_study_iuid` is no longer populated by this script (column is
    nullable and may be dropped in a future migration). On re-run the
    upsert does NOT clobber an existing row's `study_iuid`.
  - Empty cells and `#N/A` are stored as NULL.
  - `rules` is stored verbatim as text. `dicom_metadata` is left NULL by
    this script and populated later by scripts/case_anonymization.py.

Usage:
    python scripts/upload_groundtruth_csv.py path/to/borderless_db.csv
    python scripts/upload_groundtruth_csv.py path/to/borderless_db.csv --dry-run
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

logger = logging.getLogger("upload_groundtruth_csv")


# study_iuid is in the INSERT column list but intentionally NOT in the
# ON CONFLICT UPDATE list, so a re-run cannot clobber an existing row's
# study_iuid. old_study_iuid is no longer populated.
UPSERT_SQL = text(
    """
    INSERT INTO rad_incubation."Study_Groundtruth" (
        study_id,
        study_iuid,
        modstudy,
        groundtruth_pathology,
        modality,
        history,
        rules,
        category,
        observation,
        impression,
        age,
        case_type
    ) VALUES (
        :study_id,
        :study_iuid,
        :modstudy,
        :groundtruth_pathology,
        :modality,
        :history,
        :rules,
        :category,
        :observation,
        :impression,
        :age,
        :case_type
    )
    ON CONFLICT (study_id) DO UPDATE SET
        modstudy              = EXCLUDED.modstudy,
        groundtruth_pathology = EXCLUDED.groundtruth_pathology,
        modality              = EXCLUDED.modality,
        history               = EXCLUDED.history,
        rules                 = EXCLUDED.rules,
        category              = EXCLUDED.category,
        observation           = EXCLUDED.observation,
        impression            = EXCLUDED.impression,
        age                   = EXCLUDED.age,
        updated_at            = (now() AT TIME ZONE 'Asia/Kolkata')
    -- case_type is intentionally NOT in the UPDATE list. It is set on
    -- INSERT only, so a re-run of this script can never reclassify an
    -- existing row (e.g. flip a production row to 'test'). Test rows
    -- are owned by scripts/upload_test_cases_csv.py.
    """
)


# CSV header → DB column name. Required columns: every CSV must carry these.
CSV_HEADER_MAP = {
    "study_id": "study_id",
    "study_iuid": "study_iuid",
    "modalities": "modality",
    "pathology": "groundtruth_pathology",
    "category": "category",
    "observation": "observation",
    "impression": "impression",
    "history": "history",
    "age": "age",
    "modstudy": "modstudy",
    "rules": "rules",
}

# Optional CSV columns. Missing column → DB column stays NULL on insert
# and (because EXCLUDED is also NULL) NULL on upsert. Present-but-empty
# cells behave identically.
CSV_OPTIONAL_HEADER_MAP = {
    "case_type": "case_type",
}


def _clean(value: str | None) -> str | None:
    """Normalise CSV cells: empty / '#N/A' → None; otherwise stripped string."""
    if value is None:
        return None
    v = value.strip()
    if v == "" or v == "#N/A":
        return None
    return v


def parse_row(raw: dict[str, str], lineno: int) -> dict[str, object] | None:
    """Build a parameter dict for the upsert, or None if the row is unusable."""
    row: dict[str, object | None] = {}
    for csv_key, db_key in CSV_HEADER_MAP.items():
        row[db_key] = _clean(raw.get(csv_key))
    for csv_key, db_key in CSV_OPTIONAL_HEADER_MAP.items():
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

    if not row.get("modstudy"):
        logger.warning(
            "line %d: missing modstudy for study_id=%s, skipping",
            lineno,
            row["study_id"],
        )
        return None
    if not row.get("groundtruth_pathology"):
        logger.warning(
            "line %d: missing pathology for study_id=%s, skipping",
            lineno,
            row["study_id"],
        )
        return None

    if not row.get("study_iuid"):
        logger.warning(
            "line %d: missing study_iuid for study_id=%s, skipping",
            lineno,
            row["study_id"],
        )
        return None
    return row  # type: ignore[return-value]


async def run(csv_path: Path, dry_run: bool, batch_size: int) -> int:
    if not csv_path.exists():
        logger.error("CSV not found: %s", csv_path)
        return 2

    parsed: list[dict[str, object]] = []
    skipped = 0

    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            logger.error("CSV appears to be empty")
            return 2

        missing = [k for k in CSV_HEADER_MAP if k not in reader.fieldnames]
        if missing:
            logger.error("CSV missing required columns: %s", missing)
            return 2

        for i, raw in enumerate(reader, start=2):  # start=2 → first data row
            row = parse_row(raw, i)
            if row is None:
                skipped += 1
                continue
            parsed.append(row)

    logger.info("parsed %d rows; %d skipped", len(parsed), skipped)

    if dry_run:
        logger.info("dry-run: not writing to DB")
        for row in parsed[:3]:
            preview = {
                k: (v[:60] + "…" if isinstance(v, str) and len(v) > 60 else v)
                for k, v in row.items()
            }
            logger.info("sample row: %s", preview)
        return 0

    if not parsed:
        logger.info("nothing to upsert")
        return 0

    written = 0
    async with engine.begin() as conn:
        for start in range(0, len(parsed), batch_size):
            batch = parsed[start : start + batch_size]
            await conn.execute(UPSERT_SQL, batch)
            written += len(batch)
            logger.info("upserted %d / %d", written, len(parsed))

    logger.info("done. upserted=%d skipped=%d", written, skipped)
    return 0


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("csv", type=Path, help="path to borderless_db.csv")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="parse and validate only — do not write to DB",
    )
    p.add_argument("--batch-size", type=int, default=200)
    args = p.parse_args()
    rc = asyncio.run(run(args.csv, args.dry_run, args.batch_size))
    sys.exit(rc)


if __name__ == "__main__":
    main()
