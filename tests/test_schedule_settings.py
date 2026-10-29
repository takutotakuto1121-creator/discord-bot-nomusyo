import asyncio
from datetime import datetime, time, timedelta
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import discord
import pytest

from nomusyo.bot.cogs.digest import DigestCog
from nomusyo.bot.config import BotConfig
from nomusyo.schedule.clock import FixedClock
from nomusyo.schedule.digest import (
    KEY_DIGEST_TIME,
    KEY_NOTIFY_CHANNEL,
    KEY_SKIP_EMPTY,
)
from nomusyo.schedule.service import ScheduleService
from nomusyo.schedule.settings import (
    DEFAULT_REMIND,
    EDITABLE_KEYS,
    KEY_DEFAULT_REMIND,
    KEY_DEFAULT_REMIND_TODO,
    KEY_NOTIFY_DM,
    SettingsError,
    load_user_settings,
    normalize_value,
    set_setting,
)
from nomusyo.storage import Database

JST = ZoneInfo("Asia/Tokyo")
OWNER = 1
ENV_CHANNEL = 111
ENV_TIME = time(6, 55)
NOW = datetime(2026, 10, 29, 7, 0, tzinfo=JST)
LATER = NOW + timedelta(hours=1)


@pytest.fixture
def db(tmp_path):
    d = Database.open(tmp_path / "t.db")
    yield d
    d.close()


def rows(db):
    return db.query("SELECT * FROM settings ORDER BY key", ())


def load(db):
    return load_user_settings(db, OWNER, default_time=ENV_TIME, default_channel_id=ENV_CHANNEL)


# -- normalize: ok -------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [("06:05", "06:05"), (" 23:59 ", "23:59"), ("00:00", "00:00"), ("7:00", "07:00")],
)
def test_time_ok(raw, expected):
    assert normalize_value(KEY_DIGEST_TIME, raw) == expected


@pytest.mark.parametrize("raw", ["6:5", "６:５"])
def test_time_single_digit_minute_rejected(raw):
    with pytest.raises(SettingsError):
        normalize_value(KEY_DIGEST_TIME, raw)


def test_fullwidth_channel_id_is_nfkc_normalized(db):
    assert normalize_value(KEY_NOTIFY_CHANNEL, "１２３") == "123"
    assert set_setting(db, OWNER, KEY_NOTIFY_CHANNEL, "１２３", NOW) == "123"
    assert rows(db)[0]["value"] == "123"
    assert load(db).channel_id == 123


def test_get_setting_defined_in_settings_module(db):
    from nomusyo.schedule.settings import get_setting

    assert get_setting(db, OWNER, KEY_DIGEST_TIME) is None
    set_setting(db, OWNER, KEY_DIGEST_TIME, "05:30", NOW)
    assert get_setting(db, OWNER, KEY_DIGEST_TIME) == "05:30"


@pytest.mark.parametrize("key", [KEY_SKIP_EMPTY, KEY_NOTIFY_DM])
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", "true"),
        ("true", "true"),
        ("YES", "true"),
        ("On", "true"),
        ("0", "false"),
        ("False", "false"),
        ("no", "false"),
        ("OFF", "false"),
    ],
)
def test_bool_aliases(key, raw, expected):
    assert normalize_value(key, raw) == expected


@pytest.mark.parametrize("key", [KEY_DEFAULT_REMIND, KEY_DEFAULT_REMIND_TODO])
def test_remind_normalized_and_empty_allowed(key):
    assert normalize_value(key, "10M") == "10m"
    assert normalize_value(key, "") == ""
    assert normalize_value(key, "   ") == ""


def test_empty_remind_is_stored(db):
    assert set_setting(db, OWNER, KEY_DEFAULT_REMIND, "", NOW) == ""
    assert [r["value"] for r in rows(db)] == [""]
    # stored empty overrides the code default
    assert load(db).default_remind == ""


def test_channel_normalized():
    assert normalize_value(KEY_NOTIFY_CHANNEL, " 0042 ") == "42"


