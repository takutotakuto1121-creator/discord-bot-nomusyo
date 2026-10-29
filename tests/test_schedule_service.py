from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from nomusyo.schedule.clock import FixedClock
from nomusyo.schedule.errors import NotFoundError, ValidationError
from nomusyo.schedule.models import ReminderSpec, Window
from nomusyo.schedule.service import UNSET, ScheduleService
from nomusyo.storage import Database

JST = ZoneInfo("Asia/Tokyo")
NOW = datetime(2026, 10, 29, 12, 0, tzinfo=JST)


def jst(*a):
    return datetime(*a, tzinfo=JST)


@pytest.fixture
def db(tmp_path):
    d = Database.open(tmp_path / "s.db")
    yield d
    d.close()


@pytest.fixture
def clock():
    return FixedClock(NOW)


@pytest.fixture
def svc(db, clock):
    return ScheduleService(db, 1, clock=clock)


@pytest.fixture
def other(db, clock):
    return ScheduleService(db, 2, clock=clock)


def day(d, m, y=2026):
    return Window.for_day(datetime(y, m, d).date(), JST)


# ---- events --------------------------------------------------------------------------------
def test_add_and_get_event(svc):
    ev = svc.add_event(
        "  会議  ", jst(2026, 11, 3, 15), duration_min=60, note="n", place="P",
        reminders=[ReminderSpec(offset_min=10)],
    )  # fmt: skip
    got = svc.get_event(ev.id)
    assert got.title == "会議"
    assert got.start == jst(2026, 11, 3, 15)
    assert got.end == jst(2026, 11, 3, 16)
    assert got.note == "n" and got.place == "P"
    assert got.created_at == NOW
    assert [r.spec for r in got.reminders] == [ReminderSpec(offset_min=10)]


def test_add_event_stores_other_tz_wall_clock(svc):
    ev = svc.add_event("x", datetime(2026, 11, 3, 6, 0, tzinfo=ZoneInfo("UTC")))
    assert ev.start == jst(2026, 11, 3, 15)


def test_add_all_day_normalizes_midnight(svc):
    ev = svc.add_event("x", jst(2026, 11, 3, 15), all_day=True)
    assert ev.start == jst(2026, 11, 3) and ev.end == jst(2026, 11, 4)


def test_add_event_validation(svc):
    with pytest.raises(ValidationError):
        svc.add_event("x", datetime(2026, 11, 3, 15))  # naive
    with pytest.raises(ValidationError):
        svc.add_event("", jst(2026, 11, 3))
    with pytest.raises(ValidationError):
        svc.add_event("   ", jst(2026, 11, 3))
    with pytest.raises(ValidationError):
        svc.add_event("a" * 201, jst(2026, 11, 3))
    with pytest.raises(ValidationError):
        svc.add_event("x", jst(2026, 11, 3), note="n" * 2001)
    with pytest.raises(ValidationError):
        svc.add_event("x", jst(2026, 11, 3), place="p" * 201)
    with pytest.raises(ValidationError):
        svc.add_event("x", jst(2026, 11, 3), tz="Nope/Zone")
    with pytest.raises(ValidationError):
        svc.add_event("x", jst(2026, 11, 3), kind="bogus")
    with pytest.raises(ValidationError):
        svc.add_event("x", jst(2026, 11, 3), duration_min=0)
    with pytest.raises(ValidationError):
        svc.add_event("x", jst(2026, 11, 3), reminders=["10m"])
    assert svc.expand(day(3, 11)) == []  # nothing half-written


def test_too_many_reminders(svc):
    specs = [ReminderSpec(offset_min=i) for i in range(11)]
    with pytest.raises(ValidationError):
        svc.add_event("x", jst(2026, 11, 3), reminders=specs)


def test_duplicate_reminders_deduped(svc):
    ev = svc.add_event("x", jst(2026, 11, 3), reminders=[ReminderSpec(offset_min=5)] * 3)
    assert len(ev.reminders) == 1


