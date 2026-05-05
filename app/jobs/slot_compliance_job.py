"""Cron job: score every committed slot that just ended into rad_slot_status.

Schedule: every 6h IST by default (00/06/12/18). Each tick processes slots
whose `slot_end` falls in `(now_ist - lookback_hours, now_ist]`.

Per tick:
  1. Load all rads with non-null other_details.
  2. Parse each rad's commitment, expand slot instances in the window.
  3. Filter out (rad_fk, slot_date, start_hour) already in rad_slot_status
     so re-runs don't re-query CH for already-scored slots.
  4. Group remaining instances by identical (slot_start_ist, slot_end_ist).
     Per group: ONE CH query for events + ONE for boundary anchors,
     covering all rads sharing the window.
  5. Pair sessions per rad, compute overlap, classify, UPSERT.

The job is idempotent and crash-safe: re-running over the same window
re-computes the same value, and the ON CONFLICT clause keeps the row
stable.

Read-only against ClickHouse; writes only to rad_slot_status.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, text

from app.config import get_settings
from app.db import SessionLocal
from app.models import RadState
from app.services.clickhouse import ClickHouseClient, ClickHouseDisabled
from app.services.rad_commitment import (
    SlotInstance,
    expand_slot_instances,
    parse_slots,
)
from app.services.slot_compliance import (
    fetch_presence_for_window,
    pair_sessions,
    score_slot,
    upsert_slot_status,
)

logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")


async def run_slot_compliance_tick(lookback_hours: int | None = None) -> dict:
    """One cron tick. Returns a small summary dict for the caller's log.

    `lookback_hours` overrides the configured default — used by the one-shot
    startup deep backfill. Default None means use the cron's normal cadence.
    """
    settings = get_settings()
    now_ist = datetime.now(_IST)
    effective_lookback = (
        lookback_hours if lookback_hours is not None
        else settings.slot_compliance_lookback_hours
    )
    lookback = timedelta(hours=effective_lookback)
    window_lo = now_ist - lookback
    window_hi = now_ist

    t0 = time.monotonic()

    # 1. Load all rads with commitment populated.
    async with SessionLocal() as session:
        rads = (
            await session.execute(
                select(RadState).where(RadState.other_details.is_not(None))
            )
        ).scalars().all()
    logger.info(
        "slot_compliance: tick start ist=%s window=(%s, %s] rads=%d",
        now_ist.isoformat(timespec="seconds"),
        window_lo.isoformat(timespec="seconds"),
        window_hi.isoformat(timespec="seconds"),
        len(rads),
    )

    if not rads:
        return {"rads": 0, "instances": 0, "windows": 0, "rows_upserted": 0}

    # 2. Parse + expand each rad's slots in the window range. We expand a
    # padded date range that bounds the window in calendar dates IST.
    range_start_date = (window_lo - timedelta(days=1)).date()
    range_end_date = window_hi.date()

    instances: list[SlotInstance] = []
    for rad in rads:
        try:
            slot_defs = parse_slots(rad.other_details or "{}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "slot_compliance: bad commitment JSON for rad %s: %s",
                rad.rad_id, exc,
            )
            continue
        if not slot_defs:
            continue
        rad_instances = expand_slot_instances(
            rad.rad_id, slot_defs, range_start_date, range_end_date
        )
        # Keep only instances whose slot_end is in (window_lo, window_hi].
        instances.extend(
            si for si in rad_instances
            if window_lo < si.slot_end_ist <= window_hi
        )

    if not instances:
        logger.info("slot_compliance: nothing to score in this window")
        return {"rads": len(rads), "instances": 0, "windows": 0, "rows_upserted": 0}

    # 3. Filter out (rad_fk, slot_date, start_hour) already present.
    keys = [
        (si.rad_fk, si.slot_date.isoformat(), str(si.start_hour))
        for si in instances
    ]
    async with SessionLocal() as session:
        # Coarse filter: pull any rows for the rads + dates in our batch,
        # then narrow in Python by (rad_fk, slot_date, start_hour).
        existing = await session.execute(
            text(
                "SELECT rad_fk, slot_date, start_hour "
                "FROM rad_incubation.rad_slot_status "
                "WHERE rad_fk = ANY(:rad_fks) "
                "AND slot_date = ANY(:slot_dates)"
            ),
            {
                "rad_fks": list({k[0] for k in keys}),
                "slot_dates": list({k[1] for k in keys}),
            },
        )
        existing_keys = {(r[0], r[1], r[2]) for r in existing.all()}

    fresh = [si for si in instances if
             (si.rad_fk, si.slot_date.isoformat(), str(si.start_hour))
             not in existing_keys]
    if not fresh:
        logger.info(
            "slot_compliance: all %d instances already scored; nothing to do",
            len(instances),
        )
        return {
            "rads": len(rads), "instances": len(instances),
            "windows": 0, "rows_upserted": 0,
        }

    # 4. Group by (slot_start_ist, slot_end_ist) — rads sharing a window
    # get one CH query.
    try:
        ch = ClickHouseClient()
    except ClickHouseDisabled:
        logger.info("slot_compliance: CH disabled; tick aborted")
        return {
            "rads": len(rads), "instances": len(instances),
            "windows": 0, "rows_upserted": 0, "skipped": "ch_disabled",
        }

    groups: dict[tuple[datetime, datetime], list[SlotInstance]] = {}
    for si in fresh:
        groups.setdefault((si.slot_start_ist, si.slot_end_ist), []).append(si)

    rows_upserted = 0
    ch_query_count = 0

    async with SessionLocal() as session:
        async with session.begin():
            for (g_start, g_end), g_instances in groups.items():
                rad_ids = sorted({si.rad_fk for si in g_instances})
                events, anchor_by_rad = await fetch_presence_for_window(
                    ch, rad_ids, g_start, g_end
                )
                ch_query_count += 2  # events + anchor

                # Bucket events by user_fk for per-rad pairing.
                by_rad: dict[str, list[dict]] = {r: [] for r in rad_ids}
                for ev in events:
                    rk = str(ev["user_fk"])
                    if rk in by_rad:
                        by_rad[rk].append(ev)

                # Pair once per rad for this shared window.
                sessions_by_rad: dict[str, list[tuple[datetime, datetime]]] = {}
                for rk, evs in by_rad.items():
                    sessions_by_rad[rk] = pair_sessions(
                        evs, anchor_by_rad.get(rk), g_start, g_end
                    )

                # Score every instance in this group + UPSERT.
                for si in g_instances:
                    payload = score_slot(si, sessions_by_rad.get(si.rad_fk, []))
                    await upsert_slot_status(session, **payload)
                    rows_upserted += 1

    duration_ms = int((time.monotonic() - t0) * 1000)
    summary = {
        "rads": len(rads),
        "instances": len(instances),
        "fresh": len(fresh),
        "windows": len(groups),
        "ch_queries": ch_query_count,
        "rows_upserted": rows_upserted,
        "duration_ms": duration_ms,
    }
    logger.info("slot_compliance: tick done %s", summary)
    return summary
