"""End-to-end test of the grade_case + checkpoint + notification flow.

Targets a configurable rad_id against the deployed FastAPI server
(https://api.borderless.5cnetwork.com).

Usage:
    python scripts/_run_grade_test.py \
        --rad-id 2105 --target 80 --profile pass

Profiles:
    pass    Author close paraphrases for every case -> drives grade=1
            (used to test gate_20 silent + terminal_80 INFO/BORDERLESS).
    fail    Mixed authoring with mostly-bad reports -> drives quality_met=false
            (used to test BLOCK PATCH branches).

Steps:
  1. Probe DB to learn current assignment count for the rad.
  2. Fire `case-submitted` webhooks (CT,MRI) until the rad has `target` assignments.
  3. Fetch ground truth for the assigned study_iuids.
  4. Author candidate reports per the chosen profile.
  5. POST /api/v1/grade_case sequentially, capture grading_ids.
  6. Poll each grading_id until status in {done, error}.
  7. Print a verification report: per-case grades + every checkpoint_events row.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from typing import Any

import asyncpg
import httpx
from dotenv import load_dotenv

load_dotenv()

API_BASE_URL = os.environ.get(
    "API_BASE_URL", "https://api.borderless.5cnetwork.com"
)
MODALITIES = ["CT", "MRI"]
AUTH = os.environ.get("API_AUTH_KEY", "to5y7HyOAx3Q1")
HEADERS = {"Authorization": AUTH, "Content-Type": "application/json"}


def _to_asyncpg_dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _db_conn() -> asyncpg.Connection:
    return await asyncpg.connect(_to_asyncpg_dsn(os.environ["DATABASE_URL"]))


# ---------- seed assignments via /incubation/webhook ----------


async def seed_assignments_to(
    client: httpx.AsyncClient, rad_id: str, rad_id_int: int, target: int
) -> int:
    conn = await _db_conn()
    try:
        current = await conn.fetchval(
            "SELECT COUNT(*) FROM rad_incubation.case_assignments WHERE rad_id = $1",
            rad_id,
        )
    finally:
        await conn.close()

    print(f"[seed] current assignments for rad {rad_id}: {current}/{target}")
    while current < target:
        body = {
            "event": "case-submitted",
            "rad_id": rad_id_int,
            "modalities": MODALITIES,
        }
        r = await client.post(
            f"{API_BASE_URL}/api/v1/incubation/webhook", json=body, headers=HEADERS
        )
        r.raise_for_status()
        data = r.json()
        added = data.get("cases_assigned_now", 0)
        print(
            f"[seed] case-submitted -> assigned {added}, "
            f"rad_status={data.get('rad_status')}, "
            f"message={data.get('message')!r}"
        )
        if added == 0:
            print("[seed] webhook returned 0 cases; aborting seed loop")
            break
        current += added
    print(f"[seed] final assignments: {current}")
    return current


# ---------- fetch ground truth for the rad's assigned study_iuids ----------


async def fetch_assignments_with_groundtruth(
    rad_id: str, target: int
) -> list[dict[str, Any]]:
    conn = await _db_conn()
    try:
        rows = await conn.fetch(
            """
            SELECT a.case_number,
                   a.study_iuid,
                   a.study_id,
                   sg.modstudy,
                   sg.history,
                   sg.observation     AS gt_observation,
                   sg.impression      AS gt_impression,
                   sg.groundtruth_pathology,
                   sg.main_pathologies,
                   sg.modality
            FROM rad_incubation.case_assignments a
            JOIN rad_incubation."Study_Groundtruth" sg
              ON sg.study_iuid = a.study_iuid
            WHERE a.rad_id = $1
            ORDER BY a.case_number ASC
            LIMIT $2
            """,
            rad_id,
            target,
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()


# ---------- author candidate reports ----------


def author_pass(idx: int, gt: dict[str, Any]) -> dict[str, str]:
    """Close paraphrase of GT for every case -> expected grade=1."""
    gt_obs = (gt.get("gt_observation") or "").strip()
    gt_imp = (gt.get("gt_impression") or "").strip()
    obs = (
        "Findings on review:\n"
        + (gt_obs if gt_obs else "Imaging reviewed.")
    )
    imp = (
        "Impression: "
        + (gt_imp if gt_imp else "Findings as above.")
    )
    return {"observation": obs, "impression": imp}


def author_fail(idx: int, gt: dict[str, Any]) -> dict[str, str]:
    """Mixed reports designed to drive overall_grade != "1"."""
    gt_obs = (gt.get("gt_observation") or "").strip()
    gt_imp = (gt.get("gt_impression") or "").strip()

    if idx < 8:
        obs = "Findings on review:\n" + (gt_obs if gt_obs else "Imaging reviewed.")
        imp = "Impression: " + (gt_imp if gt_imp else "Findings as above.")
        return {"observation": obs, "impression": imp}
    if idx < 14:
        return {
            "observation": (
                "Imaging reviewed. No acute intracranial abnormality identified. "
                "Visualised structures appear within normal limits."
            ),
            "impression": "Impression: No significant abnormality on the current study.",
        }
    if idx < 18:
        return {
            "observation": (
                "Imaging reviewed. There is suspicion of a small acute haemorrhagic "
                "focus. Mild surrounding oedema noted. Other structures unremarkable."
            ),
            "impression": (
                "Impression: Suspected acute haemorrhage. Recommend urgent clinical "
                "correlation and follow-up imaging."
            ),
        }
    return {
        "observation": "Study reviewed. Unremarkable.",
        "impression": "Impression: No abnormality detected.",
    }


PROFILES = {"pass": author_pass, "fail": author_fail}


# ---------- POST /api/v1/grade_case ----------


async def post_grade_cases(
    client: httpx.AsyncClient,
    rad_id: str,
    items: list[dict[str, Any]],
    profile: str,
) -> list[dict[str, Any]]:
    author = PROFILES[profile]
    posted: list[dict[str, Any]] = []
    for i, gt in enumerate(items):
        report_text = author(i, gt)
        payload = {
            "rad_id": rad_id,
            "report": {
                "observation": report_text["observation"],
                "impression": report_text["impression"],
                "history": (gt.get("history") or "")[:2000],
                "modstudy": gt.get("modstudy") or "",
                "study_iuid": gt["study_iuid"],
                "study_id": "1234533",
                "report_id": "287468",
            },
        }
        r = await client.post(
            f"{API_BASE_URL}/api/v1/grade_case", json=payload, headers=HEADERS
        )
        if r.status_code != 202:
            print(f"[post {i+1}] FAIL HTTP {r.status_code}: {r.text[:200]}")
            posted.append({"case_number": gt["case_number"], "error": r.text[:200]})
            continue
        data = r.json()
        print(
            f"[post {i+1}/{len(items)}] case_number={gt['case_number']} "
            f"-> grading_id={data['grading_id']} status={data['status']}"
        )
        posted.append(
            {
                "case_number": gt["case_number"],
                "study_iuid": gt["study_iuid"],
                "grading_id": data["grading_id"],
            }
        )
        await asyncio.sleep(0.5)
    return posted


# ---------- poll until done ----------


async def poll_until_done(
    client: httpx.AsyncClient, posted: list[dict[str, Any]], timeout_s: int = 600
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    deadline = time.time() + timeout_s
    pending = list(posted)
    while pending and time.time() < deadline:
        next_pending: list[dict[str, Any]] = []
        for item in pending:
            if "error" in item:
                out.append({**item, "status": "post_error"})
                continue
            r = await client.get(
                f"{API_BASE_URL}/api/v1/grade_case/{item['grading_id']}",
                headers=HEADERS,
            )
            if r.status_code != 200:
                next_pending.append(item)
                continue
            data = r.json()
            if data["status"] in ("done", "error"):
                out.append({**item, **data})
            else:
                next_pending.append(item)
        if next_pending:
            print(f"[poll] {len(next_pending)} still running, waiting 3s")
            await asyncio.sleep(3.0)
        pending = next_pending
    if pending:
        print(f"[poll] TIMEOUT — {len(pending)} jobs not finished")
        for item in pending:
            out.append({**item, "status": "timeout"})
    return out


# ---------- verify ----------


async def verify_checkpoint(rad_id: str) -> None:
    conn = await _db_conn()
    try:
        rad = await conn.fetchrow(
            "SELECT status, cases_completed FROM rad_incubation.rad_state WHERE rad_id = $1",
            rad_id,
        )
        print(f"\n[verify] rad_state after run: {dict(rad) if rad else None}")

        cp = await conn.fetch(
            """
            SELECT kind, cases_evaluated, avg_score, overall_grade, quality_met,
                   grade_counts, callback_status, callback_attempts,
                   callback_last_error, slack_sent, slack_last_error,
                   evaluated_at
            FROM rad_incubation.checkpoint_events
            WHERE rad_id = $1
            ORDER BY evaluated_at ASC
            """,
            rad_id,
        )
        print(f"[verify] checkpoint_events ({len(cp)} row(s)):")
        for r in cp:
            d = dict(r)
            if isinstance(d.get("grade_counts"), str):
                d["grade_counts"] = json.loads(d["grade_counts"])
            d["avg_score"] = float(d["avg_score"]) if d["avg_score"] is not None else None
            print(json.dumps(d, indent=2, default=str))
    finally:
        await conn.close()


async def amain(rad_id: str, target: int, profile: str) -> None:
    rad_id_int = int(rad_id)
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(
            f"{API_BASE_URL}/api/v1/rad/{rad_id}/grades", headers=HEADERS
        )
        r.raise_for_status()

        final_count = await seed_assignments_to(client, rad_id, rad_id_int, target)
        items = await fetch_assignments_with_groundtruth(rad_id, target)
        print(f"[gt] fetched ground truth for {len(items)} assigned cases")
        if len(items) < target:
            print(f"[gt] expected {target} but got {len(items)}; aborting")
            sys.exit(1)

        posted = await post_grade_cases(client, rad_id, items, profile)
        results = await poll_until_done(client, posted)

        print("\n[results] per-case outcome:")
        for r in results:
            print(
                f"  case={r.get('case_number')} status={r.get('status')} "
                f"grade={r.get('grade')} score={r.get('score_10pt')} "
                f"critical_miss={r.get('critical_miss')}"
            )

        await verify_checkpoint(rad_id)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--rad-id", required=True)
    p.add_argument("--target", type=int, required=True)
    p.add_argument("--profile", choices=list(PROFILES), default="pass")
    args = p.parse_args()
    asyncio.run(amain(args.rad_id, args.target, args.profile))


if __name__ == "__main__":
    main()