def test_bad_default_tz(db):
    with pytest.raises(ValidationError):
        ScheduleService(db, 1, default_tz="Nope/Zone")


def test_update_unspecified_fields_unchanged(svc):
    ev = svc.add_event("t", jst(2026, 11, 3, 15), duration_min=30, note="n", place="p")
    up = svc.update_event(ev.id, title="new")
    assert up.title == "new"
    assert (up.note, up.place, up.duration_min, up.start) == ("n", "p", 30, ev.start)


def test_update_none_clears(svc):
    ev = svc.add_event("t", jst(2026, 11, 3, 15), duration_min=30, note="n", place="p")
    up = svc.update_event(ev.id, note=None, place=None, duration_min=None)
    assert (up.note, up.place, up.duration_min) == (None, None, None)
    assert up.title == "t"


def test_update_blank_note_clears_blank_title_rejected(svc):
    ev = svc.add_event("t", jst(2026, 11, 3, 15), note="n")
    assert svc.update_event(ev.id, note="  ").note is None
    with pytest.raises(ValidationError):
        svc.update_event(ev.id, title="")
    assert svc.get_event(ev.id).title == "t"


def test_update_start_and_all_day(svc):
    ev = svc.add_event("t", jst(2026, 11, 3, 15))
    up = svc.update_event(ev.id, start=jst(2026, 11, 5, 9))
    assert up.start == jst(2026, 11, 5, 9)
    ad = svc.update_event(ev.id, all_day=True)
    assert ad.all_day and ad.start == jst(2026, 11, 5)
    back = svc.update_event(ev.id, all_day=False, start=jst(2026, 11, 5, 8))
    assert not back.all_day and back.start == jst(2026, 11, 5, 8)
    assert svc.expand(day(5, 11))[0].event_id == ev.id
    assert svc.expand(day(3, 11)) == []


def test_update_naive_start_rejected(svc):
    ev = svc.add_event("t", jst(2026, 11, 3, 15))
    with pytest.raises(ValidationError):
        svc.update_event(ev.id, start=datetime(2026, 11, 4, 9))


def test_update_reminders_replace_and_keep(svc):
    ev = svc.add_event("t", jst(2026, 11, 3, 15), reminders=[ReminderSpec(offset_min=5)])
    assert len(svc.update_event(ev.id, title="u").reminders) == 1  # unspecified = kept
    up = svc.update_event(
        ev.id, reminders=[ReminderSpec(offset_min=1), ReminderSpec(at_local="08:00")]
    )
    assert len(up.reminders) == 2
    assert svc.update_event(ev.id, reminders=[]).reminders == ()
    got = svc.set_event_reminders(ev.id, [ReminderSpec(offset_min=9)])
    assert got.reminders[0].spec.offset_min == 9


def test_soft_delete_and_restore(svc):
    ev = svc.add_event("t", jst(2026, 11, 3, 15))
    d = svc.delete_event(ev.id)
    assert d.deleted
    with pytest.raises(NotFoundError):
        svc.get_event(ev.id)
    assert svc.get_event(ev.id, include_deleted=True).deleted
    assert svc.expand(day(3, 11)) == []
    with pytest.raises(NotFoundError):
        svc.delete_event(ev.id)
    with pytest.raises(NotFoundError):
        svc.update_event(ev.id, title="x")
    r = svc.restore_event(ev.id)
    assert not r.deleted
    assert len(svc.expand(day(3, 11))) == 1
    assert not svc.restore_event(ev.id).deleted  # restoring a live event is a no-op


def test_event_not_found_and_other_owner(svc, other):
    ev = svc.add_event("mine", jst(2026, 11, 3, 15))
    for call in (
        lambda s: s.get_event(ev.id),
        lambda s: s.update_event(ev.id, title="x"),
        lambda s: s.delete_event(ev.id),
        lambda s: s.restore_event(ev.id),
        lambda s: s.set_event_reminders(ev.id, []),
        lambda s: s.get_event(9999),
        lambda s: s.delete_event(9999),
        lambda s: s.restore_event(9999),
        lambda s: s.get_event(-1),
    ):
        with pytest.raises(NotFoundError):
            call(other)
    assert svc.get_event(ev.id).title == "mine"
    assert other.expand(day(3, 11)) == []


