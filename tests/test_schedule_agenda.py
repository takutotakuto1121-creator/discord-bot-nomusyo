from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from nomusyo.schedule.agenda import Holiday, assemble_agenda, build_agenda
from nomusyo.schedule.clock import FixedClock
from nomusyo.schedule.errors import ValidationError
from nomusyo.schedule.service import ScheduleService
from nomusyo.storage import Database

JST = ZoneInfo("Asia/Tokyo")
NOW = datetime(2026, 10, 29, 12, 0, tzinfo=JST)


def jst(*a):
    return datetime(*a, tzinfo=JST)


@pytest.fixture
def db(tmp_path):
    d = Database.open(tmp_path / "t.db")
    yield d
    d.close()


@pytest.fixture
def svc(db):
    return ScheduleService(db, 1, clock=FixedClock(NOW))


def titles(day):
    return [o.title for o in day.occurrences]


# ===== ordering ================================================================================
def test_order_all_day_first_then_time_then_id(svc):
    late = svc.add_event("late", jst(2026, 10, 29, 18, 0))
    early = svc.add_event("early", jst(2026, 10, 29, 9, 0))
    same_b = svc.add_event("same-b", jst(2026, 10, 29, 13, 0))
    same_a = svc.add_event("same-a", jst(2026, 10, 29, 13, 0))
    allday = svc.add_event("allday", jst(2026, 10, 29, 0, 0), all_day=True)
    day = build_agenda(svc).days[0]
    assert titles(day) == ["allday", "early", "same-b", "same-a", "late"]
    assert same_b.id < same_a.id
    assert {late.id, early.id, allday.id}


def test_overnight_event_appears_on_both_days(svc):
    svc.add_event("night", jst(2026, 10, 28, 23, 0), duration_min=120)
    svc.add_event("morning", jst(2026, 10, 29, 0, 30))
    ag = build_agenda(svc, days=2, start_date=date(2026, 10, 28))
    assert titles(ag.days[0]) == ["night"]
    # carried-over event counts as starting at midnight -> before the 00:30 event
    assert titles(ag.days[1]) == ["night", "morning"]


def test_range_boundary_next_midnight_excluded(svc):
    svc.add_event("next-midnight", jst(2026, 10, 30, 0, 0))
    svc.add_event("last-minute", jst(2026, 10, 29, 23, 59))
    ag = build_agenda(svc)
    assert titles(ag.days[0]) == ["last-minute"]


def test_event_ending_exactly_at_midnight_not_in_next_day(svc):
    svc.add_event("until-midnight", jst(2026, 10, 29, 22, 0), duration_min=120)
    ag = build_agenda(svc, days=2)
    assert titles(ag.days[0]) == ["until-midnight"]
    assert titles(ag.days[1]) == []


def test_days_count_and_default_start(svc):
    ag = build_agenda(svc, days=7)
    assert len(ag.days) == 7
    assert ag.start_date == date(2026, 10, 29)
    assert ag.end_date == date(2026, 11, 4)
    assert ag.tz == "Asia/Tokyo"
    assert ag.empty


def test_year_boundary_and_month_end(svc):
    svc.add_event("nye", jst(2026, 12, 31, 23, 0))
    svc.add_event("ny", jst(2027, 1, 1, 9, 0))
    ag = build_agenda(svc, days=3, start_date=date(2026, 12, 31))
    assert [d.date for d in ag.days] == [date(2026, 12, 31), date(2027, 1, 1), date(2027, 1, 2)]
    assert titles(ag.days[0]) == ["nye"]
    assert titles(ag.days[1]) == ["ny"]
    assert build_agenda(svc, days=2, start_date=date(2026, 10, 31)).days[1].date == date(
        2026, 11, 1
    )


def test_invalid_days_raises(svc):
    with pytest.raises(ValidationError):
        assemble_agenda([], [], start_date=date(2026, 10, 29), days=0, zone=JST, now=NOW)


def test_deleted_event_excluded(svc):
    e = svc.add_event("gone", jst(2026, 10, 29, 15, 0))
    svc.delete_event(e.id)
    assert build_agenda(svc).empty


# ===== TODO ====================================================================================
def test_todo_overdue_and_in_range_no_duplicates(svc):
    past = svc.add_todo("past", due=jst(2026, 10, 29, 9, 0))
    today = svc.add_todo("today-later", due=jst(2026, 10, 29, 20, 0))
    week = svc.add_todo("in-week", due=jst(2026, 11, 2, 10, 0))
    beyond = svc.add_todo("beyond", due=jst(2026, 11, 5, 10, 0))
    ag = build_agenda(svc, days=7)
    assert [t.id for t in ag.overdue] == [past.id]
    assert [t.id for t in ag.due_in_range] == [today.id, week.id]
    ids = [t.id for t in (*ag.overdue, *ag.due_in_range)]
    assert len(ids) == len(set(ids))
    assert beyond.id not in ids


def test_todo_range_end_exclusive(svc):
    svc.add_todo("edge", due=jst(2026, 11, 5, 0, 0))
    svc.add_todo("inside", due=jst(2026, 11, 4, 23, 59))
    ag = build_agenda(svc, days=7)
    assert [t.title for t in ag.due_in_range] == ["inside"]


def test_all_day_due_overdue_from_next_midnight(svc):
    # date-only due today is not overdue at 12:00; yesterday's is overdue
    today = svc.add_todo("today", due=jst(2026, 10, 29, 0, 0), due_all_day=True)
    yest = svc.add_todo("yesterday", due=jst(2026, 10, 28, 0, 0), due_all_day=True)
    ag = build_agenda(svc)
    assert [t.id for t in ag.overdue] == [yest.id]
    assert [t.id for t in ag.due_in_range] == [today.id]
    # at 00:00 next day it flips
    later = assemble_agenda(
        [],
        [today],
        start_date=date(2026, 10, 30),
        days=1,
        zone=JST,
        now=jst(2026, 10, 30, 0, 0),
    )
    assert [t.id for t in later.overdue] == [today.id]
    assert later.due_in_range == ()


def test_done_nodue_deleted_todos_excluded(svc):
    done = svc.add_todo("done", due=jst(2026, 10, 28, 9, 0))
    svc.complete_todo(done.id)
    svc.add_todo("nodue")
    deleted = svc.add_todo("deleted", due=jst(2026, 10, 28, 9, 0))
    svc.delete_todo(deleted.id)
    ag = build_agenda(svc, days=7)
    assert ag.overdue == () and ag.due_in_range == ()
    assert ag.empty


def test_assemble_ignores_done_todos_passed_directly(svc):
    t = svc.add_todo("x", due=jst(2026, 10, 28, 9, 0))
    done = svc.complete_todo(t.id)
    ag = assemble_agenda([], [done], start_date=date(2026, 10, 29), days=1, zone=JST, now=NOW)
    assert ag.overdue == ()


# ===== holidays ================================================================================
def test_holidays_attached_to_days(svc):
    hs = [
        Holiday(date(2026, 11, 3), "文化の日"),
        Holiday(date(2026, 11, 3), "あ振替"),
        Holiday(date(2026, 12, 25), "範囲外"),
    ]
    ag = build_agenda(svc, days=7, holidays=hs)
    day = next(d for d in ag.days if d.date == date(2026, 11, 3))
    assert [h.name for h in day.holidays] == ["あ振替", "文化の日"]
    assert not day.empty
    assert not ag.empty
    assert all(not d.holidays for d in ag.days if d.date != date(2026, 11, 3))


def test_no_holidays_by_default(svc):
    assert all(d.holidays == () for d in build_agenda(svc, days=7).days)
