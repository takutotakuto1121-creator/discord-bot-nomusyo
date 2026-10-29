"""Notification planner (V2-53): real SQLite in tmp, fixed ``now``."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from nomusyo.schedule import planner
from nomusyo.schedule.clock import FixedClock
from nomusyo.schedule.models import ReminderSpec, to_utc_str
from nomusyo.schedule.planner import (
    MATERIALIZE_DAYS,
    STALE_DAYS,
    PlanStats,
    reconcile_event,
    reconcile_todo,
)
from nomusyo.schedule.recurrence import series_until_utc
from nomusyo.schedule.service import ScheduleService
from nomusyo.storage import Database

JST = ZoneInfo("Asia/Tokyo")
NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 29, 12, 0, tzinfo=JST)


def jst(*a):
    return datetime(*a, tzinfo=JST)


@pytest.fixture
def db(tmp_path):
    d = Database.open(tmp_path / "p.db")
    yield d
    d.close()


@pytest.fixture
def clock():
    return FixedClock(NOW)


@pytest.fixture
def svc(db, clock):
    return ScheduleService(db, 1, clock=clock)


def rows(db, **where):
    sql = "SELECT * FROM notifications"
    if where:
        sql += " WHERE " + " AND ".join(f"{k} = ?" for k in where)
    return db.query(sql + " ORDER BY dedupe_key", list(where.values()))


def keys(db, **where):
    return [r["dedupe_key"] for r in rows(db, **where)]


def by_key(db):
    return {r["dedupe_key"]: r for r in rows(db)}


def put(db, owner_id, key, value):
    """Planner-only settings are not editable via set_setting; write them directly."""
    db.execute(
        "INSERT INTO settings (owner_id, key, value, updated_at_utc) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(owner_id, key) DO UPDATE SET value = excluded.value",
        (owner_id, key, value, to_utc_str(NOW)),
    )


def set_rrule(db, ev, rrule):
    u = series_until_utc(rrule, ev.start, ev.end - ev.start)
    db.execute(
        "UPDATE events SET rrule = ?, until_utc = ? WHERE id = ?",
        (rrule, to_utc_str(u) if u else None, ev.id),
    )


def recurring(db, svc, rrule, start, *, tz=None, reminders=()):
    ev = svc.add_event("r", start, tz=tz, reminders=reminders)
    set_rrule(db, ev, rrule)
    return ev


# ---- constants / dedupe_key format ----------------------------------------------------------
def test_constants():
    assert MATERIALIZE_DAYS == 35
    assert STALE_DAYS == 3


def test_event_dedupe_keys_and_times(db, svc):
    ev = svc.add_event(
        "m",
        jst(2026, 11, 3, 15),
        reminders=[ReminderSpec(offset_min=10), ReminderSpec(at_local="20:00", day_offset=1)],
    )
    got = by_key(db)
    p = f"e:{ev.id}:2026-11-03T15:00:00"
    assert set(got) == {f"{p}:m10", f"{p}:a1@2000", f"{p}:start"}
    assert got[f"{p}:m10"]["scheduled_utc"] == "2026-11-03T05:50:00Z"
    assert got[f"{p}:m10"]["kind"] == "remind"
    assert got[f"{p}:a1@2000"]["scheduled_utc"] == "2026-11-02T11:00:00Z"
    assert got[f"{p}:start"]["kind"] == "start"
    assert got[f"{p}:start"]["scheduled_utc"] == "2026-11-03T06:00:00Z"
    for r in got.values():
        assert r["status"] == "pending" and r["owner_id"] == 1
        assert r["target_type"] == "event" and r["event_id"] == ev.id and r["todo_id"] is None
        assert r["occurrence_local"] == "2026-11-03T15:00:00"
        assert r["target_utc"] == "2026-11-03T06:00:00Z"
        assert r["fire_at_utc"] == r["scheduled_utc"]
    assert got[f"{p}:start"]["rule_id"] is None
    assert got[f"{p}:m10"]["rule_id"] is not None


def test_todo_dedupe_keys(db, svc):
    t = svc.add_todo("t", due=jst(2026, 11, 3, 18), reminders=[ReminderSpec(offset_min=30)])
    got = by_key(db)
    p = f"t:{t.id}:2026-11-03T18:00:00"
    assert set(got) == {f"{p}:m30", f"{p}:due"}
    assert got[f"{p}:due"]["kind"] == "due"
    assert got[f"{p}:due"]["scheduled_utc"] == "2026-11-03T09:00:00Z"
    assert got[f"{p}:m30"]["scheduled_utc"] == "2026-11-03T08:30:00Z"
    assert got[f"{p}:due"]["target_type"] == "todo" and got[f"{p}:due"]["event_id"] is None


def test_rule_content_change_makes_new_key(db, svc):
    ev = svc.add_event("m", jst(2026, 11, 3, 15), reminders=[ReminderSpec(offset_min=10)])
    svc.update_event(ev.id, reminders=[ReminderSpec(offset_min=20)])
    got = by_key(db)
    p = f"e:{ev.id}:2026-11-03T15:00:00"
    assert got[f"{p}:m10"]["status"] == "skipped"
    assert got[f"{p}:m10"]["skip_reason"] == "superseded"
    assert got[f"{p}:m20"]["status"] == "pending"


def test_no_title_or_note_in_notifications(db, svc):
    svc.add_event("SECRET-TITLE", jst(2026, 11, 3, 15), note="SECRET-NOTE")
    dump = repr([tuple(r) for r in rows(db)])
    assert "SECRET" not in dump


# ---- idempotence / plan_all -----------------------------------------------------------------
def test_plan_all_is_idempotent(db, svc):
    svc.add_event("a", jst(2026, 11, 3, 15), reminders=[ReminderSpec(offset_min=10)])
    svc.add_todo(
        "t", due=jst(2026, 11, 5, 9), reminders=[ReminderSpec(at_local="20:00", day_offset=1)]
    )
    recurring(db, svc, "FREQ=DAILY", jst(2026, 10, 30, 9))
    first = svc.plan_all()
    snap = [tuple(r) for r in rows(db)]
    again = svc.plan_all()
    assert again == PlanStats()
    assert [tuple(r) for r in rows(db)] == snap
    assert isinstance(first, PlanStats)
    # mode does not matter for an already planned set
    assert svc.plan_all(mode="initial") == PlanStats()
    assert [tuple(r) for r in rows(db)] == snap


def test_plan_all_fills_in_missing_rows_from_scratch(db, svc):
    ev = svc.add_event("a", jst(2026, 11, 3, 15), reminders=[ReminderSpec(offset_min=10)])
    t = svc.add_todo("t", due=jst(2026, 11, 5, 9))
    before = keys(db)
    db.execute("DELETE FROM notifications")
    stats = svc.plan_all()
    assert keys(db) == before
    assert stats.inserted == len(before)
    assert ev.id and t.id


def test_plan_all_other_owner_untouched(db, clock):
    mine = ScheduleService(db, 1, clock=clock)
    theirs = ScheduleService(db, 2, clock=clock)
    mine.add_event("a", jst(2026, 11, 3, 15))
    theirs.add_event("b", jst(2026, 11, 3, 16))
    db.execute("DELETE FROM notifications")
    mine.plan_all()
    assert {r["owner_id"] for r in rows(db)} == {1}
    # a pending row of another owner is not changed by my plan_all
    theirs.plan_all()
    before = [tuple(r) for r in rows(db, owner_id=2)]
    db.execute("UPDATE events SET dtstart_local='2026-11-03T17:00:00' WHERE owner_id=2")
    mine.plan_all()
    assert [tuple(r) for r in rows(db, owner_id=2)] == before


def test_unknown_ids_are_noop(db):
    with db.transaction() as conn:
        assert reconcile_event(conn, 999, NOW) == PlanStats()
        assert reconcile_todo(conn, 999, NOW) == PlanStats()
    assert rows(db) == []


def test_planstats_add():
    assert PlanStats(1, 2, 3, 4) + PlanStats(10, 20, 30, 40) == PlanStats(11, 22, 33, 44)


# ---- edits: pending updates, sent frozen ----------------------------------------------------
def test_time_edit_updates_pending_row_in_place(db, svc):
    ev = svc.add_event("a", jst(2026, 11, 3, 15), reminders=[ReminderSpec(offset_min=10)])
    ids = {r["dedupe_key"]: r["id"] for r in rows(db)}
    svc.update_event(ev.id, start=jst(2026, 11, 3, 16))
    got = by_key(db)
    p = f"e:{ev.id}:2026-11-03T15:00:00"
    # single event: occurrence_local follows the new dtstart, so the key changes
    old_start = got[f"{p}:start"]
    assert old_start["status"] == "skipped" and old_start["skip_reason"] == "superseded"
    new_p = f"e:{ev.id}:2026-11-03T16:00:00"
    assert got[f"{new_p}:start"]["status"] == "pending"
    assert got[f"{new_p}:start"]["scheduled_utc"] == "2026-11-03T07:00:00Z"
    assert got[f"{new_p}:m10"]["scheduled_utc"] == "2026-11-03T06:50:00Z"
    assert got[f"{new_p}:m10"]["id"] != ids[f"{p}:m10"]


def test_target_change_with_same_key_updates_pending_row(db, svc):
    """A recurring series keeps its key when dtstart_local moves to another series slot."""
    ev = recurring(db, svc, "FREQ=DAILY", jst(2026, 11, 3, 15))
    svc.plan_all()
    key = f"e:{ev.id}:2026-11-05T15:00:00:start"
    row = by_key(db)[key]
    # change the stored tz-derived values so scheduled differs while the key stays the same
    db.execute("UPDATE events SET tz='Asia/Kolkata' WHERE id=?", (ev.id,))
    with db.transaction() as conn:
        stats = reconcile_event(conn, ev.id, NOW)
    assert stats.updated > 0
    new = by_key(db)[key]
    assert new["id"] == row["id"] and new["status"] == "pending"
    assert new["scheduled_utc"] == "2026-11-05T09:30:00Z"
    assert new["fire_at_utc"] == new["scheduled_utc"]


def test_sending_sent_failed_are_never_touched(db, svc):
    ev = svc.add_event("a", jst(2026, 11, 3, 15), reminders=[ReminderSpec(offset_min=10)])
    p = f"e:{ev.id}:2026-11-03T15:00:00"
    for key, st in ((f"{p}:m10", "sent"), (f"{p}:start", "sending")):
        db.execute("UPDATE notifications SET status=? WHERE dedupe_key=?", (st, key))
    before = by_key(db)
    svc.delete_event(ev.id)
    after = by_key(db)
    assert after[f"{p}:m10"]["status"] == "sent"
    assert after[f"{p}:start"]["status"] == "sending"
    assert tuple(after[f"{p}:m10"]) == tuple(before[f"{p}:m10"])
    svc.restore_event(ev.id)
    for k, st in ((f"{p}:m10", "sent"), (f"{p}:start", "sending")):
        assert by_key(db)[k]["status"] == st
        assert by_key(db)[k]["updated_at_utc"] == before[k]["updated_at_utc"]


def test_sent_row_not_changed_on_time_edit_of_recurring(db, svc):
    ev = recurring(db, svc, "FREQ=DAILY", jst(2026, 11, 3, 15))
    svc.plan_all()
    key = f"e:{ev.id}:2026-11-05T15:00:00:start"
    db.execute("UPDATE notifications SET status='sent' WHERE dedupe_key=?", (key,))
    before = tuple(by_key(db)[key])
    db.execute("UPDATE events SET tz='Asia/Kolkata' WHERE id=?", (ev.id,))
    with db.transaction() as conn:
        reconcile_event(conn, ev.id, NOW)
    assert tuple(by_key(db)[key]) == before


def test_failed_row_untouched(db, svc):
    ev = svc.add_event("a", jst(2026, 11, 3, 15))
    key = f"e:{ev.id}:2026-11-03T15:00:00:start"
    db.execute("UPDATE notifications SET status='failed' WHERE dedupe_key=?", (key,))
    svc.delete_event(ev.id)
    svc.restore_event(ev.id)
    assert by_key(db)[key]["status"] == "failed"


# ---- delete / restore -----------------------------------------------------------------------
def test_delete_skips_pending_and_restore_revives_future(db, svc):
    ev = svc.add_event("a", jst(2026, 11, 3, 15), reminders=[ReminderSpec(offset_min=10)])
    n = len(rows(db))
    assert n == 2
    svc.delete_event(ev.id)
    for r in rows(db):
        assert r["status"] == "skipped" and r["skip_reason"] == "target_deleted"
    svc.restore_event(ev.id)
    assert {r["status"] for r in rows(db)} == {"pending"}
    assert len(rows(db)) == n
    assert all(r["skip_reason"] is None for r in rows(db))


def test_restore_revives_only_future_rows(db, clock):
    svc = ScheduleService(db, 1, clock=clock)
    ev = svc.add_event("a", jst(2026, 10, 30, 15), reminders=[ReminderSpec(offset_min=10)])
    svc.delete_event(ev.id)
    # time passes: the m10 reminder (14:50) is past, the start (15:00) is still future
    late = ScheduleService(db, 1, clock=FixedClock(jst(2026, 10, 30, 14, 55)))
    late.restore_event(ev.id)
    got = by_key(db)
    p = f"e:{ev.id}:2026-10-30T15:00:00"
    assert got[f"{p}:start"]["status"] == "pending"
    assert got[f"{p}:m10"]["status"] == "skipped"
    assert got[f"{p}:m10"]["skip_reason"] == "target_deleted"


def test_delete_cleans_even_rows_outside_window(db, svc):
    ev = svc.add_event("a", jst(2026, 11, 3, 15))
    db.execute("UPDATE notifications SET scheduled_utc='2000-01-01T00:00:00Z'")
    svc.delete_event(ev.id)
    assert rows(db)[0]["skip_reason"] == "target_deleted"


def test_non_revivable_skip_reason_stays_skipped(db, svc):
    ev = svc.add_event("a", jst(2026, 11, 3, 15))
    key = f"e:{ev.id}:2026-11-03T15:00:00:start"
    db.execute(
        "UPDATE notifications SET status='skipped', skip_reason='past_at_creation'"
        " WHERE dedupe_key=?",
        (key,),
    )
    svc.plan_all()
    row = by_key(db)[key]
    assert row["status"] == "skipped" and row["skip_reason"] == "past_at_creation"
    for reason in ("user_cancelled", "acked"):
        db.execute("UPDATE notifications SET skip_reason=? WHERE dedupe_key=?", (reason, key))
        svc.plan_all()
        assert by_key(db)[key]["skip_reason"] == reason
        assert by_key(db)[key]["status"] == "skipped"


def test_exception_skip_is_revived(db, svc):
    ev = svc.add_event("a", jst(2026, 11, 3, 15))
    key = f"e:{ev.id}:2026-11-03T15:00:00:start"
    db.execute(
        "UPDATE notifications SET status='skipped', skip_reason='exception_skip'"
        " WHERE dedupe_key=?",
        (key,),
    )
    stats = svc.plan_all()
    assert stats.revived == 1
    assert by_key(db)[key]["status"] == "pending"
    assert by_key(db)[key]["skip_reason"] is None


# ---- TODO -----------------------------------------------------------------------------------
def test_todo_complete_skips_and_reopen_revives(db, svc):
    t = svc.add_todo("t", due=jst(2026, 11, 3, 18), reminders=[ReminderSpec(offset_min=30)])
    svc.complete_todo(t.id)
    assert {(r["status"], r["skip_reason"]) for r in rows(db)} == {("skipped", "todo_done")}
    svc.reopen_todo(t.id)
    assert {(r["status"], r["skip_reason"]) for r in rows(db)} == {("pending", None)}
    assert len(rows(db)) == 2


def test_todo_delete_restore(db, svc):
    t = svc.add_todo("t", due=jst(2026, 11, 3, 18))
    svc.delete_todo(t.id)
    assert rows(db)[0]["skip_reason"] == "target_deleted"
    svc.restore_todo(t.id)
    assert rows(db)[0]["status"] == "pending"


def test_completed_todo_does_not_get_new_rows_from_plan_all(db, svc):
    t = svc.add_todo("t", due=jst(2026, 11, 3, 18))
    svc.complete_todo(t.id)
    db.execute("DELETE FROM notifications")
    svc.plan_all()
    assert rows(db) == []


def test_todo_due_change_uses_new_key(db, svc):
    t = svc.add_todo("t", due=jst(2026, 11, 3, 18))
    svc.update_todo(t.id, due=jst(2026, 11, 4, 18))
    got = by_key(db)
    assert got[f"t:{t.id}:2026-11-03T18:00:00:due"]["status"] == "skipped"
    assert got[f"t:{t.id}:2026-11-03T18:00:00:due"]["skip_reason"] == "superseded"
    assert got[f"t:{t.id}:2026-11-04T18:00:00:due"]["status"] == "pending"
    assert got[f"t:{t.id}:2026-11-04T18:00:00:due"]["scheduled_utc"] == "2026-11-04T09:00:00Z"


def test_todo_without_due_has_no_notifications(db, svc):
    svc.add_todo("t")
    svc.plan_all()
    assert rows(db) == []


def test_clearing_due_skips_pending(db, svc):
    t = svc.add_todo("t", due=jst(2026, 11, 3, 18))
    svc.update_todo(t.id, due=None)
    assert {(r["status"], r["skip_reason"]) for r in rows(db)} == {("skipped", "superseded")}
    assert t.id


def test_date_only_todo_due_uses_all_day_time(db, svc):
    t = svc.add_todo(
        "t",
        due=jst(2026, 11, 3),
        due_all_day=True,
        reminders=[ReminderSpec(at_local="20:00", day_offset=1)],
    )
    got = by_key(db)
    p = f"t:{t.id}:2026-11-03T00:00:00"
    assert set(got) == {f"{p}:a1@2000", f"{p}:due"}
    assert got[f"{p}:due"]["scheduled_utc"] == "2026-11-02T23:00:00Z"  # 08:00 JST
    assert got[f"{p}:a1@2000"]["scheduled_utc"] == "2026-11-02T11:00:00Z"  # 11/2 20:00 JST


# ---- all-day events -------------------------------------------------------------------------
def test_all_day_event_default_start_notification(db, svc):
    ev = svc.add_event("d", jst(2026, 11, 3), all_day=True)
    got = by_key(db)
    p = f"e:{ev.id}:2026-11-03T00:00:00"
    assert set(got) == {f"{p}:start"}
    assert got[f"{p}:start"]["scheduled_utc"] == "2026-11-02T23:00:00Z"  # 08:00 JST


def test_all_day_event_rules_at_local_and_offset_ignored(db, svc):
    ev = svc.add_event(
        "d",
        jst(2026, 11, 3),
        all_day=True,
        reminders=[
            ReminderSpec(offset_min=30),
            ReminderSpec(at_local="21:00", day_offset=2),
            ReminderSpec(at_local="07:30", day_offset=0),
        ],
    )
    got = by_key(db)
    p = f"e:{ev.id}:2026-11-03T00:00:00"
    assert set(got) == {f"{p}:a2@2100", f"{p}:a0@0730", f"{p}:start"}
    assert got[f"{p}:a2@2100"]["scheduled_utc"] == "2026-11-01T12:00:00Z"
    assert got[f"{p}:a0@0730"]["scheduled_utc"] == "2026-11-02T22:30:00Z"


def test_settings_all_day_time_and_toggles(db, svc, clock):
    put(db, 1, "all_day_notify_time", "06:30")
    ev = svc.add_event("d", jst(2026, 11, 3), all_day=True)
    assert rows(db)[0]["scheduled_utc"] == "2026-11-02T21:30:00Z"
    put(db, 1, "notify_at_start", "off")
    svc.plan_all()
    assert by_key(db)[f"e:{ev.id}:2026-11-03T00:00:00:start"]["skip_reason"] == "superseded"
    assert [r["status"] for r in rows(db)] == ["skipped"]


def test_settings_notify_at_due_off(db, svc):
    svc.add_todo("t", due=jst(2026, 11, 3, 18))
    assert len(rows(db)) == 1
    put(db, 1, "notify_at_due", "off")
    svc.plan_all()
    assert [r["status"] for r in rows(db)] == ["skipped"]
    put(db, 1, "notify_at_due", "on")
    stats = svc.plan_all()
    assert stats.revived == 1
    assert [r["status"] for r in rows(db)] == ["pending"]


def test_settings_notify_at_start_off_from_start(db, svc):
    put(db, 1, "notify_at_start", "off")
    svc.add_event("a", jst(2026, 11, 3, 15), reminders=[ReminderSpec(offset_min=10)])
    assert [r["kind"] for r in rows(db)] == ["remind"]


def test_settings_are_per_owner(db, clock):
    put(db, 2, "notify_at_start", "off")
    a = ScheduleService(db, 1, clock=clock)
    a.add_event("a", jst(2026, 11, 3, 15))
    assert len(rows(db)) == 1


def test_invalid_all_day_setting_falls_back_to_default(db, svc):
    db.execute(
        "INSERT INTO settings (owner_id, key, value, updated_at_utc) VALUES (1, ?, 'zz', ?)",
        ("all_day_notify_time", to_utc_str(NOW)),
    )
    svc.add_event("d", jst(2026, 11, 3), all_day=True)
    assert rows(db)[0]["scheduled_utc"] == "2026-11-02T23:00:00Z"


# ---- modes / past ---------------------------------------------------------------------------
def event_row_now(db, clock_now, mode, start, offset=None):
    d = ScheduleService(db, 1, clock=FixedClock(clock_now))
    ev = d.add_event("a", start, reminders=[ReminderSpec(offset_min=offset)] if offset else [])
    db.execute("DELETE FROM notifications")
    with db.transaction() as conn:
        reconcile_event(conn, ev.id, clock_now, mode=mode)
    return ev


@pytest.mark.parametrize("mode", ["edit", "initial"])
def test_past_is_skipped_at_creation_in_edit_and_initial(db, mode):
    event_row_now(db, NOW, mode, jst(2026, 10, 29, 10))
    r = rows(db)[0]
    assert (r["status"], r["skip_reason"]) == ("skipped", "past_at_creation")


def test_past_within_stale_days_is_pending_in_steady(db):
    event_row_now(db, NOW, "steady", jst(2026, 10, 27, 10))  # about 2 days ago
    r = rows(db)[0]
    assert (r["status"], r["skip_reason"]) == ("pending", None)


def test_past_beyond_stale_days_is_not_generated(db):
    event_row_now(db, NOW, "steady", jst(2026, 10, 25, 10))  # 4 days ago
    assert rows(db) == []


def test_service_plan_all_mode_steady_default_and_initial(db, svc):
    ev = svc.add_event("a", jst(2026, 10, 28, 10))  # past: skipped at creation via edit
    db.execute("DELETE FROM notifications")
    svc.plan_all(mode="initial")
    assert rows(db)[0]["skip_reason"] == "past_at_creation"
    db.execute("DELETE FROM notifications")
    svc.plan_all()
    assert rows(db)[0]["status"] == "pending"
    assert ev.id


def test_edit_moving_pending_row_into_past_marks_past_at_creation(db, svc):
    ev = recurring(db, svc, "FREQ=DAILY", jst(2026, 10, 30, 9))
    svc.plan_all()
    key = f"e:{ev.id}:2026-10-30T09:00:00:start"
    assert by_key(db)[key]["scheduled_utc"] == "2026-10-30T00:00:00Z"
    # the key stays the same while the time moves earlier (Kiritimati is UTC+14), and "now" is
    # already past the new time
    db.execute("UPDATE events SET tz='Pacific/Kiritimati' WHERE id=?", (ev.id,))
    later = jst(2026, 10, 30, 6)  # 10/29 21:00Z, after the new 10/29 19:00Z
    with db.transaction() as conn:
        reconcile_event(conn, ev.id, later, mode="edit")
    row = by_key(db)[key]
    assert row["scheduled_utc"] == "2026-10-29T19:00:00Z"
    assert (row["status"], row["skip_reason"]) == ("skipped", "past_at_creation")


# ---- recurring ------------------------------------------------------------------------------
def test_recurring_window_is_35_days_and_advances(db, svc):
    ev = recurring(db, svc, "FREQ=DAILY", jst(2026, 10, 30, 9))
    svc.plan_all()
    starts = sorted(
        r["occurrence_local"] for r in rows(db) if r["event_id"] == ev.id and r["kind"] == "start"
    )
    end = NOW + timedelta(days=MATERIALIZE_DAYS)
    assert starts[0] == "2026-10-30T09:00:00"
    assert all(datetime.fromisoformat(s).replace(tzinfo=JST) <= end for s in starts)
    last = datetime.fromisoformat(starts[-1])
    assert last.date() == end.date()  # 12/03 09:00 <= 12/03 12:00
    assert len(starts) == 35
    # beyond the window: not generated
    assert not any(s > "2026-12-03T09:00:00" for s in starts)

    later = ScheduleService(db, 1, clock=FixedClock(NOW + timedelta(days=10)))
    stats = later.plan_all()
    assert stats.inserted == 10
    starts2 = sorted(r["occurrence_local"] for r in rows(db) if r["kind"] == "start")
    assert starts2[-1] == "2026-12-13T09:00:00"


def test_recurring_count_and_until_fit_in_window(db, svc):
    c = recurring(db, svc, "FREQ=DAILY;COUNT=3", jst(2026, 10, 30, 9))
    u = recurring(db, svc, "FREQ=DAILY;UNTIL=20261102T000000Z", jst(2026, 10, 30, 9), reminders=[])
    svc.plan_all()
    cs = [r for r in rows(db, event_id=c.id) if r["kind"] == "start"]
    us = [r for r in rows(db, event_id=u.id) if r["kind"] == "start"]
    assert [r["occurrence_local"][:10] for r in cs] == ["2026-10-30", "2026-10-31", "2026-11-01"]
    assert all(r["occurrence_local"] <= "2026-11-02T09:00:00" for r in us)
    assert 2 <= len(us) <= 4


def test_recurring_occurrence_local_is_rule_value_single_is_dtstart(db, svc):
    single = svc.add_event("s", jst(2026, 11, 3, 15))
    rec = recurring(db, svc, "FREQ=WEEKLY", jst(2026, 11, 3, 15))
    svc.plan_all()
    assert {r["occurrence_local"] for r in rows(db, event_id=single.id)} == {"2026-11-03T15:00:00"}
    occ = sorted(r["occurrence_local"] for r in rows(db, event_id=rec.id))
    assert occ[:3] == ["2026-11-03T15:00:00", "2026-11-10T15:00:00", "2026-11-17T15:00:00"]


def test_recurring_reminder_before_window_edge_included(db, svc):
    """An occurrence just beyond the window end whose reminder falls inside it is planned."""
    ev = recurring(
        db,
        svc,
        "FREQ=DAILY;COUNT=1",
        jst(2026, 12, 3, 13),  # 35 days + 1h after NOW
        reminders=[ReminderSpec(at_local="12:00", day_offset=1)],
    )
    svc.plan_all()
    got = by_key(db)
    p = f"e:{ev.id}:2026-12-03T13:00:00"
    assert f"{p}:a1@1200" in got  # 12/02 12:00 JST is inside
    assert f"{p}:start" not in got  # 12/03 13:00 is outside


def test_dst_recurring_keeps_wall_clock(db, svc):
    ev = recurring(
        db, svc, "FREQ=DAILY", datetime(2026, 10, 30, 9, 0, tzinfo=NY), tz="America/New_York"
    )
    clock_now = datetime(2026, 10, 30, 0, 0, tzinfo=NY)
    ScheduleService(db, 1, clock=FixedClock(clock_now)).plan_all()
    got = {
        r["occurrence_local"]: r["scheduled_utc"]
        for r in rows(db, event_id=ev.id)
        if r["kind"] == "start"
    }
    # DST ends 2026-11-01 in the US: 09:00 stays 09:00 local, UTC offset shifts by 1h
    assert got["2026-10-31T09:00:00"] == "2026-10-31T13:00:00Z"
    assert got["2026-11-01T09:00:00"] == "2026-11-01T14:00:00Z"
    assert got["2026-11-02T09:00:00"] == "2026-11-02T14:00:00Z"


def test_dst_at_local_rule_keeps_wall_clock(db, svc):
    ev = recurring(
        db,
        svc,
        "FREQ=DAILY",
        datetime(2026, 10, 30, 9, 0, tzinfo=NY),
        tz="America/New_York",
        reminders=[ReminderSpec(at_local="20:00", day_offset=1)],
    )
    ScheduleService(db, 1, clock=FixedClock(datetime(2026, 10, 30, 0, 0, tzinfo=NY))).plan_all()
    got = by_key(db)
    # reminder for the 11/02 09:00 occurrence: 11/01 20:00 NY (EST) = 11/02 01:00Z
    assert got[f"e:{ev.id}:2026-11-02T09:00:00:a1@2000"]["scheduled_utc"] == "2026-11-02T01:00:00Z"
    # for 11/01 09:00: 10/31 20:00 NY (EDT) = 11/01 00:00Z
    assert got[f"e:{ev.id}:2026-11-01T09:00:00:a1@2000"]["scheduled_utc"] == "2026-11-01T00:00:00Z"


# ---- invalid rrule / robustness -------------------------------------------------------------
def test_invalid_rrule_keeps_existing_rows_and_is_skipped(db, svc):
    ev = recurring(db, svc, "FREQ=DAILY", jst(2026, 10, 30, 9))
    other = svc.add_event("ok", jst(2026, 11, 3, 15))
    svc.plan_all()
    before = [tuple(r) for r in rows(db, event_id=ev.id)]
    assert before
    db.execute("UPDATE events SET rrule='FREQ=NOPE;;' WHERE id=?", (ev.id,))
    with db.transaction() as conn:
        assert reconcile_event(conn, ev.id, NOW) == PlanStats()
    assert [tuple(r) for r in rows(db, event_id=ev.id)] == before
    # plan_all must not raise and still handles the healthy event
    db.execute("DELETE FROM notifications WHERE event_id=?", (other.id,))
    stats = svc.plan_all()
    assert stats.inserted == 1
    assert rows(db, event_id=other.id)
    assert [tuple(r) for r in rows(db, event_id=ev.id)] == before


def test_invalid_rrule_event_without_rows_creates_nothing(db, svc):
    ev = svc.add_event("x", jst(2026, 11, 3, 15))
    db.execute("DELETE FROM notifications")
    db.execute("UPDATE events SET rrule='garbage' WHERE id=?", (ev.id,))
    svc.plan_all()
    assert rows(db) == []


def test_deleted_event_with_invalid_rrule_is_still_cleaned(db, svc):
    ev = svc.add_event("x", jst(2026, 11, 3, 15))
    db.execute(
        "UPDATE events SET rrule='garbage', deleted_at_utc=? WHERE id=?", (NOW.isoformat(), ev.id)
    )
    svc.plan_all()
    assert {r["skip_reason"] for r in rows(db)} == {"target_deleted"}


def test_plan_all_skips_events_ended_before_window(db, svc):
    ev = svc.add_event("old", jst(2026, 9, 1, 9))
    db.execute("DELETE FROM notifications")
    svc.plan_all()
    assert rows(db, event_id=ev.id) == []


def test_other_owner_rows_untouched_by_reconcile_of_my_event(db, clock):
    a = ScheduleService(db, 1, clock=clock)
    b = ScheduleService(db, 2, clock=clock)
    a.add_event("a", jst(2026, 11, 3, 15))
    eb = b.add_event("b", jst(2026, 11, 3, 15))
    before = [tuple(r) for r in rows(db, owner_id=2)]
    a.delete_event(1)
    assert [tuple(r) for r in rows(db, owner_id=2)] == before
    assert eb.id


# ---- transactions ---------------------------------------------------------------------------
def test_reconcile_runs_inside_service_transaction_and_rolls_back(db, svc, monkeypatch):
    ev = svc.add_event("a", jst(2026, 11, 3, 15), reminders=[ReminderSpec(offset_min=10)])
    before = [tuple(r) for r in rows(db)]
    ev_before = db.query_one("SELECT * FROM events WHERE id=?", (ev.id,))

    real = planner.reconcile_event

    def boom(conn, event_id, now, **kw):
        real(conn, event_id, now, **kw)  # writes happen first ...
        raise RuntimeError("planner failure")

    monkeypatch.setattr(planner, "reconcile_event", boom)
    with pytest.raises(RuntimeError):
        svc.update_event(ev.id, title="new", start=jst(2026, 11, 4, 9))
    monkeypatch.undo()
    # ... but the whole edit (event row, rules, notifications) is rolled back
    assert [tuple(r) for r in rows(db)] == before
    assert tuple(db.query_one("SELECT * FROM events WHERE id=?", (ev.id,))) == tuple(ev_before)
    assert not db.connection.in_transaction


def test_todo_failure_rolls_back(db, svc, monkeypatch):
    t = svc.add_todo("t", due=jst(2026, 11, 3, 18))
    before = [tuple(r) for r in rows(db)]

    def boom(conn, todo_id, now, **kw):
        raise RuntimeError("x")

    monkeypatch.setattr(planner, "reconcile_todo", boom)
    with pytest.raises(RuntimeError):
        svc.complete_todo(t.id)
    monkeypatch.undo()
    assert svc.get_todo(t.id).done_at is None
    assert [tuple(r) for r in rows(db)] == before


def test_add_event_failure_leaves_nothing(db, svc, monkeypatch):
    def boom(conn, event_id, now, **kw):
        raise RuntimeError("x")

    monkeypatch.setattr(planner, "reconcile_event", boom)
    with pytest.raises(RuntimeError):
        svc.add_event("a", jst(2026, 11, 3, 15))
    monkeypatch.undo()
    assert db.query("SELECT * FROM events") == []
    assert rows(db) == []


def test_plan_all_one_transaction_per_target(db, svc, monkeypatch):
    a = svc.add_event("a", jst(2026, 11, 3, 15))
    b = svc.add_event("b", jst(2026, 11, 4, 15))
    db.execute("DELETE FROM notifications")
    real = planner.reconcile_event

    def flaky(conn, event_id, now, **kw):
        stats = real(conn, event_id, now, **kw)
        if event_id == b.id:
            raise RuntimeError("second target fails")
        return stats

    monkeypatch.setattr(planner, "reconcile_event", flaky)
    with pytest.raises(RuntimeError):
        svc.plan_all()
    monkeypatch.undo()
    assert {r["event_id"] for r in rows(db)} == {a.id}  # first target committed, second rolled back
