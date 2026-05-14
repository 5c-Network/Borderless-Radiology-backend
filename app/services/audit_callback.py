"""Post-grading audit callback to api.5cnetwork.com/report/audit-result.

Fire-and-forget from run_grading_job after the LLM result lands. Builds the
payload from the GradingJob row + matching Study_Groundtruth, persists it
into grading_jobs.audit_json BEFORE the POST so the audit trail survives a
total network failure, then POSTs once with a single retry-after-5s on
transient errors. 4xx errors do not retry.

The grading row's outcome is NEVER affected by audit failures. Failures are
recorded into audit_json.post_error and logged once after retries are
exhausted.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import SessionLocal
from app.models import GradingJob, StudyGroundtruth

logger = logging.getLogger(__name__)


# ---------- payload helpers --------------------------------------------------


_SEVERITY_BY_GRADE = {
    "1":  "No Changes",
    "2A": "Incidental Findings",
    "2B": "Incidental Findings",
    "3A": "Minor - Clinically Significant",
    "3B": "Major - Clinically Significant",
}


def _derive_severity(grade: str | None) -> str | None:
    if grade is None:
        return None
    return _SEVERITY_BY_GRADE.get(grade.upper())


# Upstream column is varchar(255); leave a small buffer for the ellipsis.
_MAX_TYPE_LEN = 252


def _derive_type(llm_raw_json: dict[str, Any] | None) -> str | None:
    """Descriptive type string naming every missed / over-called pathology
    verbatim from the LLM output. Examples:

      "Missed - fibrotic strands, lung cysts, degenerative changes"
      "Overcalled - subcentimetric lymph nodes"
      "Missed - mild soft tissue thickening, Overcalled - subcentimetric lymph nodes"

    Returns None if neither set has items (e.g. grade-1 perfect read).
    Main and incidental misses are flattened into a single "Missed -" clause.
    Truncated at the last clean comma if the full string would exceed the
    upstream column's varchar(255) limit; truncated marker is "...".
    """
    if not llm_raw_json:
        return None
    missed = list(llm_raw_json.get("main_pathologies_missed") or []) + list(
        llm_raw_json.get("incidental_findings_missed") or []
    )
    overcalls = list(llm_raw_json.get("overcalls") or [])
    clauses: list[str] = []
    if missed:
        clauses.append("Missed - " + ", ".join(str(x) for x in missed))
    if overcalls:
        clauses.append("Overcalled - " + ", ".join(str(x) for x in overcalls))
    if not clauses:
        return None
    full = ", ".join(clauses)
    if len(full) <= _MAX_TYPE_LEN:
        return full
    # Truncate at the last clean ", " boundary before the limit so we never
    # cut a pathology name in half.
    head = full[:_MAX_TYPE_LEN]
    last_comma = head.rfind(", ")
    if last_comma > 0:
        head = head[:last_comma]
    return head + "..."


def _extract_candidate_report_id(raw_payload: dict[str, Any] | None) -> int | None:
    if not raw_payload:
        return None
    report = raw_payload.get("report") if isinstance(raw_payload, dict) else None
    if not isinstance(report, dict):
        return None
    rid = report.get("report_id")
    return int(rid) if rid is not None else None


def _build_payload(job: GradingJob, gt: StudyGroundtruth | None) -> dict[str, Any]:
    return {
        "report_fk": _extract_candidate_report_id(job.raw_payload),
        "revised_report_fk": (gt.report_id if gt is not None else None),
        "delta": job.llm_rationale or "",
        "severity": _derive_severity(job.grade),
        # Upstream API requires lowercase: "1" | "2a" | "2b" | "3a" | "3b".
        "grade": (job.grade or "").lower() or None,
        "overcall_type": "CLIENT_OVERCALL",
        "status": "COMPLETED",
        "type": _derive_type(job.llm_raw_json),
        "dispute": None,
        "severity_history": None,
    }


# ---------- HTTP with single retry ------------------------------------------


_RETRY_DELAY_S = 5.0
_TIMEOUT_S = 10.0


async def _post_with_retry(
    url: str, headers: dict[str, str], payload: dict[str, Any]
) -> tuple[int | None, str | None, int]:
    """Returns (http_status, error_message, attempts).

    On 2xx: (status, None, attempts).
    On 4xx: (status, body[:200], 1)  — no retry, treated as client error.
    On 5xx / timeout / connection error on attempt 1: retry once after 5s.
      Attempt 2 success: (status, None, 2)
      Attempt 2 failure: (status_or_None, last_error, 2)
    """
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
                resp = await client.post(url, json=payload, headers=headers)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_err = f"{type(e).__name__}: {e}"
            if attempt == 1:
                await asyncio.sleep(_RETRY_DELAY_S)
                continue
            return None, last_err, 2
        # Got a response.
        if 200 <= resp.status_code < 300:
            return resp.status_code, None, attempt
        # 4xx: client error, no retry.
        if 400 <= resp.status_code < 500:
            return resp.status_code, f"client error: {resp.text[:200]}", attempt
        # 5xx: transient — retry once.
        if attempt == 1:
            await asyncio.sleep(_RETRY_DELAY_S)
            continue
        return resp.status_code, f"server error: {resp.text[:200]}", 2
    # unreachable
    return None, "exhausted", 2


# ---------- main entry point -------------------------------------------------


async def post_audit_result(grading_id: str) -> None:
    """Fire-and-forget audit POST + audit_json persistence for a graded row.

    Uses its own SessionLocal so it doesn't share state with the grader's
    session. Never raises — failures land in audit_json.post_error.
    """
    settings = get_settings()

    async with SessionLocal() as session:
        try:
            await _run(session, grading_id, settings)
        except Exception:  # noqa: BLE001
            # Defensive: never let a callback bug fail the graded run.
            logger.exception("audit callback raised for grading_id=%s", grading_id)


async def _run(session: AsyncSession, grading_id: str, settings) -> None:
    job = await session.get(GradingJob, grading_id)
    if job is None:
        logger.warning("audit callback: grading_id=%s not found", grading_id)
        return

    gt = (
        await session.execute(
            select(StudyGroundtruth).where(StudyGroundtruth.study_iuid == job.study_iuid)
        )
    ).scalar_one_or_none()

    payload = _build_payload(job, gt)

    # Persist the payload to audit_json BEFORE the POST so the audit trail
    # survives even a total network outage.
    job.audit_json = dict(payload)
    await session.commit()

    if not settings.audit_api_enabled or not settings.audit_api_url:
        logger.info(
            "audit callback skipped (enabled=%s, url_set=%s) grading_id=%s",
            settings.audit_api_enabled,
            bool(settings.audit_api_url),
            grading_id,
        )
        job.audit_json = {
            **payload,
            "posted_at": None,
            "http_status": None,
            "post_error": "callback_disabled",
            "attempts": 0,
        }
        await session.commit()
        return

    headers = {"Content-Type": "application/json"}
    if settings.audit_api_auth_key:
        headers["Authorization"] = settings.audit_api_auth_key

    status_code, error, attempts = await _post_with_retry(
        settings.audit_api_url, headers, payload
    )

    # Re-fetch the row for the commit (session was committed above).
    job = await session.get(GradingJob, grading_id)
    if job is None:
        return

    if error is None:
        job.audit_json = {
            **payload,
            "posted_at": datetime.now(timezone.utc).isoformat(),
            "http_status": status_code,
            "attempts": attempts,
        }
        logger.info(
            "audit POST ok grading_id=%s status=%s attempts=%s",
            grading_id, status_code, attempts,
        )
    else:
        job.audit_json = {
            **payload,
            "posted_at": None,
            "http_status": status_code,
            "post_error": error,
            "attempts": attempts,
        }
        logger.error(
            "audit POST failed grading_id=%s status=%s attempts=%s err=%s",
            grading_id, status_code, attempts, error,
        )
    await session.commit()