# -- normalize: errors ---------------------------------------------------------------------
BAD = [
    (KEY_DIGEST_TIME, "24:00"),
    (KEY_DIGEST_TIME, "12:60"),
    (KEY_DIGEST_TIME, "-1:00"),
    (KEY_DIGEST_TIME, "abc"),
    (KEY_DIGEST_TIME, "0600"),
    (KEY_DIGEST_TIME, ""),
    (KEY_NOTIFY_CHANNEL, "abc"),
    (KEY_NOTIFY_CHANNEL, "0"),
    (KEY_NOTIFY_CHANNEL, "-5"),
    (KEY_NOTIFY_CHANNEL, ""),
    (KEY_NOTIFY_CHANNEL, "12.5"),
    (KEY_NOTIFY_CHANNEL, "１２３４x"),
    (KEY_NOTIFY_CHANNEL, "０"),
    (KEY_NOTIFY_CHANNEL, "1" * 21),
    (KEY_SKIP_EMPTY, "maybe"),
    (KEY_NOTIFY_DM, ""),
    (KEY_DEFAULT_REMIND, "banana"),
    (KEY_DEFAULT_REMIND, "10x"),
    (KEY_DEFAULT_REMIND, "10m," + "1h," * 40),
    (KEY_DEFAULT_REMIND_TODO, "a" * 101),
    ("owner_id", "1"),
    ("unknown", "x"),
    ("", "x"),
]


@pytest.mark.parametrize(("key", "raw"), BAD)
def test_invalid_raises_and_writes_nothing(db, key, raw):
    with pytest.raises(SettingsError):
        normalize_value(key, raw)
    with pytest.raises(SettingsError):
        set_setting(db, OWNER, key, raw, NOW)
    assert rows(db) == []


@pytest.mark.parametrize(("key", "raw"), BAD)
def test_invalid_keeps_existing_value(db, key, raw):
    if key in EDITABLE_KEYS:
        good = {
            KEY_DIGEST_TIME: "05:00",
            KEY_NOTIFY_CHANNEL: "7",
            KEY_SKIP_EMPTY: "true",
            KEY_NOTIFY_DM: "true",
            KEY_DEFAULT_REMIND: "5m",
            KEY_DEFAULT_REMIND_TODO: "1h",
        }[key]
        set_setting(db, OWNER, key, good, NOW)
    before = rows(db)
    with pytest.raises(SettingsError):
        set_setting(db, OWNER, key, raw, LATER)
    assert rows(db) == before


def test_exactly_100_chars_not_rejected_for_length():
    # 101 is the boundary: error message is the length one, not a parse one
    with pytest.raises(SettingsError, match="長すぎ"):
        normalize_value(KEY_DEFAULT_REMIND, "1" * 101)


# -- upsert / priority ---------------------------------------------------------------------
def test_upsert_updates_value_and_timestamp(db):
    set_setting(db, OWNER, KEY_DIGEST_TIME, "06:00", NOW)
    first = rows(db)
    assert len(first) == 1 and first[0]["value"] == "06:00"
    assert set_setting(db, OWNER, KEY_DIGEST_TIME, "08:30", LATER) == "08:30"
    after = rows(db)
    assert len(after) == 1 and after[0]["value"] == "08:30"
    assert after[0]["updated_at_utc"] != first[0]["updated_at_utc"]
    assert after[0]["updated_at_utc"].startswith("2026-10-28T23:00")  # 08:00 JST -> UTC


def test_other_owner_rows_isolated(db):
    set_setting(db, 2, KEY_DIGEST_TIME, "01:00", NOW)
    assert load(db).digest_time == ENV_TIME


def test_defaults_when_nothing_stored(db):
    s = load(db)
    assert s.digest_time == ENV_TIME
    assert s.channel_id == ENV_CHANNEL
    assert s.skip_empty is False and s.notify_dm is False
    assert s.default_remind == DEFAULT_REMIND == "10m"
    assert s.default_remind_todo == ""


