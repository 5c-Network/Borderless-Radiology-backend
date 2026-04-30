"""PATCH the platform notification + phase-flip API.

One endpoint, two body shapes:

  send_notice                  Notice-only (gate-20 BLOCK, terminal BLOCK,
                               or clear).
                               body: {"notice": {...}} | {"notice": null}

  send_borderless_qualified    Phase-flip + INFO in one call (terminal eligible).
                               body: {"phase": "BORDERLESS",
                                      "qualified_at": "YYYY-MM-DD",
                                      "notice": {...}}

Both PATCH /user/radiologist/{rad_id}/borderless. The platform returns the
rad's full state (phase, notice, commitment, qualified_at) on every call.

Both return (ok: bool, err: str | None). No exceptions to caller — failures
are persisted on the checkpoint row and surfaced via Slack.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


async def send_notice(
    rad_id: str, body: dict[str, Any] | None
) -> tuple[bool, str | None]:
    """PATCH /user/radiologist/{rad_id}/borderless with notice only.

    body is either {"notice": {...}} for BLOCK, or None for clear (we send
    {"notice": null} on the wire).
    """
    payload = body if body is not None else {"notice": None}
    return await _patch(
        path=f"/user/radiologist/{rad_id}/borderless",
        payload=payload,
    )


async def send_borderless_qualified(
    rad_id: str,
    qualified_at: date,
    notice: dict[str, Any],
) -> tuple[bool, str | None]:
    """PATCH /user/radiologist/{rad_id}/borderless with phase + notice.

    Flips the rad to the Borderless phase and shows the eligible notice in a
    single call. notice is the {"kind": "INFO", "title": ..., "body": ...}
    dict produced by build_notice.
    """
    payload = {
        "phase": "BORDERLESS",
        "qualified_at": qualified_at.isoformat(),
        "notice": notice,
    }
    return await _patch(
        path=f"/user/radiologist/{rad_id}/borderless",
        payload=payload,
    )


async def _patch(
    *, path: str, payload: dict[str, Any]
) -> tuple[bool, str | None]:
    settings = get_settings()
    if not settings.external_callback_url:
        logger.warning("EXTERNAL_CALLBACK_URL not configured; skipping PATCH %s", path)
        return False, "url_not_configured"

    base = settings.external_callback_url.rstrip("/")
    url = f"{base}{path}"
    headers = {"Content-Type": "application/json"}
    if settings.external_callback_key:
        headers["Authorization"] = settings.external_callback_key

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.patch(url, json=payload, headers=headers)
            if resp.status_code >= 300:
                return False, f"http_{resp.status_code}: {resp.text[:200]}"
            return True, None
    except Exception as e:  # noqa: BLE001
        return False, str(e)