def test_other_owner_cannot_see_deleted_either(svc, other):
    ev = svc.add_event("mine", jst(2026, 11, 3, 15))
    svc.delete_event(ev.id)
    with pytest.raises(NotFoundError):
        other.get_event(ev.id, include_deleted=True)


def test_sql_injection_title(svc):
    evil = "'; DROP TABLE events; --"
    ev = svc.add_event(evil, jst(2026, 11, 3, 15))
    assert svc.get_event(ev.id).title == evil
    t = svc.add_todo(evil)
    assert svc.get_todo(t.id).title == evil
    assert svc.list_todos("all")[0].title == evil


# ---- expand --------------------------------------------------------------------------------
def test_expand_window_is_half_open(svc):
    w = day(3, 11)
    at_start = svc.add_event("start", jst(2026, 11, 3, 0, 0))
    at_end = svc.add_event("end", jst(2026, 11, 4, 0, 0))
    ids = [o.event_id for o in svc.expand(w)]
    assert at_start.id in ids and at_end.id not in ids


def test_expand_duration_event_overlap_edges(svc):
    w = day(3, 11)
    ends_at_start = svc.add_event("a", jst(2026, 11, 2, 23, 0), duration_min=60)
    crosses_in = svc.add_event("b", jst(2026, 11, 2, 23, 30), duration_min=60)
    crosses_out = svc.add_event("c", jst(2026, 11, 3, 23, 30), duration_min=60)
    ids = [o.event_id for o in svc.expand(w)]
    assert ends_at_start.id not in ids
    assert crosses_in.id in ids and crosses_out.id in ids


def test_expand_all_day_first_then_by_start(svc):
    late = svc.add_event("late", jst(2026, 11, 3, 18))
    timed_midnight = svc.add_event("mid", jst(2026, 11, 3, 0, 0))
    ad = svc.add_event("ad", jst(2026, 11, 3), all_day=True)
    early = svc.add_event("early", jst(2026, 11, 3, 9))
    got = [o.event_id for o in svc.expand(day(3, 11))]
    assert got == [ad.id, timed_midnight.id, early.id, late.id]
    assert svc.expand(day(3, 11))[0].all_day


def test_expand_multi_day_all_day(svc):
    ev = svc.add_event("trip", jst(2026, 11, 3), all_day=True, duration_min=3 * 1440)
    for d in (3, 4, 5):
        assert [o.event_id for o in svc.expand(day(d, 11))] == [ev.id]
    assert svc.expand(day(6, 11)) == []
    assert svc.expand(day(2, 11)) == []


def test_expand_across_year_boundary(svc):
    ev = svc.add_event("nye", jst(2026, 12, 31, 23, 30), duration_min=60)
    assert [o.event_id for o in svc.expand(day(1, 1, 2027))] == [ev.id]
    assert [o.event_id for o in svc.expand(day(31, 12))] == [ev.id]
    ny = svc.add_event("ny", jst(2027, 1, 1, 0, 0))
    both = Window(jst(2026, 12, 31), jst(2027, 1, 2))
    assert [o.event_id for o in svc.expand(both)] == [ev.id, ny.id]


def test_expand_month_end(svc):
    a = svc.add_event("a", jst(2026, 10, 31, 23, 59))
    b = svc.add_event("b", jst(2026, 11, 1, 0, 0))
    assert [o.event_id for o in svc.expand(day(31, 10))] == [a.id]
    assert [o.event_id for o in svc.expand(day(1, 11))] == [b.id]
    month = Window(jst(2026, 10, 1), jst(2026, 11, 1))
    assert [o.event_id for o in svc.expand(month)] == [a.id]


