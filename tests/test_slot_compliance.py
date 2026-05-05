"""Pure-function tests for session pairing + overlap + classification."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.slot_compliance import (
    classify,
    overlap_minutes,
    pair_sessions,
)

IST = timezone(timedelta(hours=5, minutes=30))


def _evt(hh: int, mm: int, status: str, day: int = 5) -> dict:
    # Helper: build a CH-shaped event dict (date_time as IST string).
    return {
        "user_fk": 9999,
        "status": status,
        "date_time": f"2026-05-0{day} {hh:02d}:{mm:02d}:00",
    }


def test_pair_sessions_basic_pair():
    range_start = datetime(2026, 5, 5, 0, 0, tzinfo=IST)
    range_end = datetime(2026, 5, 5, 3, 0, tzinfo=IST)
    events = [_evt(0, 30, "ONLINE"), _evt(2, 0, "OFFLINE")]
    sessions = pair_sessions(events, status_at_start=None,
                             range_start_ist=range_start, range_end_ist=range_end)
    assert len(sessions) == 1
    on, off = sessions[0]
    assert on == datetime(2026, 5, 5, 0, 30, tzinfo=IST)
    assert off == datetime(2026, 5, 5, 2, 0, tzinfo=IST)


def test_pair_sessions_synthetic_online_at_range_start_when_anchor_online():
    range_start = datetime(2026, 5, 5, 0, 0, tzinfo=IST)
    range_end = datetime(2026, 5, 5, 3, 0, tzinfo=IST)
    events = [_evt(1, 0, "OFFLINE")]
    sessions = pair_sessions(events, status_at_start="ONLINE",
                             range_start_ist=range_start, range_end_ist=range_end)
    assert sessions == [(range_start, datetime(2026, 5, 5, 1, 0, tzinfo=IST))]


def test_pair_sessions_open_session_closed_at_range_end():
    range_start = datetime(2026, 5, 5, 0, 0, tzinfo=IST)
    range_end = datetime(2026, 5, 5, 3, 0, tzinfo=IST)
    events = [_evt(0, 30, "ONLINE")]  # never closed in-range
    sessions = pair_sessions(events, status_at_start=None,
                             range_start_ist=range_start, range_end_ist=range_end)
    assert sessions == [(datetime(2026, 5, 5, 0, 30, tzinfo=IST), range_end)]


def test_pair_sessions_ignores_duplicate_online():
    range_start = datetime(2026, 5, 5, 0, 0, tzinfo=IST)
    range_end = datetime(2026, 5, 5, 3, 0, tzinfo=IST)
    events = [
        _evt(0, 30, "ONLINE"),
        _evt(0, 30, "ONLINE"),  # dup
        _evt(1, 0, "OFFLINE"),
    ]
    sessions = pair_sessions(events, status_at_start=None,
                             range_start_ist=range_start, range_end_ist=range_end)
    assert len(sessions) == 1


def test_pair_sessions_returns_ist_datetimes_no_double_shift():
    """Bug 2 regression: returned event timestamps must be tagged as IST,
    not parsed as UTC then re-shifted."""
    range_start = datetime(2026, 5, 5, 0, 0, tzinfo=IST)
    range_end = datetime(2026, 5, 5, 23, 59, tzinfo=IST)
    events = [_evt(18, 58, "ONLINE"), _evt(19, 17, "OFFLINE")]
    sessions = pair_sessions(events, None, range_start, range_end)
    assert len(sessions) == 1
    on, off = sessions[0]
    # If Bug 2 returned, on would be 18:58 + 5:30 = 00:28 next day.
    assert on.hour == 18
    assert on.minute == 58


def test_overlap_inside_outside_partial():
    slot_start = datetime(2026, 5, 5, 0, 0, tzinfo=IST)
    slot_end = datetime(2026, 5, 5, 3, 0, tzinfo=IST)
    sessions = [
        # fully inside
        (datetime(2026, 5, 5, 0, 30, tzinfo=IST),
         datetime(2026, 5, 5, 1, 0, tzinfo=IST)),  # 30 min
        # fully outside (after slot)
        (datetime(2026, 5, 5, 5, 0, tzinfo=IST),
         datetime(2026, 5, 5, 6, 0, tzinfo=IST)),  # 0 min counted
        # partial: starts before slot ends, runs past
        (datetime(2026, 5, 5, 2, 30, tzinfo=IST),
         datetime(2026, 5, 5, 4, 0, tzinfo=IST)),  # 30 min counted (2:30-3:00)
    ]
    assert overlap_minutes(sessions, slot_start, slot_end) == 60


def test_classify_thresholds():
    # Exact match -> full
    assert classify(180, 3) == ("present", "full")
    # Zero -> nil
    assert classify(0, 3) == ("absent", "nil")
    # Anything in between -> partial
    assert classify(1, 3) == ("present", "partial")
    assert classify(179, 3) == ("present", "partial")
