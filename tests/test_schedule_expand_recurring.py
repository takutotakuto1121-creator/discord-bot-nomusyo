"""ScheduleService.expand with rrule rows (V2-50). Rules are set via SQL (no add API yet)."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from nomusyo.schedule.agenda import build_agenda
from nomusyo.schedule.clock import FixedClock
from nomusyo.schedule.models import Window, to_utc_str
from nomusyo.schedule.recurrence import series_until_utc
from nomusyo.schedule.service import ScheduleService
from nomusyo.storage import Database

JST = ZoneInfo("Asia/Tokyo")
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=JST)


def jst(*a):
    return datetime(*a, tzinfo=JST)


@pytest.fixture
def db(tmp_path):
    d = Database.open(tmp_path / "s.db")
    yield d
    d.close()


@pytest.fixture
def svc(db):
    return ScheduleService(db, 1, clock=FixedClock(NOW))


def set_rrule(db, svc, ev, rrule, *, set_until=True):
    until = None
    if set_until:
        span = ev.end - ev.start
        u = series_until_utc(rrule, ev.start, span)
        until = to_utc_str(u) if u else None
    db.execute("UPDATE events SET rrule = ?, until_utc = ? WHERE id = ?", (rrule, until, ev.id))


def titles(occs):
    return [(o.title, o.start.day) for o in occs]


def test_daily_expands_in_window(db, svc):
    ev = svc.add_event("daily", jst(2026, 1, 1, 9), duration_min=30)
    set_rrule(db, svc, ev, "FREQ=DAILY")
    occs = svc.expand(Window(jst(2026, 1, 3), jst(2026, 1, 6)))
    assert [o.start.day for o in occs] == [3, 4, 5]
    assert {o.event_id for o in occs} == {ev.id}
    assert occs[0].occurrence_local == "2026-01-03T09:00:00"
    assert occs[0].end == jst(2026, 1, 3, 9, 30)


def test_window_before_dtstart_empty(db, svc):
    ev = svc.add_event("daily", jst(2026, 2, 1, 9))
    set_rrule(db, svc, ev, "FREQ=DAILY")
    assert svc.expand(Window(jst(2026, 1, 1), jst(2026, 1, 10))) == []


def test_finite_series_before_window_excluded_by_until(db, svc):
    ev = svc.add_event("old", jst(2026, 1, 1, 9), duration_min=60)
    set_rrule(db, svc, ev, "FREQ=DAILY;COUNT=3")
    assert svc.expand(Window(jst(2026, 1, 10), jst(2026, 1, 12))) == []
    assert len(svc.expand(Window(jst(2026, 1, 1), jst(2026, 1, 12)))) == 3


def test_span_started_before_window(db, svc):
    ev = svc.add_event("night", jst(2026, 1, 1, 22), duration_min=180)
    set_rrule(db, svc, ev, "FREQ=DAILY")
    occs = svc.expand(Window(jst(2026, 1, 3, 0, 30), jst(2026, 1, 3, 1)))
    assert [o.occurrence_local for o in occs] == ["2026-01-02T22:00:00"]


def test_all_day_recurring(db, svc):
    ev = svc.add_event("holiday", jst(2026, 1, 1), all_day=True)
    set_rrule(db, svc, ev, "FREQ=WEEKLY;BYDAY=MO")  # 2026-01-01 is Thursday
    occs = svc.expand(Window(jst(2026, 1, 1), jst(2026, 1, 20)))
    assert all(o.all_day for o in occs)
    assert [o.start.day for o in occs] == [5, 12, 19]
    assert occs[0].end - occs[0].start == occs[-1].end - occs[-1].start


def test_instant_event_recurring(db, svc):
    ev = svc.add_event("ping", jst(2026, 1, 1, 9))
    set_rrule(db, svc, ev, "FREQ=DAILY")
    occs = svc.expand(Window(jst(2026, 1, 2, 9), jst(2026, 1, 3, 9)))
    assert [o.start.day for o in occs] == [2]
    assert occs[0].end == occs[0].start


def test_corrupt_rrule_skipped_listing_survives(db, svc):
    good = svc.add_event("good", jst(2026, 1, 2, 9))
    bad = svc.add_event("bad", jst(2026, 1, 2, 10))
    for rule in ("FREQ=HOURLY", "garbage", "FREQ=DAILY;BYHOUR=1"):
        db.execute("UPDATE events SET rrule = ? WHERE id = ?", (rule, bad.id))
        occs = svc.expand(Window(jst(2026, 1, 1), jst(2026, 1, 5)))
        assert [o.title for o in occs] == ["good"]
    assert good.id != bad.id


def test_single_events_unchanged_and_mixed_sorted(db, svc):
    rec = svc.add_event("rec", jst(2026, 1, 1, 9))
    set_rrule(db, svc, rec, "FREQ=DAILY")
    svc.add_event("once", jst(2026, 1, 2, 8))
    occs = svc.expand(Window(jst(2026, 1, 2), jst(2026, 1, 3)))
    assert [o.title for o in occs] == ["once", "rec"]
    once = occs[0]
    assert once.occurrence_local == "2026-01-02T08:00:00"


def test_deleted_recurring_excluded(db, svc):
    ev = svc.add_event("rec", jst(2026, 1, 1, 9))
    set_rrule(db, svc, ev, "FREQ=DAILY")
    svc.delete_event(ev.id)
    assert svc.expand(Window(jst(2026, 1, 2), jst(2026, 1, 5))) == []


def test_reminder_kind_filter_ignores_nothing_for_events(db, svc):
    ev = svc.add_event("rec", jst(2026, 1, 1, 9))
    set_rrule(db, svc, ev, "FREQ=DAILY")
    occs = svc.expand(Window(jst(2026, 1, 2), jst(2026, 1, 3)), include_reminders_kind=False)
    assert len(occs) == 1


def test_other_owner_not_visible(db, svc):
    ev = svc.add_event("rec", jst(2026, 1, 1, 9))
    set_rrule(db, svc, ev, "FREQ=DAILY")
    other = ScheduleService(db, 2, clock=FixedClock(NOW))
    assert other.expand(Window(jst(2026, 1, 2), jst(2026, 1, 3))) == []


def test_max_results_applies_per_event(db, svc, monkeypatch):
    from nomusyo.schedule import recurrence

    monkeypatch.setattr(recurrence, "MAX_RESULTS", 3)
    ev = svc.add_event("rec", jst(2026, 1, 1, 9))
    set_rrule(db, svc, ev, "FREQ=DAILY")
    assert len(svc.expand(Window(jst(2026, 1, 1), jst(2026, 3, 1)))) == 3


def test_dst_gap_through_service(db):
    ny = ZoneInfo("America/New_York")
    s = ScheduleService(
        db, 1, clock=FixedClock(datetime(2024, 3, 1, tzinfo=ny)), default_tz="America/New_York"
    )
    ev = s.add_event("x", datetime(2024, 3, 8, 2, 30, tzinfo=ny), duration_min=30)
    set_rrule(db, s, ev, "FREQ=DAILY")
    occs = s.expand(Window(datetime(2024, 3, 10, tzinfo=ny), datetime(2024, 3, 11, tzinfo=ny)))
    assert len(occs) == 1
    assert occs[0].occurrence_local == "2024-03-10T02:30:00"
    assert (occs[0].start.hour, occs[0].start.minute) == (3, 30)


def test_build_agenda_includes_recurring(db, svc):
    ev = svc.add_event("standup", jst(2026, 1, 1, 9))
    set_rrule(db, svc, ev, "FREQ=DAILY")
    ag = build_agenda(svc, days=3, start_date=date(2026, 1, 1))
    assert [[o.title for o in d.occurrences] for d in ag.days] == [["standup"]] * 3


def test_build_agenda_today_weekday_rule(db, svc):
    ev = svc.add_event("work", jst(2025, 12, 29, 9))  # Monday
    set_rrule(db, svc, ev, "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR")
    ag = build_agenda(svc, days=4, start_date=date(2026, 1, 1))  # Thu Fri Sat Sun
    assert [len(d.occurrences) for d in ag.days] == [1, 1, 0, 0]
