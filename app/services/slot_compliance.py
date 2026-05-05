"""Session pairing + slot overlap math + classification + UPSERT.

Pure pieces (pair_sessions, overlap_minutes, classify) are unit-tested.
The IO pieces (fetch_presence, upsert_slot_status) hit ClickHouse and
Postgres respectively.

The cron orchestrator lives in app/jobs/slot_compliance_job.py — this
module is only the building blocks.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.clickhouse import ClickHouseClient
from app.services.rad_commitment import IST, SlotInstance, slot_name_from

logger = logging.getLogger(__name__)


# ---------- ClickHouse: presence pull ----------


async def fetch_presence_for_window(
    client: ClickHouseClient,
    rad_ids: list[str],
    range_start_ist: datetime,
    range_end_ist: datetime,
) -> tuple[list[dict], dict[str, str | None]]:
    """Pull ONLINE/OFFLINE events for the given rads within the IST window.
    Also pull a per-rad boundary anchor: the last status BEFORE range_start.
    Returns (events, anchor_by_rad_fk).

    Bug 1 mitigation: WHERE clause uses explicit Asia/Kolkata literals to
    match the column type DateTime64(3, 'Asia/Kolkata') — never UTC strings.
    Bug 2 mitigation: callers must tag returned date_time strings as IST
    when parsing (see pair_sessions).
    """
    if not rad_ids:
        return [], {}
    ids_csv = ",".join(str(int(r)) for r in rad_ids)
    rs = range_start_ist.strftime("%Y-%m-%d %H:%M:%S")
    re_ = range_end_ist.strftime("%Y-%m-%d %H:%M:%S")

    events_sql = (
        "SELECT user_fk, status, date_time "
        "FROM transform.AncillaryPresences "
        f"WHERE user_fk IN ({ids_csv}) "
        "AND status IN ('ONLINE','OFFLINE') "
        f"AND date_time >= toDateTime('{rs}','Asia/Kolkata') "
        f"AND date_time <  toDateTime('{re_}','Asia/Kolkata') "
        "ORDER BY user_fk, date_time ASC"
    )
    events = await client.query(events_sql)

    anchor_sql = (
        "SELECT user_fk, argMax(status, date_time) AS s "
        "FROM transform.AncillaryPresences "
        f"WHERE user_fk IN ({ids_csv}) "
        "AND status IN ('ONLINE','OFFLINE') "
        f"AND date_time < toDateTime('{rs}','Asia/Kolkata') "
        "GROUP BY user_fk"
    )
    anchor_rows = await client.query(anchor_sql)
    anchor_by_rad: dict[str, str | None] = {str(r): None for r in rad_ids}
    for row in anchor_rows:
        anchor_by_rad[str(row["user_fk"])] = row.get("s") or None
    return events, anchor_by_rad


# ---------- Pure: pair sessions ----------


def pair_sessions(
    events: list[dict],
    status_at_start: str | None,
    range_start_ist: datetime,
    range_end_ist: datetime,
) -> list[tuple[datetime, datetime]]:
    """Walk events in time order, emit (online_ist, offline_ist) pairs.

    If status_at_start == 'ONLINE', synthesize an ONLINE at range_start_ist
    so a session that began before the range is captured from the edge.
    If a final ONLINE has no closing OFFLINE within range, close it at
    range_end_ist.
    """
    parsed: list[tuple[datetime, str]] = []
    for e in events:
        dt_str = e["date_time"]
        if "." in dt_str:
            dt_str = dt_str.split(".", 1)[0]
        # ClickHouse returns IST already (column is DateTime64(3, 'Asia/Kolkata')).
        dt_ist = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
        parsed.append((dt_ist, str(e["status"])))

    if status_at_start == "ONLINE":
        parsed.insert(0, (range_start_ist, "ONLINE"))

    sessions: list[tuple[datetime, datetime]] = []
    online_at: datetime | None = None
    for ts, status in parsed:
        if status == "ONLINE":
            if online_at is None:
                online_at = ts
        elif status == "OFFLINE":
            if online_at is not None and ts > online_at:
                sessions.append((online_at, ts))
                online_at = None
    if online_at is not None:
        sessions.append((online_at, range_end_ist))
    return sessions


# ---------- Pure: overlap + classification ----------


def overlap_minutes(
    sessions: list[tuple[datetime, datetime]],
    slot_start: datetime,
    slot_end: datetime,
) -> int:
    """Sum overlap minutes between sessions and one slot window."""
    total = 0.0
    for s_on, s_off in sessions:
        ov_start = max(s_on, slot_start)
        ov_end = min(s_off, slot_end)
        if ov_end > ov_start:
            total += (ov_end - ov_start).total_seconds() / 60.0
    return int(round(total))


def classify(total_min: int, committed_hours: int) -> tuple[str, str]:
    """Returns (compliance_status, rad_availability)."""
    full_min = committed_hours * 60
    if total_min == 0:
        return "absent", "nil"
    if total_min == full_min:
        return "present", "full"
    return "present", "partial"


# ---------- IO: UPSERT into rad_slot_status ----------


async def upsert_slot_status(
    session: AsyncSession,
    *,
    rad_fk: str,
    slot_date_iso: str,
    slot_name: str,
    start_hour: int,
    end_hour: int,
    committed_hours: int,
    committed_days: list[str],
    total_active_minutes: int,
    compliance_status: str,
    rad_availability: str,
) -> None:
    """Idempotent insert/update keyed by (rad_fk, slot_date, start_hour)."""
    await session.execute(
        text(
            """
            INSERT INTO rad_incubation.rad_slot_status (
                rad_fk, slot_date, slot_name, start_hour, end_hour,
                committed_hours, committed_days, total_active_minutes,
                compliance_status, rad_availability
            ) VALUES (
                :rad_fk, :slot_date, :slot_name, :start_hour, :end_hour,
                :committed_hours, :committed_days, :total_active_minutes,
                :compliance_status, :rad_availability
            )
            ON CONFLICT (rad_fk, slot_date, start_hour) DO UPDATE SET
                slot_name            = EXCLUDED.slot_name,
                end_hour             = EXCLUDED.end_hour,
                committed_hours      = EXCLUDED.committed_hours,
                committed_days       = EXCLUDED.committed_days,
                total_active_minutes = EXCLUDED.total_active_minutes,
                compliance_status    = EXCLUDED.compliance_status,
                rad_availability     = EXCLUDED.rad_availability,
                last_updated_at      = now()
            """
        ),
        {
            "rad_fk": rad_fk,
            "slot_date": slot_date_iso,
            "slot_name": slot_name,
            "start_hour": str(start_hour),
            "end_hour": str(end_hour),
            "committed_hours": str(committed_hours),
            "committed_days": ",".join(committed_days),
            "total_active_minutes": str(total_active_minutes),
            "compliance_status": compliance_status,
            "rad_availability": rad_availability,
        },
    )


# ---------- Pure: classify a single SlotInstance against sessions ----------


def score_slot(
    si: SlotInstance,
    sessions: list[tuple[datetime, datetime]],
) -> dict:
    """Return a dict ready for upsert_slot_status (no IO)."""
    mins = overlap_minutes(sessions, si.slot_start_ist, si.slot_end_ist)
    committed_hours = si.end_hour - si.start_hour
    comp, avail = classify(mins, committed_hours)
    return {
        "rad_fk": si.rad_fk,
        "slot_date_iso": si.slot_date.isoformat(),
        "slot_name": slot_name_from(si.start_hour, si.end_hour),
        "start_hour": si.start_hour,
        "end_hour": si.end_hour,
        "committed_hours": committed_hours,
        "committed_days": si.committed_days,
        "total_active_minutes": mins,
        "compliance_status": comp,
        "rad_availability": avail,
    }


# ---------- Read helper for the 7-day Slack summary ----------


async def fetch_slot_compliance_rows(
    session: AsyncSession,
    rad_id: str,
    start_date: date,
    end_date: date,
) -> list[dict]:
    """Pull rad_slot_status rows for one rad over [start_date, end_date], IST.

    Returns list of dicts in (slot_date, start_hour) order — same shape the
    Slack formatter expects. Empty list when no scoring exists yet.
    """
    rows = (
        await session.execute(
            text(
                "SELECT slot_date, start_hour, slot_name, end_hour, "
                "       committed_hours, total_active_minutes, "
                "       compliance_status, rad_availability "
                "FROM rad_incubation.rad_slot_status "
                "WHERE rad_fk = :rad "
                "AND slot_date >= :start "
                "AND slot_date <= :end "
                "ORDER BY slot_date ASC, start_hour::int ASC"
            ),
            {
                "rad": rad_id,
                "start": start_date.isoformat(),
                "end": end_date.isoformat(),
            },
        )
    ).mappings().all()
    return [dict(r) for r in rows]
