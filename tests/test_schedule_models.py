from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from nomusyo.schedule.clock import FixedClock, SystemClock
from nomusyo.schedule.errors import ValidationError
from nomusyo.schedule.models import (
    Event,
    ReminderSpec,
    Todo,
    Window,
    from_local_str,
    from_utc_str,
    get_zone,
    local_day_bounds,
    to_local_str,
    to_utc_str,
)

JST = ZoneInfo("Asia/Tokyo")


def jst(*a):
    return datetime(*a, tzinfo=JST)


def test_fixed_clock():
    c = FixedClock(jst(2026, 10, 29, 12))
    assert c.now() == jst(2026, 10, 29, 12)
    c.advance(timedelta(hours=1))
    assert c.now().hour == 13
    c.set(jst(2027, 1, 1))
    assert c.now().year == 2027


def test_fixed_clock_rejects_naive():
    with pytest.raises(ValueError):
        FixedClock(datetime(2026, 1, 1))
    c = FixedClock(jst(2026, 1, 1))
    with pytest.raises(ValueError):
        c.set(datetime(2026, 1, 1))


def test_system_clock_is_aware():
    assert SystemClock().now().tzinfo is not None


def test_utc_roundtrip():
    s = to_utc_str(jst(2026, 11, 3, 15, 0))
    assert s == "2026-11-03T06:00:00Z"
    assert from_utc_str(s) == jst(2026, 11, 3, 15, 0)


def test_local_roundtrip():
    s = to_local_str(jst(2026, 11, 3, 15, 0))
    assert s == "2026-11-03T15:00:00"
    assert from_local_str(s, "Asia/Tokyo") == jst(2026, 11, 3, 15, 0)


def test_naive_rejected():
    with pytest.raises(ValidationError):
        to_utc_str(datetime(2026, 1, 1))
    with pytest.raises(ValidationError):
        to_local_str(datetime(2026, 1, 1))


def test_bad_zone():
    with pytest.raises(ValidationError):
        get_zone("Mars/Base")
    with pytest.raises(ValidationError):
        get_zone("")


def test_window_validation():
    Window(jst(2026, 1, 1), jst(2026, 1, 2))
    with pytest.raises(ValidationError):
        Window(jst(2026, 1, 2), jst(2026, 1, 2))
    with pytest.raises(ValidationError):
        Window(jst(2026, 1, 3), jst(2026, 1, 2))
    with pytest.raises(ValidationError):
        Window(datetime(2026, 1, 1), jst(2026, 1, 2))


def test_window_for_day_dst_safe():
    ny = ZoneInfo("America/New_York")
    w = Window.for_day(date(2026, 3, 8), ny)  # DST start: 23h day
    assert w.end.astimezone(UTC) - w.start.astimezone(UTC) == timedelta(hours=23)
    s, e = local_day_bounds(date(2026, 12, 31), JST)
    assert e == jst(2027, 1, 1)


def test_reminder_spec_validation():
    ReminderSpec(offset_min=0)
    ReminderSpec(at_local="20:00", day_offset=2)
    for kw in (
        {},
        {"offset_min": 5, "at_local": "20:00"},
        {"offset_min": -1},
        {"offset_min": 5, "day_offset": 1},
        {"at_local": "20:00", "day_offset": -1},
    ):
        with pytest.raises(ValidationError):
            ReminderSpec(**kw)


def test_event_end_variants():
    base = dict(id=1, owner_id=1, title="x", tz="Asia/Tokyo")
    assert Event(**base, start=jst(2026, 1, 1, 9)).end == jst(2026, 1, 1, 9)
    assert Event(**base, start=jst(2026, 1, 1, 9), duration_min=90).end == jst(2026, 1, 1, 10, 30)
    assert Event(**base, start=jst(2026, 1, 1), all_day=True).end == jst(2026, 1, 2)
    assert Event(**base, start=jst(2026, 12, 31), all_day=True).end == jst(2027, 1, 1)
    assert Event(**base, start=jst(2026, 1, 1), all_day=True, duration_min=2 * 1440).end == jst(
        2026, 1, 3
    )
    # partial days round up
    assert Event(**base, start=jst(2026, 1, 1), all_day=True, duration_min=1441).end == jst(
        2026, 1, 3
    )


def test_event_requires_aware_start():
    with pytest.raises(ValidationError):
        Event(id=1, owner_id=1, title="x", tz="Asia/Tokyo", start=datetime(2026, 1, 1))


def test_event_deleted_flag():
    e = Event(id=1, owner_id=1, title="x", tz="Asia/Tokyo", start=jst(2026, 1, 1))
    assert not e.deleted
    assert Event(
        id=1,
        owner_id=1,
        title="x",
        tz="Asia/Tokyo",
        start=jst(2026, 1, 1),
        deleted_at=jst(2026, 1, 2),
    ).deleted


def test_todo_overdue_timed_boundary():
    t = Todo(id=1, owner_id=1, title="x", tz="Asia/Tokyo", due=jst(2026, 10, 29, 12))
    assert not t.is_overdue(jst(2026, 10, 29, 11, 59, 59))
    assert t.is_overdue(jst(2026, 10, 29, 12))


def test_todo_overdue_date_only_boundary():
    t = Todo(id=1, owner_id=1, title="x", tz="Asia/Tokyo", due=jst(2026, 10, 29), due_all_day=True)
    assert t.overdue_boundary == jst(2026, 10, 30)
    assert not t.is_overdue(jst(2026, 10, 29, 23, 59, 59))
    assert t.is_overdue(jst(2026, 10, 30))


def test_todo_overdue_year_end():
    t = Todo(id=1, owner_id=1, title="x", tz="Asia/Tokyo", due=jst(2026, 12, 31), due_all_day=True)
    assert t.overdue_boundary == jst(2027, 1, 1)


def test_todo_done_or_no_due_never_overdue():
    now = jst(2030, 1, 1)
    assert not Todo(id=1, owner_id=1, title="x", tz="Asia/Tokyo").is_overdue(now)
    done = Todo(
        id=1, owner_id=1, title="x", tz="Asia/Tokyo", due=jst(2026, 1, 1), done_at=jst(2026, 1, 2)
    )
    assert done.done and not done.is_overdue(now)


def test_todo_rejects_naive_due():
    with pytest.raises(ValidationError):
        Todo(id=1, owner_id=1, title="x", tz="Asia/Tokyo", due=datetime(2026, 1, 1))


def test_reminder_spec_day_offset_bounds():
    assert ReminderSpec(at_local="10:00", day_offset=31).day_offset == 31
    with pytest.raises(ValidationError):
        ReminderSpec(at_local="10:00", day_offset=32)


def test_max_reminders_single_definition():
    from nomusyo.schedule import models, parsing, service

    assert parsing.MAX_REMINDERS is models.MAX_REMINDERS
    assert getattr(service, "MAX_REMINDERS", models.MAX_REMINDERS) == models.MAX_REMINDERS
