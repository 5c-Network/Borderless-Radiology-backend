"""Parse rad commitment JSON and expand into per-slot instances.

The borderless commitment lives in transform.RadDetails.other_details and
is mirrored byte-identical onto rad_state.other_details by the T1 hook
(populate_other_details). The shape is:

    {
      "borderless": {
        "commitment": {
          "slots": [
            {"day": "MON", "start_hour": 0, "end_hour": 3},
            {"day": "MON", "start_hour": 18, "end_hour": 22},   # multi-slot day
            {"day": "TUE", "start_hour": 22, "end_hour": 25},   # cross-midnight
            ...
          ],
          ...
        },
        "incubation_start_date": "2026-04-30",
        ...
      },
      ...
    }

A weekday may appear multiple times in slots[] — each entry yields one
SlotInstance per matching calendar date.

Cross-midnight encoding: if end_hour <= start_hour, treat end_hour as +24
(e.g., {"start_hour": 22, "end_hour": 1} means 22:00 -> 01:00 next day,
stored on slot_date = the day the slot starts).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

DAY_TO_WEEKDAY = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}
WEEKDAY_TO_DAY = {v: k for k, v in DAY_TO_WEEKDAY.items()}


@dataclass(frozen=True)
class SlotDef:
    day: str           # "MON".."SUN"
    start_hour: int
    end_hour: int      # may be < start_hour to denote cross-midnight


@dataclass
class SlotInstance:
    rad_fk: str
    slot_date: date
    day: str
    start_hour: int
    end_hour: int                      # effective end (may exceed 24 for cross-midnight)
    slot_start_ist: datetime
    slot_end_ist: datetime
    committed_days: list[str] = field(default_factory=list)


def fmt_hour_label(h: int) -> str:
    """0 -> '12AM', 3 -> '3AM', 12 -> '12PM', 22 -> '10PM', 25 -> '1AM'."""
    h = h % 24
    if h == 0:
        return "12AM"
    if h < 12:
        return f"{h}AM"
    if h == 12:
        return "12PM"
    return f"{h - 12}PM"


def slot_name_from(start_hour: int, end_hour: int) -> str:
    return f"{fmt_hour_label(start_hour)} - {fmt_hour_label(end_hour)}"


def parse_slots(raw_json: str) -> list[SlotDef]:
    """Pull the slots[] array out of borderless.commitment.slots."""
    obj = json.loads(raw_json)
    slots = (
        obj.get("borderless", {})
           .get("commitment", {})
           .get("slots", [])
    )
    out: list[SlotDef] = []
    for s in slots:
        out.append(
            SlotDef(
                day=str(s["day"]).upper(),
                start_hour=int(s["start_hour"]),
                end_hour=int(s["end_hour"]),
            )
        )
    return out


def expand_slot_instances(
    rad_id: str,
    slots: list[SlotDef],
    start: date,
    end: date,
) -> list[SlotInstance]:
    """Build a SlotInstance for every (committed slot, calendar date in [start, end]).

    Multi-slot days yield one instance per slot. Cross-midnight slots have
    effective_end = end_hour + 24 when end_hour <= start_hour.
    """
    by_weekday: dict[int, list[SlotDef]] = {}
    for s in slots:
        by_weekday.setdefault(DAY_TO_WEEKDAY[s.day], []).append(s)

    seen: set[str] = set()
    committed_days: list[str] = []
    for s in slots:
        if s.day not in seen:
            seen.add(s.day)
            committed_days.append(s.day)

    out: list[SlotInstance] = []
    cur = start
    while cur <= end:
        for s in by_weekday.get(cur.weekday(), []):
            effective_end = s.end_hour if s.end_hour > s.start_hour else s.end_hour + 24
            slot_start_ist = (
                datetime.combine(cur, time(0, 0), tzinfo=IST)
                + timedelta(hours=s.start_hour)
            )
            slot_end_ist = (
                datetime.combine(cur, time(0, 0), tzinfo=IST)
                + timedelta(hours=effective_end)
            )
            out.append(
                SlotInstance(
                    rad_fk=rad_id,
                    slot_date=cur,
                    day=s.day,
                    start_hour=s.start_hour,
                    end_hour=effective_end,
                    slot_start_ist=slot_start_ist,
                    slot_end_ist=slot_end_ist,
                    committed_days=committed_days,
                )
            )
        cur += timedelta(days=1)
    return out


# ---------- T1 entrypoint ----------


async def fetch_other_details_from_clickhouse(rad_id: str) -> str | None:
    """One-off CH read: SELECT other_details FROM transform.RadDetails WHERE rad_fk = <rad_id>.

    Returns the raw JSON text (byte-identical to CH) or None if no row.
    Raises ClickHouseDisabled if CH is not configured.
    """
    from app.services.clickhouse import ClickHouseClient

    client = ClickHouseClient()
    rows = await client.query(
        f"SELECT other_details FROM transform.RadDetails WHERE rad_fk = {int(rad_id)}"
    )
    if not rows:
        return None
    val = rows[0].get("other_details")
    if val is None:
        return None
    if isinstance(val, str):
        return val
    return json.dumps(val, separators=(",", ":"), ensure_ascii=False)


async def populate_other_details(rad_id: str) -> str:
    """T1 hook. Fire-and-forget background task for the onboarding webhook.

    Fetches other_details from ClickHouse and writes it to
    rad_state.other_details. Opens its own DB session so the caller's
    transaction is not entangled. Idempotent: skips if value already set.

    Returns one of: 'ok', 'skip:already_set', 'skip:no_ch_data',
    'skip:ch_disabled', 'skip:rad_missing', 'err:<reason>'.

    NEVER raises — this runs detached from the webhook response path and
    must not surface failures to n8n. Logs everything for ops visibility.
    """
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import RadState
    from app.services.clickhouse import ClickHouseDisabled

    try:
        async with SessionLocal() as session:
            rad = await session.get(RadState, rad_id)
            if rad is None:
                logger.warning(
                    "populate_other_details: rad %s not in rad_state; skipping",
                    rad_id,
                )
                return "skip:rad_missing"
            if rad.other_details is not None:
                logger.info(
                    "populate_other_details: rad %s already has other_details; skip",
                    rad_id,
                )
                return "skip:already_set"

            try:
                raw = await fetch_other_details_from_clickhouse(rad_id)
            except ClickHouseDisabled:
                logger.info(
                    "populate_other_details: CH disabled; rad %s skipped", rad_id
                )
                return "skip:ch_disabled"
            if raw is None:
                logger.warning(
                    "populate_other_details: CH has no row for rad %s", rad_id
                )
                return "skip:no_ch_data"

            rad.other_details = raw
            # The Postgres trigger stamps other_details_updated_at.
            # Re-fetch the row to confirm in caller logs if needed.
            await session.commit()
            logger.info(
                "populate_other_details: rad %s populated (%d chars)",
                rad_id,
                len(raw),
            )
            return "ok"
    except Exception as exc:  # noqa: BLE001 — must never raise
        logger.exception(
            "populate_other_details: unexpected failure for rad %s: %s",
            rad_id,
            exc,
        )
        return f"err:{type(exc).__name__}"
