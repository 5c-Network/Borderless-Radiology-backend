"""Push checkpoint results to the external decision system.

The external system is TBD — URL + auth key are config. If the URL isn't set
we skip the call (row stays callback_status=pending, can be retried later).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


async def send_external_callback(payload: dict[str, Any]) -> tuple[bool, str | None]:
    settings = get_settings()
    if not settings.external_callback_url:
        logger.warning("EXTERNAL_CALLBACK_URL not configured; skipping callback")
        return False, "url_not_configured"

    headers = {"Content-Type": "application/json"}
    if settings.external_callback_key:
        headers["X-API-Key"] = settings.external_callback_key

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                settings.external_callback_url, json=payload, headers=headers
            )
            if resp.status_code >= 300:
                return False, f"http_{resp.status_code}: {resp.text[:200]}"
            return True, None
    except Exception as e:  # noqa: BLE001
        return False, str(e)
