from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from nomusyo.schedule.digest import (
    DIGEST_GRACE,
    KEY_DIGEST_TIME,
    KEY_NOTIFY_CHANNEL,
    KEY_SKIP_EMPTY,
    digest_timing,
    load_digest_settings,
    parse_bool,
    parse_hhmm,
    should_skip_empty,
)
from nomusyo.storage import Database

JST = ZoneInfo("Asia/Tokyo")
T = time(6, 55)


def jst(*a):
    return datetime(*a, tzinfo=JST)


@pytest.mark.parametrize(
    ("now", "state"),
    [
        (jst(2026, 10, 29, 6, 54, 59), "wait"),
        (jst(2026, 10, 29, 0, 0), "wait"),
        (jst(2026, 10, 29, 6, 55), "due"),
        (jst(2026, 10, 29, 8, 0), "due"),
        (jst(2026, 10, 29, 9, 55), "due"),
        (jst(2026, 10, 29, 9, 55, 1), "expired"),
        (jst(2026, 10, 29, 23, 59), "expired"),
    ],
)
def test_digest_timing_states(now, state):
    t = digest_timing(now, T, "Asia/Tokyo")
    assert t.state == state
    assert t.run_date == "2026-10-29"
    assert t.scheduled == jst(2026, 10, 29, 6, 55)


def test_digest_timing_utc_input_crosses_date_boundary():
    # 2026-10-28 21:55 UTC == 2026-10-29 06:55 JST
    t = digest_timing(datetime(2026, 10, 28, 21, 55, tzinfo=UTC), T, "Asia/Tokyo")
    assert (t.state, t.run_date) == ("due", "2026-10-29")
    t = digest_timing(datetime(2026, 10, 28, 21, 54, tzinfo=UTC), T, "Asia/Tokyo")
    assert (t.state, t.run_date) == ("wait", "2026-10-29")
    # same instant read in UTC date would be 10-28; the key must stay local
    t = digest_timing(datetime(2026, 10, 28, 15, 0, tzinfo=UTC), T, "Asia/Tokyo")
    assert t.run_date == "2026-10-29"


def test_digest_timing_other_tz_and_custom_grace():
    now = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)  # 08:00 in New York (EDT? Oct 29 -> EDT)
    t = digest_timing(now, T, "America/New_York")
    assert t.run_date == "2026-10-29" and t.state == "due"
    t = digest_timing(now, T, "America/New_York", grace=timedelta(minutes=30))
    assert t.state == "expired"
    assert DIGEST_GRACE == timedelta(hours=3)


def test_should_skip_empty():
    assert should_skip_empty(True, True)
    assert not should_skip_empty(True, False)
    assert not should_skip_empty(False, True)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("06:55", time(6, 55)),
        (" 7:05 ", time(7, 5)),
        ("00:00", time(0, 0)),
        ("23:59", time(23, 59)),
        (None, None),
        ("", None),
        ("24:00", None),
        ("12:60", None),
        ("0655", None),
        ("6:5:1", None),
        ("ab:cd", None),
        ("-1:10", None),
        ("１２:３０", None),
        ("7:5", None),
        ("07:5", None),
    ],
)
def test_parse_hhmm(raw, expected):
    assert parse_hhmm(raw) == expected


@pytest.mark.parametrize(
    ("raw", "default", "expected"),
    [
        ("true", False, True),
        ("ON", False, True),
        (" 1 ", False, True),
        ("yes", False, True),
        ("false", True, False),
        ("Off", True, False),
        ("0", True, False),
        ("no", True, False),
        (None, True, True),
        (None, False, False),
        ("maybe", True, True),
        ("", False, False),
    ],
)
def test_parse_bool(raw, default, expected):
    assert parse_bool(raw, default) is expected


@pytest.fixture
def db(tmp_path):
    d = Database.open(tmp_path / "t.db")
    yield d
    d.close()


def put(db, key, value, owner=1):
    db.execute(
        "INSERT OR REPLACE INTO settings (owner_id, key, value, updated_at_utc) VALUES (?,?,?,?)",
        (owner, key, value, "2026-10-29 00:00:00"),
    )


def load(db):
    return load_digest_settings(db, 1, default_time=T, default_channel_id=555)


def test_load_settings_defaults(db):
    s = load(db)
    assert (s.digest_time, s.skip_empty, s.channel_id) == (T, False, 555)


def test_load_settings_stored_values(db):
    put(db, KEY_DIGEST_TIME, "08:30")
    put(db, KEY_SKIP_EMPTY, "true")
    put(db, KEY_NOTIFY_CHANNEL, "777")
    s = load(db)
    assert (s.digest_time, s.skip_empty, s.channel_id) == (time(8, 30), True, 777)


@pytest.mark.parametrize("bad", ["25:00", "x", "", "9"])
def test_load_settings_invalid_time_falls_back(db, bad):
    put(db, KEY_DIGEST_TIME, bad)
    assert load(db).digest_time == T


def test_load_settings_invalid_bool_is_false(db):
    put(db, KEY_SKIP_EMPTY, "perhaps")
    assert load(db).skip_empty is False


@pytest.mark.parametrize("bad", ["abc", "", "0", "-5", "12.5", "１２"])
def test_load_settings_invalid_channel_falls_back(db, bad):
    put(db, KEY_NOTIFY_CHANNEL, bad)
    assert load(db).channel_id == 555


def test_load_settings_scoped_to_owner(db):
    put(db, KEY_NOTIFY_CHANNEL, "777", owner=2)
    assert load(db).channel_id == 555


def test_late_digest_time_evaluates_previous_day_after_midnight():
    late = time(22, 30)
    t = digest_timing(jst(2026, 10, 30, 1, 0), late, "Asia/Tokyo")
    assert (t.state, t.run_date) == ("due", "2026-10-29")
    assert t.scheduled == jst(2026, 10, 29, 22, 30)
    # grace (3h) ends at 01:30, inclusive
    assert digest_timing(jst(2026, 10, 30, 1, 30), late, "Asia/Tokyo").state == "due"
    t = digest_timing(jst(2026, 10, 30, 1, 30, 1), late, "Asia/Tokyo")
    assert (t.state, t.run_date) == ("wait", "2026-10-30")


def test_early_digest_time_waits_at_05_00():
    t = digest_timing(jst(2026, 10, 29, 5, 0), time(7, 0), "Asia/Tokyo")
    assert (t.state, t.run_date) == ("wait", "2026-10-29")
