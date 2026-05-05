"""Throwaway test: extract rad details from ClickHouse + populate
rad_state.other_details and rad_slot_status for one rad. Read every step's
output to verify values + format before we wire this into a real cron.

Usage:
    python scripts/_test_rad_slot_status.py --rad-id 2105
    python scripts/_test_rad_slot_status.py --rad-id 2105 --dry-run
    python scripts/_test_rad_slot_status.py --rad-id 2105 \\
        --start-date 2026-04-30 --end-date 2026-05-04

Pre-reqs (run once in DBWeb):
    ALTER TABLE rad_incubation.rad_state
        ADD COLUMN other_details text NULL;
    ALTER TABLE rad_incubation.rad_state
        ADD COLUMN other_details_updated_at timestamptz NULL;
    CREATE TABLE rad_incubation.rad_slot_status (
        rad_fk text NOT NULL,
        slot_date text NOT NULL,
        slot_name text NOT NULL,
        start_hour text NOT NULL,
        end_hour text NOT NULL,
        committed_hours text NOT NULL,
        committed_days text NOT NULL,
        total_active_minutes text NOT NULL,
        compliance_status text NOT NULL,
        rad_availability text NOT NULL,
        last_updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT rad_slot_status_pkey PRIMARY KEY (rad_fk, slot_date)
    );

Pipeline:
  1. CH:  SELECT other_details FROM transform.RadDetails WHERE rad_fk = <rad-id>
  2. PG:  UPDATE rad_state SET other_details = $1,
                              other_details_updated_at = now()
          WHERE rad_id = <rad-id>
  3. Parse borderless.commitment.slots[] from the JSON.
  4. For every committed weekday in [start-date, end-date] whose slot has
     already ended, build (slot_date, start_hour, end_hour) tuples in IST.
  5. CH:  pull AncillaryPresences events covering the union range +
          one boundary anchor (last status BEFORE the earliest slot start).
  6. Pair ONLINE -> OFFLINE per rad in Python; compute overlap minutes per slot.
  7. UPSERT rad_slot_status rows. Print everything for inspection.

Read-only against ClickHouse. Writes to Postgres only.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

import asyncpg
import httpx
from dotenv import load_dotenv

load_dotenv()

IST = timezone(timedelta(hours=5, minutes=30))

# JSON day-of-week (uppercase 3-letter) -> python weekday() (0=Mon..6=Sun)
DAY_TO_WEEKDAY = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}
WEEKDAY_TO_DAY = {v: k for k, v in DAY_TO_WEEKDAY.items()}


# ---------- helpers ----------


def _to_asyncpg_dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


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


# ---------- ClickHouse over HTTP ----------


class ClickHouse:
    def __init__(self) -> None:
        host = os.environ["CLICKHOUSE_HOST"]
        port = os.environ.get("CLICKHOUSE_PORT", "8123")
        self.url = f"http://{host}:{port}/"
        self.user = os.environ["CLICKHOUSE_USER"]
        self.password = os.environ["CLICKHOUSE_PASSWORD"]

    async def query(self, sql: str) -> list[dict]:
        """Run a SELECT against ClickHouse, return parsed rows."""
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.post(
                self.url,
                params={"query": f"{sql} FORMAT JSONEachRow"},
                auth=(self.user, self.password),
            )
            if r.status_code != 200:
                raise RuntimeError(
                    f"ClickHouse query failed ({r.status_code}): {r.text[:500]}"
                )
            text = r.text.strip()
            if not text:
                return []
            return [json.loads(line) for line in text.splitlines() if line.strip()]


# ---------- step 1: fetch other_details from ClickHouse ----------


async def fetch_other_details(ch: ClickHouse, rad_id: str) -> str | None:
    """Return the raw JSON text of other_details, or None if no row."""
    rows = await ch.query(
        f"SELECT other_details FROM transform.RadDetails WHERE rad_fk = {rad_id}"
    )
    if not rows:
        return None
    val = rows[0].get("other_details")
    if val is None:
        return None
    # ClickHouse may return as a string (if column is String) or a parsed
    # object (if JSON). Always normalize to JSON text so PG stores byte-identical.
    if isinstance(val, str):
        return val
    return json.dumps(val, separators=(",", ":"), ensure_ascii=False)


# ---------- step 2: write to Postgres rad_state ----------


async def update_rad_state(pg: asyncpg.Connection, rad_id: str, raw_json: str) -> None:
    has_updated_col = await pg.fetchval(
        """
        SELECT 1 FROM information_schema.columns
         WHERE table_schema='rad_incubation'
           AND table_name='rad_state'
           AND column_name='other_details_updated_at'
        """
    )
    if has_updated_col:
        res = await pg.execute(
            """
            UPDATE rad_incubation.rad_state
               SET other_details = $1,
                   other_details_updated_at = now()
             WHERE rad_id = $2
            """,
            raw_json,
            rad_id,
        )
    else:
        print("[pg] note: other_details_updated_at column missing; updating only other_details")
        res = await pg.execute(
            """
            UPDATE rad_incubation.rad_state
               SET other_details = $1
             WHERE rad_id = $2
            """,
            raw_json,
            rad_id,
        )
    print(f"[pg] UPDATE rad_state ({rad_id}): {res}")


# ---------- step 3: parse commitment slots ----------


@dataclass
class SlotDef:
    day: str           # "MON".."SUN"
    start_hour: int
    end_hour: int      # may be < start_hour to denote cross-midnight in some encodings


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


def parse_slots(raw_json: str) -> list[SlotDef]:
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
    """Build a SlotInstance for every (committed weekday in [start, end]).
    A weekday may have multiple slots (e.g., MON 3-6 and MON 18-22) — emit
    one SlotInstance per slot."""
    by_weekday: dict[int, list[SlotDef]] = {}
    for s in slots:
        by_weekday.setdefault(DAY_TO_WEEKDAY[s.day], []).append(s)
    # Unique day list preserving first-seen order from the JSON.
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
            # Cross-midnight defense: if end_hour <= start_hour, treat as +24
            effective_end = s.end_hour if s.end_hour > s.start_hour else s.end_hour + 24
            slot_start_ist = datetime.combine(cur, time(0, 0), tzinfo=IST) \
                + timedelta(hours=s.start_hour)
            slot_end_ist = datetime.combine(cur, time(0, 0), tzinfo=IST) \
                + timedelta(hours=effective_end)
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


# ---------- step 4: pull presence events from ClickHouse ----------


async def fetch_presence(
    ch: ClickHouse,
    rad_id: str,
    range_start_utc: datetime,
    range_end_utc: datetime,
) -> tuple[list[dict], str | None]:
    """Returns (events_in_range, status_at_range_start)."""
    sql = (
        "SELECT user_fk, status, date_time "
        "FROM transform.AncillaryPresences "
        f"WHERE user_fk = {rad_id} "
        "AND status IN ('ONLINE','OFFLINE') "
        f"AND date_time >= toDateTime('{range_start_utc:%Y-%m-%d %H:%M:%S}') "
        f"AND date_time <  toDateTime('{range_end_utc:%Y-%m-%d %H:%M:%S}') "
        "ORDER BY date_time ASC"
    )
    events = await ch.query(sql)
    print(f"[ch] fetched {len(events)} presence events in range")

    anchor_sql = (
        "SELECT argMax(status, date_time) AS status_at_start "
        "FROM transform.AncillaryPresences "
        f"WHERE user_fk = {rad_id} "
        "AND status IN ('ONLINE','OFFLINE') "
        f"AND date_time < toDateTime('{range_start_utc:%Y-%m-%d %H:%M:%S}')"
    )
    anchor = await ch.query(anchor_sql)
    status_at_start = (anchor[0].get("status_at_start") if anchor else None) or None
    print(f"[ch] anchor (status before range start): {status_at_start!r}")
    return events, status_at_start


# ---------- step 5: pair ONLINE -> OFFLINE sessions ----------


def pair_sessions(
    events: list[dict],
    status_at_start: str | None,
    range_start_ist: datetime,
    range_end_ist: datetime,
) -> list[tuple[datetime, datetime]]:
    """Walk events in order, emit (online_ist, offline_ist) pairs.
    If status_at_start == 'ONLINE', synthesize an ONLINE at range_start_ist.
    If a final ONLINE has no closing OFFLINE within range, close it at range_end_ist.
    """
    parsed = []
    for e in events:
        # transform.AncillaryPresences.date_time is DateTime64(3, 'Asia/Kolkata').
        # ClickHouse returns it already in IST. Tag as IST directly; do NOT
        # treat as UTC and re-shift (would double-add +5:30).
        dt_str = e["date_time"]
        if "." in dt_str:
            dt_str = dt_str.split(".", 1)[0]
        dt_ist = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
        parsed.append((dt_ist, str(e["status"])))

    # Optionally prepend a synthetic ONLINE event so a session that started
    # before the range is captured from range_start onward.
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
    # Open session at the right edge: close at range_end_ist
    if online_at is not None:
        sessions.append((online_at, range_end_ist))
    return sessions


# ---------- step 6: overlap minutes per slot ----------


def overlap_minutes(
    sessions: list[tuple[datetime, datetime]],
    slot_start: datetime,
    slot_end: datetime,
) -> int:
    total = 0.0
    for s_on, s_off in sessions:
        ov_start = max(s_on, slot_start)
        ov_end = min(s_off, slot_end)
        if ov_end > ov_start:
            total += (ov_end - ov_start).total_seconds() / 60.0
    return int(round(total))


def classify(total_min: int, committed_hours: int) -> tuple[str, str]:
    full_min = committed_hours * 60
    if total_min == 0:
        return "absent", "nil"
    if total_min == full_min:
        return "present", "full"
    return "present", "partial"


# ---------- step 7: UPSERT rad_slot_status ----------


async def upsert_slot_status(
    pg: asyncpg.Connection,
    rad_fk: str,
    slot_date: date,
    slot_name: str,
    start_hour: int,
    end_hour: int,
    committed_hours: int,
    committed_days: list[str],
    total_active_minutes: int,
    compliance_status: str,
    rad_availability: str,
) -> None:
    await pg.execute(
        """
        INSERT INTO rad_incubation.rad_slot_status (
            rad_fk, slot_date, slot_name, start_hour, end_hour,
            committed_hours, committed_days, total_active_minutes,
            compliance_status, rad_availability
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
        ON CONFLICT (rad_fk, slot_date, start_hour) DO UPDATE
        SET slot_name            = EXCLUDED.slot_name,
            end_hour             = EXCLUDED.end_hour,
            committed_hours      = EXCLUDED.committed_hours,
            committed_days       = EXCLUDED.committed_days,
            total_active_minutes = EXCLUDED.total_active_minutes,
            compliance_status    = EXCLUDED.compliance_status,
            rad_availability     = EXCLUDED.rad_availability,
            last_updated_at      = now();
        """,
        rad_fk,
        slot_date.isoformat(),
        slot_name,
        str(start_hour),
        str(end_hour),
        str(committed_hours),
        ",".join(committed_days),
        str(total_active_minutes),
        compliance_status,
        rad_availability,
    )


# ---------- main ----------


async def amain(
    rad_id: str,
    start_date: date | None,
    end_date: date | None,
    dry_run: bool,
    commitment_file: str | None,
) -> None:
    ch = ClickHouse()
    pg = await asyncpg.connect(_to_asyncpg_dsn(os.environ["DATABASE_URL"]))
    try:
        if commitment_file:
            print(f"\n=== STEP 1: load commitment JSON from {commitment_file} ===")
            with open(commitment_file, "r", encoding="utf-8") as f:
                obj = json.load(f)
            raw_json = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
        else:
            print(f"\n=== STEP 1: fetch other_details for rad_fk={rad_id} from ClickHouse ===")
            raw_json = await fetch_other_details(ch, rad_id)
        if raw_json is None:
            print("[source] no other_details — aborting")
            return
        print(f"[ch] raw other_details JSON ({len(raw_json)} chars):")
        try:
            print(json.dumps(json.loads(raw_json), indent=2))
        except json.JSONDecodeError:
            print(raw_json)

        print(f"\n=== STEP 2: UPDATE rad_state.other_details for rad_id={rad_id} ===")
        if dry_run:
            print("[pg] dry-run, skipping UPDATE")
        else:
            await update_rad_state(pg, rad_id, raw_json)
            row = await pg.fetchrow(
                "SELECT rad_id, other_details_updated_at, "
                "left(other_details, 80) AS preview "
                "FROM rad_incubation.rad_state WHERE rad_id = $1",
                rad_id,
            )
            print(f"[pg] post-update row: {dict(row) if row else None}")

        print("\n=== STEP 3: parse commitment slots ===")
        slot_defs = parse_slots(raw_json)
        if not slot_defs:
            print("[parse] no slots in commitment — nothing to score")
            return
        for s in slot_defs:
            print(f"  {s.day}: {s.start_hour:02d}:00 - {s.end_hour:02d}:00 IST  "
                  f"({slot_name_from(s.start_hour, s.end_hour)})")

        committed_days_list = [s.day for s in slot_defs]

        print("\n=== STEP 4: build slot instances in date range ===")
        if end_date is None:
            end_date = datetime.now(IST).date()
        if start_date is None:
            # Default: incubation_start_date if present, else 7 days ago
            try:
                start_date = date.fromisoformat(
                    json.loads(raw_json)["borderless"]["incubation_start_date"]
                )
            except (KeyError, TypeError, ValueError):
                start_date = end_date - timedelta(days=7)
        print(f"[range] {start_date} -> {end_date}")
        instances = expand_slot_instances(rad_id, slot_defs, start_date, end_date)
        # Only score slots whose end is strictly in the past
        now_ist = datetime.now(IST)
        eligible = [si for si in instances if si.slot_end_ist <= now_ist]
        skipped = len(instances) - len(eligible)
        print(f"[range] {len(instances)} committed slot-instances in range; "
              f"{len(eligible)} ended (eligible), {skipped} still upcoming")
        for si in instances:
            ended = "DONE " if si.slot_end_ist <= now_ist else "FUTURE"
            print(f"  [{ended}] {si.slot_date} {si.day}  "
                  f"{si.slot_start_ist:%Y-%m-%d %H:%M %z} -> "
                  f"{si.slot_end_ist:%Y-%m-%d %H:%M %z}")

        if not eligible:
            print("[range] no eligible slots ended yet — nothing to score")
            return

        print("\n=== STEP 5: query AncillaryPresences (single union range) ===")
        range_start_ist = min(si.slot_start_ist for si in eligible)
        range_end_ist = max(si.slot_end_ist for si in eligible)
        range_start_utc = range_start_ist.astimezone(timezone.utc)
        range_end_utc = range_end_ist.astimezone(timezone.utc)
        print(f"[ch] union IST: {range_start_ist} -> {range_end_ist}")
        print(f"[ch] union UTC: {range_start_utc} -> {range_end_utc}")
        events, status_at_start = await fetch_presence(
            ch, rad_id, range_start_utc, range_end_utc
        )

        print("\n=== STEP 6: pair sessions + compute overlap ===")
        sessions = pair_sessions(
            events, status_at_start, range_start_ist, range_end_ist
        )
        print(f"[sessions] {len(sessions)} pairs:")
        for on_, off_ in sessions:
            mins = (off_ - on_).total_seconds() / 60.0
            print(f"  ONLINE {on_:%Y-%m-%d %H:%M:%S}  ->  "
                  f"OFFLINE {off_:%Y-%m-%d %H:%M:%S}  ({mins:.1f} min)")

        print("\n=== STEP 7: classify + UPSERT rad_slot_status ===")
        for si in eligible:
            mins = overlap_minutes(sessions, si.slot_start_ist, si.slot_end_ist)
            committed_hours = si.end_hour - si.start_hour
            comp, avail = classify(mins, committed_hours)
            slot_label = slot_name_from(si.start_hour, si.end_hour)
            print(
                f"  rad_fk={si.rad_fk}  slot_date={si.slot_date}  "
                f"slot={slot_label}  committed={committed_hours}h  "
                f"active={mins} min  -> compliance={comp}  availability={avail}"
            )
            if dry_run:
                continue
            await upsert_slot_status(
                pg=pg,
                rad_fk=si.rad_fk,
                slot_date=si.slot_date,
                slot_name=slot_label,
                start_hour=si.start_hour,
                end_hour=si.end_hour,
                committed_hours=committed_hours,
                committed_days=si.committed_days,
                total_active_minutes=mins,
                compliance_status=comp,
                rad_availability=avail,
            )

        if dry_run:
            print("\n[done] dry-run complete (no PG writes)")
        else:
            print("\n=== final: rad_slot_status rows for this rad ===")
            rows = await pg.fetch(
                "SELECT * FROM rad_incubation.rad_slot_status "
                "WHERE rad_fk = $1 "
                "ORDER BY slot_date, start_hour::int",
                rad_id,
            )
            for r in rows:
                print("  ", dict(r))
    finally:
        await pg.close()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--rad-id", required=True)
    p.add_argument("--start-date", type=date.fromisoformat, default=None,
                   help="YYYY-MM-DD; default = borderless.incubation_start_date")
    p.add_argument("--end-date", type=date.fromisoformat, default=None,
                   help="YYYY-MM-DD; default = today (IST)")
    p.add_argument("--dry-run", action="store_true",
                   help="Compute everything but skip Postgres writes")
    p.add_argument("--commitment-from-file", default=None,
                   help="Path to a JSON file containing other_details. "
                        "If set, skips the ClickHouse fetch in step 1.")
    args = p.parse_args()

    for var in ("DATABASE_URL", "CLICKHOUSE_HOST", "CLICKHOUSE_USER", "CLICKHOUSE_PASSWORD"):
        if not os.environ.get(var):
            print(f"missing env var: {var}", file=sys.stderr)
            sys.exit(2)

    asyncio.run(amain(
        args.rad_id, args.start_date, args.end_date,
        args.dry_run, args.commitment_from_file,
    ))


if __name__ == "__main__":
    main()