def test_expand_leap_day(svc):
    ev = svc.add_event("leap", jst(2028, 2, 29, 10))
    assert [o.event_id for o in svc.expand(day(29, 2, 2028))] == [ev.id]
    assert svc.expand(day(1, 3, 2028)) == []


def test_expand_instant_event_at_window_start(svc):
    ev = svc.add_event("i", jst(2026, 11, 3, 9))
    w = Window(jst(2026, 11, 3, 9), jst(2026, 11, 3, 10))
    assert [o.event_id for o in svc.expand(w)] == [ev.id]
    w2 = Window(jst(2026, 11, 3, 9, 0, 1), jst(2026, 11, 3, 10))
    assert svc.expand(w2) == []


def test_expand_reminder_kind_filter(svc):
    r = svc.add_event("r", jst(2026, 11, 3, 9), kind="reminder")
    e = svc.add_event("e", jst(2026, 11, 3, 10))
    assert [o.event_id for o in svc.expand(day(3, 11))] == [r.id, e.id]
    assert [o.event_id for o in svc.expand(day(3, 11), include_reminders_kind=False)] == [e.id]


def test_expand_occurrence_fields(svc):
    ev = svc.add_event("t", jst(2026, 11, 3, 15), duration_min=30, place="P", note="N")
    o = svc.expand(day(3, 11))[0]
    assert (o.title, o.place, o.note, o.kind, o.all_day) == ("t", "P", "N", "event", False)
    assert o.occurrence_local == "2026-11-03T15:00:00"
    assert o.start == ev.start and o.end == ev.end


def test_expand_other_timezone_event(svc):
    ny = ZoneInfo("America/New_York")
    ev = svc.add_event("ny", datetime(2026, 11, 3, 9, 0, tzinfo=ny), tz="America/New_York")
    # 09:00 NY (EST) = 23:00 JST same day
    assert [o.event_id for o in svc.expand(day(3, 11))] == [ev.id]
    assert ev.tz == "America/New_York"


# ---- todos ---------------------------------------------------------------------------------
def test_add_todo_defaults(svc):
    t = svc.add_todo("  買い物 ")
    assert t.title == "買い物" and t.due is None and not t.done and t.priority == 0
    assert t.list_name == "todo" and t.reminders == ()


def test_add_todo_validation(svc):
    with pytest.raises(ValidationError):
        svc.add_todo("")
    with pytest.raises(ValidationError):
        svc.add_todo("x", priority=4)
    with pytest.raises(ValidationError):
        svc.add_todo("x", priority=-1)
    with pytest.raises(ValidationError):
        svc.add_todo("x", due=datetime(2026, 11, 1))
    with pytest.raises(ValidationError):
        svc.add_todo("x", due_all_day=True)
    with pytest.raises(ValidationError):
        svc.add_todo("x", list_name=" ")
    with pytest.raises(ValidationError):
        svc.add_todo("x", tz="Bad/Zone")


def test_remind_without_due_rejected(svc):
    with pytest.raises(ValidationError):
        svc.add_todo("x", reminders=[ReminderSpec(offset_min=10)])
    assert svc.list_todos("all") == []
    t = svc.add_todo("x")
    with pytest.raises(ValidationError):
        svc.update_todo(t.id, reminders=[ReminderSpec(offset_min=10)])
    assert svc.get_todo(t.id).reminders == ()


def test_todo_with_due_and_reminders(svc):
    t = svc.add_todo("x", due=jst(2026, 11, 3, 18), reminders=[ReminderSpec(offset_min=30)])
    assert t.due == jst(2026, 11, 3, 18) and len(t.reminders) == 1


def test_date_only_due(svc):
    t = svc.add_todo("x", due=jst(2026, 11, 3, 15), due_all_day=True)
    assert t.due == jst(2026, 11, 3) and t.due_all_day


