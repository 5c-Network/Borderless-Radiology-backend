"""case_anonymization.py — pool-setup anonymisation, run once.

For every row in rad_incubation."Study_Groundtruth" whose dicom_metadata
column is NULL, this script:

  1) calls https://api.5cnetwork.com/dicom/v2/study?study_iuid=<old_uid>
     using the Authorization header value from 5C_API_AUTH_KEY,
  2) reshapes the response into the standard "dicomData" JSON schema,
  3) overwrites pat_id, pat_name_fk, study_iuid with anonymised values,
  4) writes the JSON into the row's dicom_metadata column AND copies the
     new study_iuid into the row's study_iuid column.

Idempotent: rows that already have dicom_metadata are skipped.
Safe:       aborts cleanly if any picked study_id already has a row in
            case_assignments or grading_jobs.

Required env (.env):
    DATABASE_URL          - Postgres URL (asyncpg).
    5C_API_AUTH_KEY       - Sent verbatim as the Authorization header.

Usage:
<<<<<<< HEAD
    python Borderless-Radiology-backend/scripts/case_anonymization.py
=======
    python scripts/case_anonymization.py
>>>>>>> bcb5126 (n8n flow integration)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import sys
import uuid
from typing import Any

import httpx
from dotenv import find_dotenv, load_dotenv
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


load_dotenv(find_dotenv(usecwd=True))

DATABASE_URL = os.environ.get("DATABASE_URL")
SC_API_KEY = os.environ.get("5C_API_AUTH_KEY") or os.environ.get("5C_API_auth_key")

DICOM_API_URL = "https://api.5cnetwork.com/dicom/v2/study"
HTTP_TIMEOUT = 30.0
SCHEMA = "rad_incubation"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s - %(message)s",
)
log = logging.getLogger("anonymise")


# Upstream API key  ->  standard dicomData key. Order matters: insertion
# order is preserved on json.dumps, so the JSON we store mirrors the
# canonical schema 1:1.
FIELD_MAP: list[tuple[str, str]] = [
    ("study_received_date_time", "created_time"),
    ("study_date",               "study_date"),
    ("study_iuid",               "study_iuid"),
    ("patient_sex",              "pat_sex"),
    ("patient_dob",              "pat_birthdate"),
    ("patient_id",               "pat_id"),
    ("modalities",               "mods_in_study"),
    ("instance_count",           "num_instances"),
    ("series_count",             "num_series"),
    ("patient_name",             "pat_name_fk"),
    ("accession_number",         "accession_number"),
    ("study_time",               "study_time"),
]

_DEMO_RE = re.compile(r"DEMO PATIENT-(\d+)", re.IGNORECASE)


def new_study_iuid() -> str:
    return f"2.25.{uuid.uuid4().int}"


def new_pat_id() -> str:
    return str(random.randint(10**9, 10**10 - 1))


def starting_demo_index(existing_blobs: list[str]) -> int:
    """Find the highest DEMO PATIENT-N already present so re-runs continue."""
    highest = 0
    for blob in existing_blobs:
        if not blob:
            continue
        try:
<<<<<<< HEAD
            name = json.loads(blob).get("pat_name_fk", "") or ""
        except (TypeError, ValueError):
            continue
=======
            obj = json.loads(blob)
        except (TypeError, ValueError):
            continue
        inner = obj.get("dicomData", obj) if isinstance(obj, dict) else {}
        name = inner.get("pat_name_fk", "") or ""
>>>>>>> bcb5126 (n8n flow integration)
        m = _DEMO_RE.search(name)
        if m:
            highest = max(highest, int(m.group(1)))
    return highest + 1


def reshape(api_obj: dict[str, Any]) -> dict[str, Any]:
    return {dst: api_obj.get(src) for src, dst in FIELD_MAP}


def anonymise(dicom_data: dict[str, Any], demo_index: int) -> dict[str, Any]:
    dicom_data["pat_name_fk"] = f"DEMO PATIENT-{demo_index}"
    dicom_data["pat_id"] = new_pat_id()
    dicom_data["study_iuid"] = new_study_iuid()
    return dicom_data


async def fetch_metadata(
    client: httpx.AsyncClient, study_iuid: str
) -> dict[str, Any] | None:
    try:
        r = await client.get(
            DICOM_API_URL,
            params={"study_iuid": study_iuid},
            headers={"Authorization": SC_API_KEY or ""},
        )
    except httpx.HTTPError as e:
        log.warning("study_iuid=%s -> network error: %s", study_iuid, e)
        return None
    if r.status_code != 200:
        log.warning("study_iuid=%s -> HTTP %s: %s",
                    study_iuid, r.status_code, r.text[:200])
        return None
    try:
        body = r.json()
    except ValueError:
        log.warning("study_iuid=%s -> non-JSON body", study_iuid)
        return None
    if not isinstance(body, list) or not body or not isinstance(body[0], dict):
        log.warning("study_iuid=%s -> unexpected body shape", study_iuid)
        return None
    return body[0]


async def main() -> int:
    if not DATABASE_URL:
        sys.exit("Missing DATABASE_URL in .env")
    if not SC_API_KEY:
        sys.exit(
            "Missing 5C_API_AUTH_KEY in .env. "
            "Add: 5C_API_AUTH_KEY=NWNuZXR3b3JrOjVjbmV0d29yaw=="
        )

    engine = create_async_engine(DATABASE_URL)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    summary: list[tuple[str, int, str, str, str]] = []
    try:
        async with Session() as session:
            rows = (
                await session.execute(
                    text(
                        f'SELECT study_id, study_iuid, modality '
                        f'FROM {SCHEMA}."Study_Groundtruth" '
                        f'WHERE dicom_metadata IS NULL '
                        f'ORDER BY study_id ASC'
                    )
                )
            ).all()
            if not rows:
                log.info("no rows with NULL dicom_metadata; nothing to do")
                return 0
            log.info("found %d rows with NULL dicom_metadata", len(rows))

            picked_ids = [r.study_id for r in rows]

            # Safety: refuse to swap study_iuid on rows already referenced
            # elsewhere in the schema; would orphan the assignment / grading.
            for tbl in ("case_assignments", "grading_jobs"):
                stmt = text(
                    f"SELECT DISTINCT study_id FROM {SCHEMA}.{tbl} "
                    f"WHERE study_id IN :ids"
                ).bindparams(bindparam("ids", expanding=True))
                clash = (
                    await session.execute(stmt, {"ids": picked_ids})
                ).scalars().all()
                if clash:
                    log.error(
                        "ABORTING: %d picked study_id(s) already exist in %s: %s",
                        len(clash), tbl, clash,
                    )
                    return 2

            existing_blobs = (
                await session.execute(
                    text(
                        f'SELECT dicom_metadata '
                        f'FROM {SCHEMA}."Study_Groundtruth" '
                        f'WHERE dicom_metadata IS NOT NULL'
                    )
                )
            ).scalars().all()
            next_demo = starting_demo_index(existing_blobs)
            log.info("DEMO PATIENT counter starts at %d", next_demo)

            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as http:
                for r in rows:
                    old_uid = r.study_iuid
                    api_obj = await fetch_metadata(http, old_uid)
                    if api_obj is None:
                        summary.append(
                            (r.modality or "?", r.study_id, old_uid, "", "skipped (api)")
                        )
                        continue

                    dicom_data = anonymise(reshape(api_obj), next_demo)
                    next_demo += 1
                    new_uid = dicom_data["study_iuid"]
<<<<<<< HEAD
                    blob = json.dumps(dicom_data, ensure_ascii=False)
=======
                    blob = json.dumps({"dicomData": dicom_data}, ensure_ascii=False)
>>>>>>> bcb5126 (n8n flow integration)

                    try:
                        await session.execute(
                            text(
                                f'UPDATE {SCHEMA}."Study_Groundtruth" '
                                f'SET study_iuid = :new_uid, '
                                f'    dicom_metadata = :blob '
                                f'WHERE study_id = :sid'
                            ),
                            {"new_uid": new_uid, "blob": blob, "sid": r.study_id},
                        )
                        await session.commit()
                    except Exception as e:
                        await session.rollback()
                        summary.append(
                            (r.modality or "?", r.study_id, old_uid, "", f"db error: {e}")
                        )
                        log.exception("study_id=%s db update failed", r.study_id)
                        continue

                    summary.append(
                        (r.modality or "?", r.study_id, old_uid, new_uid, "ok")
                    )
                    log.info(
                        "study_id=%s ok (%s, %s)",
                        r.study_id, dicom_data["pat_name_fk"], r.modality,
                    )
    finally:
        await engine.dispose()

    print()
    print(f"{'modality':<8} {'study_id':>10}  {'old_iuid':>13}  {'new_iuid':>13}  status")
    for mod, sid, old, new, st in summary:
        old_tail = "..." + (old or "")[-8:] if old else "—"
        new_tail = "..." + (new or "")[-8:] if new else "—"
        print(f"{mod:<8} {sid:>10}  {old_tail:>13}  {new_tail:>13}  {st}")
    ok = sum(1 for s in summary if s[4] == "ok")
    print(f"\n{ok}/{len(summary)} rows updated.")
    return 0 if ok == len(summary) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
