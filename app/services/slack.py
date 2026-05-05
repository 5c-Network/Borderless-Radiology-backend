"""Slack alerts at 20-case gate, 80-case terminal, and 7-day timeout."""

from __future__ import annotations

import logging
from datetime import date

import httpx

from app.config import get_settings
from app.models import CheckpointKind

logger = logging.getLogger(__name__)


_KIND_HEADERS: dict[CheckpointKind, str] = {
    CheckpointKind.gate_20: "20-case gate",
    CheckpointKind.terminal_80: "80-case terminal",
    CheckpointKind.terminal_7_days: "7-day timeout",
}

_DOW = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


def format_slot_compliance_block(rows: list[dict]) -> str:
    """Render rad_slot_status rows as a fixed-width Slack block.

    Layout (one line per slot, multi-slot days produce multiple lines):
        *Slot compliance (incubation window):*
        ```
        2026-04-30 THU | 12AM - 3AM |   0 min |  0% | nil    | absent
        2026-05-01 FRI | 12AM - 3AM | 180 min |100% | full   | present
        ...
        ```

    Returns "*Slot compliance (incubation window):* _no rows scored yet_"
    when the list is empty (e.g., cron hasn't run for this rad yet).
    """
    if not rows:
        return "*Slot compliance (incubation window):* _no rows scored yet_"

    lines = ["*Slot compliance (incubation window):*", "```"]
    for r in rows:
        try:
            mins = int(r["total_active_minutes"])
            committed_min = int(r["committed_hours"]) * 60
            pct = (mins * 100 // committed_min) if committed_min else 0
        except (TypeError, ValueError):
            mins = r.get("total_active_minutes", "?")
            pct = "?"
        # Day-of-week short label from slot_date.
        try:
            dow = _DOW[date.fromisoformat(str(r["slot_date"])).weekday()]
        except Exception:  # noqa: BLE001
            dow = "?"
        slot_name = str(r.get("slot_name", "?"))
        avail = str(r.get("rad_availability", "?"))
        comp = str(r.get("compliance_status", "?"))
        lines.append(
            f"{r['slot_date']} {dow} | {slot_name:<11s} | "
            f"{mins:>4} min | {pct:>3}% | {avail:<7s} | {comp}"
        )
    lines.append("```")
    return "\n".join(lines)


def build_slack_text(
    *,
    kind: CheckpointKind,
    rad_id: str,
    cases_evaluated: int,
    avg_score: float,
    overall_grade: str,
    quality_met: bool,
    summary: str,
    slot_summary: str | None = None,
) -> str:
    header = _KIND_HEADERS[kind]
    quality_line = "YES" if quality_met else f"NO (avg {avg_score:.2f})"

    if kind == CheckpointKind.terminal_7_days:
        count_line = f"Cases completed: {cases_evaluated} / 80"
    else:
        count_line = f"Cases evaluated: {cases_evaluated}"

    text = (
        f"*Rad {rad_id} — {header}*\n"
        f"{count_line}\n"
        f"Quality met (grade 1): *{quality_line}*\n"
        f"Avg score: {avg_score:.2f} / 10 (overall grade {overall_grade})\n"
        f"{summary}"
    )
    if slot_summary:
        text = f"{text}\n\n{slot_summary}"
    return text


async def send_slack_alert(text: str) -> tuple[bool, str | None]:
    settings = get_settings()
    if not settings.slack_webhook_url:
        logger.warning("SLACK_WEBHOOK_URL not configured; skipping Slack alert")
        return False, "webhook_not_configured"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(settings.slack_webhook_url, json={"text": text})
            if resp.status_code >= 300:
                return False, f"http_{resp.status_code}: {resp.text[:200]}"
            return True, None
    except Exception as e:  # noqa: BLE001
        return False, str(e)
