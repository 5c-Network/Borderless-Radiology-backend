"""Upload borderless_db.csv into rad_incubation."Study_Groundtruth".

One-shot ingestion. Re-runs are safe (upsert on study_id).

Order of execution on the VM:
    1. alembic upgrade head           (apply 20260428_0002 first)
    2. python scripts/upload_groundtruth_csv.py /path/to/borderless_db.csv
    3. <run your separate study_iuid extraction>
    4. python scripts/classify_groundtruth_pathologies.py

Notes:
  - `study_iuid` is filled with the value of `old_study_iuid` as a placeholder
    so the existing NOT NULL UNIQUE constraint is satisfied. Your extraction
    script overwrites `study_iuid` with the real yotta-pushed IUID later.
    On re-run of this script the placeholder is set ONLY for newly inserted
    rows; existing rows keep whatever study_iuid they have (i.e. the value
    your extraction script wrote).
  - Empty cells and `#N/A` are stored as NULL.
  - `rules` and `dicom_metadata` are stored verbatim as text.

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


# IMPORTANT: study_iuid is in the INSERT column list (placeholder = old_study_iuid)
# but is intentionally NOT in the ON CONFLICT UPDATE list. This way an
# existing row's study_iuid (set by the extraction script) is never
# clobbered by a re-run of this upload.
UPSERT_SQL = text(
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
        age
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
        :age
    )
    ON CONFLICT (study_id) DO UPDATE SET
        old_study_iuid        = EXCLUDED.old_study_iuid,
        modstudy              = EXCLUDED.modstudy,
        groundtruth_pathology = EXCLUDED.groundtruth_pathology,
        modality              = EXCLUDED.modality,
        history               = EXCLUDED.history,
        dicom_metadata        = EXCLUDED.dicom_metadata,
        rules                 = EXCLUDED.rules,
        category              = EXCLUDED.category,
        observation           = EXCLUDED.observation,
        impression            = EXCLUDED.impression,
        age                   = EXCLUDED.age,
        updated_at            = (now() AT TIME ZONE 'Asia/Kolkata')
    """
)


# CSV header → DB column name.
CSV_HEADER_MAP = {
    "study_id": "study_id",
    "old_study_iuid": "old_study_iuid",
    "Modality": "modality",
    "pathology": "groundtruth_pathology",
    "Category": "category",
    "Observation": "observation",
    "Impression": "impression",
    "history": "history",
    "age": "age",
    "modstudy": "modstudy",
    "rules": "rules",
    "dicom_metadata": "dicom_metadata",
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

    old_iuid = row.get("old_study_iuid")
    if not old_iuid:
        logger.warning(
            "line %d: missing old_study_iuid for study_id=%s, skipping",
            lineno,
            row["study_id"],
        )
        return None

    # study_iuid placeholder. The separate extraction script overwrites this
    # with the real yotta-pushed IUID; the upsert above does NOT clobber
    # study_iuid on conflict, so the extraction's value persists across
    # re-runs of this script.
    row["study_iuid"] = old_iuid
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