def test_clearing_due_removes_rules_n3(svc, db):
    t = svc.add_todo("x", due=jst(2026, 11, 3, 18), reminders=[ReminderSpec(offset_min=30)])
    up = svc.update_todo(t.id, due=None)
    assert up.due is None and up.reminders == ()
    assert db.query("SELECT * FROM reminder_rules") == []
    assert not up.due_all_day


def test_clearing_due_on_date_only(svc):
    t = svc.add_todo("x", due=jst(2026, 11, 3), due_all_day=True)
    up = svc.update_todo(t.id, due=None)
    assert up.due is None and not up.due_all_day


def test_update_todo_keeps_unspecified(svc):
    t = svc.add_todo(
        "x", due=jst(2026, 11, 3, 18), note="n", priority=2,
        reminders=[ReminderSpec(offset_min=30)],
    )  # fmt: skip
    up = svc.update_todo(t.id, title="y")
    assert (up.note, up.priority, up.due, len(up.reminders)) == ("n", 2, t.due, 1)
    up = svc.update_todo(t.id, note=None, priority=0)
    assert up.note is None and up.priority == 0
    with pytest.raises(ValidationError):
        svc.update_todo(t.id, priority=9)
    with pytest.raises(ValidationError):
        svc.update_todo(t.id, title=" ")


def test_set_due_on_no_due_todo(svc):
    t = svc.add_todo("x")
    up = svc.update_todo(t.id, due=jst(2026, 11, 3), due_all_day=True)
    assert up.due == jst(2026, 11, 3) and up.due_all_day
    up2 = svc.update_todo(t.id, due_all_day=False)
    assert not up2.due_all_day and up2.due == jst(2026, 11, 3)


def test_due_all_day_without_due_is_ignored(svc):
    t = svc.add_todo("x")
    with pytest.raises(ValidationError, match="期限が必要です"):
        svc.update_todo(t.id, due_all_day=True)
    with pytest.raises(ValidationError, match="期限が必要です"):
        svc.add_todo("y", due_all_day=True)


def test_complete_is_idempotent_keeps_first_time(svc, clock):
    t = svc.add_todo("x")
    first = svc.complete_todo(t.id)
    assert first.done and first.done_at == NOW
    clock.advance(timedelta(hours=3))
    again = svc.complete_todo(t.id)
    assert again.done_at == first.done_at


def test_reopen(svc):
    t = svc.add_todo("x")
    svc.complete_todo(t.id)
    assert not svc.reopen_todo(t.id).done
    assert not svc.reopen_todo(t.id).done  # reopen of open is fine


def test_todo_delete_restore(svc):
    t = svc.add_todo("x")
    d = svc.delete_todo(t.id)
    assert d.deleted_at is not None
    with pytest.raises(NotFoundError):
        svc.get_todo(t.id)
    with pytest.raises(NotFoundError):
        svc.complete_todo(t.id)
    with pytest.raises(NotFoundError):
        svc.delete_todo(t.id)
    assert svc.list_todos("all") == []
    assert svc.get_todo(t.id, include_deleted=True).deleted_at is not None
    r = svc.restore_todo(t.id)
    assert r.deleted_at is None
    assert [x.id for x in svc.list_todos("all")] == [t.id]


def test_todo_not_found_and_other_owner(svc, other):
    t = svc.add_todo("mine")
    for call in (
        lambda s: s.get_todo(t.id),
        lambda s: s.complete_todo(t.id),
        lambda s: s.reopen_todo(t.id),
        lambda s: s.delete_todo(t.id),
        lambda s: s.restore_todo(t.id),
        lambda s: s.update_todo(t.id, title="x"),
        lambda s: s.get_todo(9999),
        lambda s: s.complete_todo(9999),
        lambda s: s.restore_todo(9999),
    ):
        with pytest.raises(NotFoundError):
            call(other)
    assert other.list_todos("all") == []


