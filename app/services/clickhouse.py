"""Thin async ClickHouse HTTP client.

Read-only. Uses httpx (already a dep) so no new package is required.

ClickHouse server: configured via CLICKHOUSE_HOST/PORT/USER/PASSWORD env.
If CLICKHOUSE_HOST is empty, queries raise ClickHouseDisabled — used as the
"feature off" signal so the rad_slot_status pipeline can no-op cleanly in
environments without a CH endpoint (e.g., local dev without CH access).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


class ClickHouseDisabled(RuntimeError):
    """Raised when CH config is missing — call sites should treat as 'skip'."""


class ClickHouseQueryError(RuntimeError):
    """Raised when ClickHouse rejects a query (HTTP non-200)."""


class ClickHouseClient:
    """One client instance per call-site. Cheap to construct; no pooling here.

    All queries return a list of dicts (one per row) using JSONEachRow format.
    Caller is responsible for parsing typed values (e.g., DateTime strings).
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._host = settings.clickhouse_host
        self._port = settings.clickhouse_port
        self._user = settings.clickhouse_user
        self._password = settings.clickhouse_password
        self._timeout = settings.clickhouse_timeout_s
        if not self._host:
            raise ClickHouseDisabled(
                "CLICKHOUSE_HOST is empty; rad_slot_status feature disabled"
            )

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._port}/"

    async def query(self, sql: str) -> list[dict[str, Any]]:
        """Run a SELECT, return rows. Caller-supplied SQL — do not pass
        untrusted input."""
        timeout = httpx.Timeout(
            connect=min(20.0, self._timeout),
            read=self._timeout,
            write=10.0,
            pool=10.0,
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                self.url,
                params={"query": f"{sql} FORMAT JSONEachRow"},
                auth=(self._user, self._password),
            )
        if response.status_code != 200:
            raise ClickHouseQueryError(
                f"ClickHouse query failed ({response.status_code}): "
                f"{response.text[:500]}"
            )
        text = response.text.strip()
        if not text:
            return []
        return [json.loads(line) for line in text.splitlines() if line.strip()]
