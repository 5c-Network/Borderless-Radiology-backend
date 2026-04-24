"""Slack alerts at 20-case gate, 80-case terminal, and 7-day timeout."""

from __future__ import annotations

import logging

import httpx

from app.config import get_settings
from app.models import CheckpointKind

logger = logging.getLogger(__name__)


_KIND_HEADERS: dict[CheckpointKind, str] = {
    CheckpointKind.gate_20: "20-case gate",
    CheckpointKind.terminal_80: "80-case terminal",
    CheckpointKind.terminal_7_days: "7-day timeout",
}


def build_slack_text(
    *,
    kind: CheckpointKind,
    rad_id: str,
    cases_evaluated: int,
    avg_score: float,
    overall_grade: str,
    quality_met: bool,
    summary: str,
) -> str:
    header = _KIND_HEADERS[kind]
    quality_line = "YES" if quality_met else f"NO (avg {avg_score:.2f})"

    if kind == CheckpointKind.terminal_7_days:
        count_line = f"Cases completed: {cases_evaluated} / 80"
    else:
        count_line = f"Cases evaluated: {cases_evaluated}"

    return (
        f"*Rad {rad_id} — {header}*\n"
        f"{count_line}\n"
        f"Quality met (grade 1): *{quality_line}*\n"
        f"Avg score: {avg_score:.2f} / 10 (overall grade {overall_grade})\n"
        f"{summary}"
    )


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