def test_list_todos_statuses(svc, clock):
    overdue = svc.add_todo("overdue", due=jst(2026, 10, 29, 11, 0))
    today_date = svc.add_todo("today-date", due=jst(2026, 10, 29), due_all_day=True)
    yesterday_date = svc.add_todo("yday-date", due=jst(2026, 10, 28), due_all_day=True)
    future = svc.add_todo("future", due=jst(2026, 11, 3, 9))
    nodue = svc.add_todo("nodue")
    done = svc.add_todo("done", due=jst(2026, 10, 1))
    svc.complete_todo(done.id)
    gone = svc.add_todo("gone")
    svc.delete_todo(gone.id)

    def ids(status):
        return [t.id for t in svc.list_todos(status)]

    assert ids("open") == [yesterday_date.id, today_date.id, overdue.id, future.id, nodue.id]
    assert ids("done") == [done.id]
    assert ids("overdue") == [yesterday_date.id, overdue.id]  # date-only due today: not yet
    assert ids("no_due") == [nodue.id]
    assert sorted(ids("all")) == sorted(
        [overdue.id, today_date.id, yesterday_date.id, future.id, nodue.id, done.id]
    )
    clock.set(jst(2026, 10, 30, 0, 0))
    assert today_date.id in ids("overdue")
    with pytest.raises(ValidationError):
        svc.list_todos("bogus")


def test_overdue_exact_boundary(svc, clock):
    t = svc.add_todo("x", due=jst(2026, 10, 29, 12, 0))
    assert [x.id for x in svc.list_todos("overdue")] == [t.id]
    clock.set(jst(2026, 10, 29, 11, 59, 59))
    assert svc.list_todos("overdue") == []


def test_completed_todo_not_overdue(svc):
    t = svc.add_todo("x", due=jst(2026, 10, 1))
    svc.complete_todo(t.id)
    assert svc.list_todos("overdue") == []


def test_open_order_priority_then_id(svc):
    a = svc.add_todo("a", due=jst(2026, 11, 3, 9), priority=0)
    b = svc.add_todo("b", due=jst(2026, 11, 3, 9), priority=3)
    c = svc.add_todo("c", due=jst(2026, 11, 3, 9), priority=3)
    n1 = svc.add_todo("n1", priority=1)
    n2 = svc.add_todo("n2", priority=2)
    assert [t.id for t in svc.list_todos("open")] == [b.id, c.id, a.id, n2.id, n1.id]


def test_done_order_newest_first(svc, clock):
    a = svc.add_todo("a")
    b = svc.add_todo("b")
    svc.complete_todo(a.id)
    clock.advance(timedelta(minutes=5))
    svc.complete_todo(b.id)
    assert [t.id for t in svc.list_todos("done")] == [b.id, a.id]


def test_list_filter_by_list_name(svc):
    a = svc.add_todo("a", list_name="shop")
    svc.add_todo("b")
    assert [t.id for t in svc.list_todos("open", list_name="shop")] == [a.id]


def test_reopen_after_due_passed_is_overdue_again(svc):
    t = svc.add_todo("x", due=jst(2026, 10, 1))
    svc.complete_todo(t.id)
    svc.reopen_todo(t.id)
    assert [x.id for x in svc.list_todos("overdue")] == [t.id]


def test_unset_marker_is_distinct_from_none():
    assert UNSET is not None
    assert repr(UNSET) == "UNSET"


# ---- date-only TODO vs offset_min rules ----------------------------------------------------
def test_add_todo_all_day_rejects_offset_rule(svc):
    with pytest.raises(ValidationError):
        svc.add_todo(
            "x", due=jst(2026, 11, 3), due_all_day=True, reminders=[ReminderSpec(offset_min=10)]
        )
    assert svc.list_todos("all") == []


def test_add_todo_all_day_allows_at_local_rule(svc):
    t = svc.add_todo(
        "x",
        due=jst(2026, 11, 3),
        due_all_day=True,
        reminders=[ReminderSpec(at_local="20:00", day_offset=1)],
    )
    assert len(t.reminders) == 1