def test_stored_overrides_env_and_default(db):
    set_setting(db, OWNER, KEY_DIGEST_TIME, "05:30", NOW)
    set_setting(db, OWNER, KEY_NOTIFY_CHANNEL, "999", NOW)
    set_setting(db, OWNER, KEY_SKIP_EMPTY, "on", NOW)
    set_setting(db, OWNER, KEY_NOTIFY_DM, "yes", NOW)
    set_setting(db, OWNER, KEY_DEFAULT_REMIND, "1h", NOW)
    set_setting(db, OWNER, KEY_DEFAULT_REMIND_TODO, "30M", NOW)
    s = load(db)
    assert s.digest_time == time(5, 30)
    assert s.channel_id == 999
    assert s.skip_empty is True and s.notify_dm is True
    assert s.default_remind == "1h"
    assert s.default_remind_todo == "30m"


def test_broken_stored_values_fall_back(db):
    for k, v in [
        (KEY_DIGEST_TIME, "zz"),
        (KEY_NOTIFY_CHANNEL, "-3"),
        (KEY_SKIP_EMPTY, "???"),
        (KEY_NOTIFY_DM, "???"),
    ]:
        db.execute(
            "INSERT INTO settings (owner_id, key, value, updated_at_utc) VALUES (?, ?, ?, 'x')",
            (OWNER, k, v),
        )
    s = load(db)
    assert s.digest_time == ENV_TIME
    assert s.channel_id == ENV_CHANNEL
    assert s.skip_empty is False and s.notify_dm is False


# -- DigestCog picks changes up on the next tick -------------------------------------------
def text_channel():
    ch = MagicMock(spec=discord.TextChannel)
    ch.guild.id = 1
    ch.permissions_for.return_value = MagicMock(
        view_channel=True, send_messages=True, embed_links=True
    )
    ch.send = AsyncMock()
    return ch


def make_digest_cog(db, clock, channel):
    bot = MagicMock()
    bot.config = BotConfig(
        discord_bot_token="tok",
        guild_id=1,
        allowed_user_ids=frozenset({OWNER}),
        notify_channel_id=ENV_CHANNEL,
        admin_user_id=9,
        owner_user_id=OWNER,
    )
    bot.db = db
    bot.notify_admin = AsyncMock()
    bot.get_channel = MagicMock(return_value=channel)
    bot.fetch_channel = AsyncMock(return_value=channel)
    return DigestCog(bot, ScheduleService(db, OWNER, clock=clock)), bot


def test_digest_uses_new_time_on_next_tick(db):
    clock = FixedClock(datetime(2026, 10, 29, 7, 0, tzinfo=JST))
    ch = text_channel()
    cog, _ = make_digest_cog(db, clock, ch)
    set_setting(db, OWNER, KEY_DIGEST_TIME, "09:00", clock.now())
    assert asyncio.run(cog.run_once()) == "wait"
    ch.send.assert_not_awaited()
    set_setting(db, OWNER, KEY_DIGEST_TIME, "07:00", clock.now())
    assert asyncio.run(cog.run_once()) == "posted"
    ch.send.assert_awaited_once()


def test_digest_uses_new_skip_empty_on_next_tick(db):
    clock = FixedClock(datetime(2026, 10, 29, 7, 0, tzinfo=JST))
    ch = text_channel()
    cog, _ = make_digest_cog(db, clock, ch)
    set_setting(db, OWNER, KEY_SKIP_EMPTY, "on", clock.now())
    assert asyncio.run(cog.run_once()) == "skipped"
    ch.send.assert_not_awaited()


def test_digest_uses_new_channel_on_next_tick(db):
    clock = FixedClock(datetime(2026, 10, 29, 7, 0, tzinfo=JST))
    ch = text_channel()
    cog, bot = make_digest_cog(db, clock, ch)
    set_setting(db, OWNER, KEY_NOTIFY_CHANNEL, "777", clock.now())
    assert asyncio.run(cog.run_once()) == "posted"
    bot.get_channel.assert_called_with(777)


def test_jobs_get_setting_still_agrees_with_settings(db):
    from nomusyo.schedule import jobs
    from nomusyo.schedule.settings import get_setting

    set_setting(db, OWNER, KEY_DIGEST_TIME, "05:30", NOW)
    assert jobs.get_setting(db, OWNER, KEY_DIGEST_TIME) == get_setting(db, OWNER, KEY_DIGEST_TIME)
