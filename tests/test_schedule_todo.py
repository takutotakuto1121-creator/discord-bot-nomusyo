from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from nomusyo.schedule.errors import PastDateError, ScheduleError, ValidationError
from nomusyo.schedule.todo import build_todo_input

JST = ZoneInfo("Asia/Tokyo")
NOW = datetime(2026, 10, 29, 12, 0, tzinfo=JST)


def build(**kw):
    kw.setdefault("title", "牛乳を買う")
    return build_todo_input(now=NOW, tz="Asia/Tokyo", **kw)


def test_no_due():
    t = build()
    assert t.title == "牛乳を買う"
    assert t.due is None and t.due_all_day is False
    assert t.reminders == () and t.priority == 0 and t.note is None
    assert t.tz == "Asia/Tokyo"


def test_with_date_only_due_is_all_day():
    t = build(due="2026-11-03")
    assert t.due == datetime(2026, 11, 3, tzinfo=JST)
    assert t.due_all_day is True


def test_with_timed_due_and_reminder():
    t = build(due="2026-11-03 18:00", remind="1h", note="  メモ  ", priority=2)
    assert t.due == datetime(2026, 11, 3, 18, 0, tzinfo=JST)
    assert t.due_all_day is False
    assert len(t.reminders) == 1
    assert t.note == "メモ" and t.priority == 2


def test_blank_due_treated_as_none():
    assert build(due="   ").due is None
    assert build(note="  ").note is None


def test_fullwidth_input():
    t = build(due="２０２６-１１-０３ １８：００", title="　全角　")
    assert t.due == datetime(2026, 11, 3, 18, 0, tzinfo=JST)
    assert t.title == "全角"


def test_remind_without_due_is_error():
    with pytest.raises(ValidationError, match="期限が必要"):
        build(remind="1h")


def test_past_due_rejected():
    with pytest.raises(PastDateError):
        build(due="2026-10-29 11:00")
    with pytest.raises(PastDateError):
        build(due="2026-10-28")


def test_today_all_day_allowed():
    assert build(due="今日").due_all_day is True


def test_bad_due_format():
    with pytest.raises(ScheduleError):
        build(due="いつか")


@pytest.mark.parametrize("p", [-1, 4, 100])
def test_priority_out_of_range(p):
    with pytest.raises(ValidationError, match="優先度"):
        build(priority=p)


@pytest.mark.parametrize("p", [0, 1, 2, 3])
def test_priority_in_range(p):
    assert build(priority=p).priority == p


@pytest.mark.parametrize("title", ["", "   ", "　"])
def test_empty_title(title):
    with pytest.raises(ValidationError, match="タイトル"):
        build(title=title)


def test_title_length_boundary():
    assert len(build(title="あ" * 200).title) == 200
    with pytest.raises(ValidationError, match="200"):
        build(title="あ" * 201)


def test_note_length_boundary():
    assert len(build(note="x" * 2000).note) == 2000
    with pytest.raises(ValidationError, match="2000"):
        build(note="x" * 2001)


def test_error_does_not_echo_input():
    with pytest.raises(ScheduleError) as ei:
        build(title="SECRET" * 100)
    assert "SECRET" not in str(ei.value)


def test_invalid_tz():
    with pytest.raises(ScheduleError):
        build_todo_input(title="a", now=NOW, tz="Nowhere/Land", due="2026-11-03")


def test_date_only_due_rejects_offset_rule():
    with pytest.raises(ValidationError):
        build(due="2026-11-03", remind="1h")


def test_date_only_due_allows_at_local_rule():
    t = build(due="2026-11-03", remind="1d@20:00")
    assert t.due_all_day is True and len(t.reminders) == 1
