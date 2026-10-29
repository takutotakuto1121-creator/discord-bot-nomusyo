from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from nomusyo.schedule.errors import ParseError, PastDateError, ScheduleError
from nomusyo.schedule.models import ReminderSpec
from nomusyo.schedule.parsing import (
    MAX_REMINDERS,
    parse_duration,
    parse_hhmm,
    parse_reminders,
    parse_when,
)

JST = ZoneInfo("Asia/Tokyo")
NOW = datetime(2026, 10, 29, 12, 0, tzinfo=JST)


def jst(*a):
    return datetime(*a, tzinfo=JST)


def pw(text, now=NOW, **kw):
    return parse_when(text, now=now, tz=JST, **kw)


def test_today_is_all_day():
    r = pw("今日")
    assert r.all_day and r.start == jst(2026, 10, 29)


def test_tomorrow_with_time():
    r = pw("明日 9:00")
    assert not r.all_day and r.start == jst(2026, 10, 30, 9, 0)


def test_day_after_tomorrow():
    assert pw("明後日").start == jst(2026, 10, 31)


def test_full_date_time():
    r = pw("2026-11-03 15:00")
    assert r.start == jst(2026, 11, 3, 15) and not r.all_day


def test_full_date_slash_and_all_day():
    r = pw("2026/11/3")
    assert r.all_day and r.start == jst(2026, 11, 3)


def test_md_with_time_uses_current_year():
    assert pw("11/3 15:00").start == jst(2026, 11, 3, 15)


def test_md_past_rolls_to_next_year():
    r = pw("10/1")
    assert r.start == jst(2027, 10, 1) and r.all_day
    assert pw("1/5 10:00").start == jst(2027, 1, 5, 10)


def test_md_today_stays_this_year():
    assert pw("10/29").start == jst(2026, 10, 29)


def test_md_today_earlier_time_is_past_not_rolled():
    # date is today so no year rollover; the timed value is in the past
    with pytest.raises(PastDateError):
        pw("10/29 9:00")


def test_md_allow_past_does_not_roll():
    assert pw("10/1", allow_past=True).start == jst(2026, 10, 1)


def test_leap_day():
    assert pw("2/29").start == jst(2028, 2, 29)
    assert pw("2028-02-29 10:00").start == jst(2028, 2, 29, 10)
    with pytest.raises(ParseError):
        pw("2027-02-29")
    with pytest.raises(ParseError):
        pw("2/30")


def test_year_end_and_new_year():
    now = jst(2026, 12, 31, 23, 0)
    assert pw("12/31 23:30", now=now).start == jst(2026, 12, 31, 23, 30)
    assert pw("1/1", now=now).start == jst(2027, 1, 1)
    assert pw("明日", now=now).start == jst(2027, 1, 1)
    assert pw("明後日 0:00", now=now).start == jst(2027, 1, 2)


def test_relative_uses_local_date_not_utc():
    # 2026-10-29 20:00 UTC is already 10-30 05:00 in JST
    now = datetime(2026, 10, 29, 20, 0, tzinfo=ZoneInfo("UTC"))
    assert pw("今日", now=now).start == jst(2026, 10, 30)


def test_fullwidth_normalized():
    assert pw("２０２６－１１－０３　１５：００").start == jst(2026, 11, 3, 15)
    assert pw("明日　９：００").start == jst(2026, 10, 30, 9)


def test_whitespace_trimmed():
    assert pw("  明日  ").all_day


def test_past_rejected():
    with pytest.raises(PastDateError):
        pw("2026-10-28")
    with pytest.raises(PastDateError):
        pw("2026-10-29 11:59")
    with pytest.raises(PastDateError):
        pw("今日 9:00")


def test_now_exactly_is_allowed():
    assert pw("2026-10-29 12:00").start == NOW


def test_past_allowed_flag():
    assert pw("2020-01-01 10:00", allow_past=True).start == jst(2020, 1, 1, 10)
    assert pw("昨日".replace("昨日", "2020-01-01"), allow_past=True).all_day


def test_today_all_day_is_not_past():
    assert pw("2026-10-29").all_day


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "あした",
        "来週",
        "13/1",
        "0/5",
        "2026-13-01",
        "2026-11-31",
        "2026-11-03 25:00",
        "2026-11-03 10:60",
        "明日 24:00",
        "11/3 9",
        "2026-11-03T10:00",
        "'; DROP TABLE events; --",
    ],
)
def test_invalid_inputs(bad):
    with pytest.raises(ParseError) as ei:
        pw(bad)
    assert isinstance(ei.value, ScheduleError)
    assert isinstance(ei.value, ValueError)
    msg = str(ei.value)
    assert msg and (not bad.strip() or bad not in msg)


def test_naive_now_rejected():
    from nomusyo.schedule.errors import ValidationError

    with pytest.raises(ValidationError):
        parse_when("今日", now=datetime(2026, 1, 1), tz=JST)


def test_parse_hhmm():
    assert parse_hhmm("9:05").minute == 5
    assert parse_hhmm("２３：５９").hour == 23
    for bad in ("24:00", "9:60", "9", "ab", "", "9:5"):
        with pytest.raises(ParseError):
            parse_hhmm(bad)


@pytest.mark.parametrize(
    ("text", "minutes"),
    [
        ("90m", 90),
        ("2h", 120),
        ("1h30m", 90),
        ("1d", 1440),
        ("1d2h", 1560),
        ("31d", 31 * 1440),
        ("1M", 1),
        (" 45m ", 45),
    ],
)
def test_duration_ok(text, minutes):
    assert parse_duration(text) == minutes


@pytest.mark.parametrize("bad", ["", "0m", "0h0m", "32d", "abc", "1.5h", "-5m", "m", "30m1h", "1w"])
def test_duration_bad(bad):
    with pytest.raises(ParseError):
        parse_duration(bad)


def test_reminders_basic():
    assert parse_reminders("10m,1h,1d@20:00") == (
        ReminderSpec(offset_min=10),
        ReminderSpec(offset_min=60),
        ReminderSpec(at_local="20:00", day_offset=1),
    )


def test_reminders_day_is_1440():
    assert parse_reminders("1d") == (ReminderSpec(offset_min=1440),)


def test_reminders_same_day_at_time():
    assert parse_reminders("0d@8:00") == (ReminderSpec(at_local="08:00", day_offset=0),)


def test_reminders_empty_and_duplicates():
    assert parse_reminders("") == ()
    assert parse_reminders("  ") == ()
    assert parse_reminders("10m, 10m ,10M") == (ReminderSpec(offset_min=10),)
    assert parse_reminders("1h,60m") == (ReminderSpec(offset_min=60),)


@pytest.mark.parametrize("bad", ["1h@20:00", "10", "m", "10x", "1d@25:00", "1d@", "10m,,5m", "-5m"])
def test_reminders_bad(bad):
    with pytest.raises(ParseError):
        parse_reminders(bad)


def test_reminders_limit():
    ok = ",".join(f"{i}m" for i in range(1, MAX_REMINDERS + 1))
    assert len(parse_reminders(ok)) == MAX_REMINDERS
    too_many = ",".join(f"{i}m" for i in range(1, MAX_REMINDERS + 2))
    with pytest.raises(ParseError):
        parse_reminders(too_many)


def test_reminders_too_far():
    with pytest.raises(ParseError):
        parse_reminders("32d")
    assert parse_reminders("31d")


def test_day_offset_upper_bound_32_rejected_31_ok():
    with pytest.raises(ParseError):
        parse_reminders("32d@10:00")
    assert parse_reminders("31d@10:00") == (ReminderSpec(at_local="10:00", day_offset=31),)