def test_update_todo_to_all_day_with_existing_offset_rejected(svc):
    t = svc.add_todo("x", due=jst(2026, 11, 3, 18), reminders=[ReminderSpec(offset_min=10)])
    with pytest.raises(ValidationError):
        svc.update_todo(t.id, due=jst(2026, 11, 3), due_all_day=True)
    got = svc.get_todo(t.id)
    assert not got.due_all_day and got.due == jst(2026, 11, 3, 18)
    assert [r.spec for r in got.reminders] == [ReminderSpec(offset_min=10)]


def test_update_todo_to_all_day_replacing_reminders_allowed(svc):
    t = svc.add_todo("x", due=jst(2026, 11, 3, 18), reminders=[ReminderSpec(offset_min=10)])
    up = svc.update_todo(
        t.id,
        due=jst(2026, 11, 3),
        due_all_day=True,
        reminders=[ReminderSpec(at_local="20:00", day_offset=1)],
    )
    assert up.due_all_day
    assert [r.spec for r in up.reminders] == [ReminderSpec(at_local="20:00", day_offset=1)]


def test_update_todo_offset_rule_on_all_day_rejected(svc):
    t = svc.add_todo("x", due=jst(2026, 11, 3), due_all_day=True)
    with pytest.raises(ValidationError):
        svc.update_todo(t.id, reminders=[ReminderSpec(offset_min=5)])


def test_update_todo_clear_due_normalizes_all_day_and_clears_rules(svc):
    t = svc.add_todo(
        "x",
        due=jst(2026, 11, 3),
        due_all_day=True,
        reminders=[ReminderSpec(at_local="20:00", day_offset=1)],
    )
    up = svc.update_todo(t.id, due=None)
    assert up.due is None and up.due_all_day is False and up.reminders == ()
    up2 = svc.update_todo(t.id, due=None, due_all_day=False)
    assert up2.due is None and not up2.due_all_day


# ---- restore/reopen: no-op leaves updated_at; one transaction -------------------------------
def test_restore_and_reopen_noop_keep_updated_at(svc, clock):
    ev = svc.add_event("e", jst(2026, 11, 3, 15))
    t = svc.add_todo("t")
    clock.advance(timedelta(hours=1))
    assert svc.restore_event(ev.id).updated_at == ev.updated_at
    assert svc.restore_todo(t.id).updated_at == t.updated_at
    assert svc.reopen_todo(t.id).updated_at == t.updated_at


def test_restore_and_reopen_real_change_updates_updated_at(svc, clock):
    ev = svc.add_event("e", jst(2026, 11, 3, 15))
    t = svc.add_todo("t")
    svc.delete_event(ev.id)
    svc.delete_todo(t.id)
    clock.advance(timedelta(hours=1))
    assert svc.restore_event(ev.id).updated_at == clock.now()
    assert svc.restore_todo(t.id).updated_at == clock.now()
    svc.complete_todo(t.id)
    clock.advance(timedelta(hours=1))
    assert svc.reopen_todo(t.id).updated_at == clock.now()


def test_restore_reopen_other_owner_not_found(svc, other):
    t = svc.add_todo("t")
    ev = svc.add_event("e", jst(2026, 11, 3, 15))
    for fn, i in [
        (other.restore_todo, t.id),
        (other.reopen_todo, t.id),
        (other.restore_event, ev.id),
    ]:
        with pytest.raises(NotFoundError):
            fn(i)


def test_update_event_and_todo_use_single_transaction(svc, db, monkeypatch):
    ev = svc.add_event("e", jst(2026, 11, 3, 15))
    t = svc.add_todo("t")
    calls = []
    orig = db.transaction

    def counting():
        calls.append(1)
        return orig()

    monkeypatch.setattr(db, "transaction", counting)
    svc.update_event(ev.id, title="e2")
    assert len(calls) == 1
    svc.update_todo(t.id, title="t2")
    assert len(calls) == 2
    svc.restore_event(ev.id)
    svc.restore_todo(t.id)
    svc.reopen_todo(t.id)
    assert len(calls) == 5
