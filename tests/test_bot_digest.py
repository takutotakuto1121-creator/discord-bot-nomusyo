import asyncio
import logging
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import discord
import pytest

from nomusyo.bot.cogs.digest import DigestCog
from nomusyo.bot.config import BotConfig
from nomusyo.schedule.clock import FixedClock
from nomusyo.schedule.digest import (
    JOB_DAILY_DIGEST,
    KEY_NOTIFY_CHANNEL,
    KEY_SKIP_EMPTY,
)
from nomusyo.schedule.jobs import job_status
from nomusyo.schedule.service import ScheduleService
from nomusyo.storage import Database

JST = ZoneInfo("Asia/Tokyo")
DAY = "2026-10-29"
ENV_CHANNEL = 111
SECRET = "SECRET-TOKEN-xyz"
EMPTY_TEXT = "予定・TODO はありません。"


def jst(*a):
    return datetime(*a, tzinfo=JST)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db(tmp_path):
    d = Database.open(tmp_path / "t.db")
    yield d
    d.close()


@pytest.fixture
def clock():
    return FixedClock(jst(2026, 10, 29, 7, 0))


def make_bot(db, channel):
    bot = MagicMock()
    bot.config = BotConfig(
        discord_bot_token=SECRET,
        guild_id=1,
        allowed_user_ids=frozenset({1}),
        notify_channel_id=ENV_CHANNEL,
        admin_user_id=9,
        owner_user_id=1,
    )
    bot.db = db
    bot.notify_admin = AsyncMock()
    bot.get_channel = MagicMock(return_value=channel)
    bot.fetch_channel = AsyncMock(return_value=channel)
    return bot


@pytest.fixture
def channel():
    return make_channel()


def make_channel(guild_id=1, view=True, send=True, embed=True):
    ch = MagicMock(spec=discord.TextChannel)
    ch.send = AsyncMock()
    ch.guild = MagicMock()
    ch.guild.id = guild_id
    ch.permissions_for.return_value = MagicMock(
        view_channel=view, send_messages=send, embed_links=embed
    )
    return ch


def make_cog(db, clock, channel):
    bot = make_bot(db, channel)
    svc = ScheduleService(db, 1, clock=clock)
    return DigestCog(bot, svc), bot, svc


def set_setting(db, key, value):
    db.execute(
        "INSERT OR REPLACE INTO settings (owner_id, key, value, updated_at_utc) "
        "VALUES (1, ?, ?, 'x')",
        (key, value),
    )


def detail(db):
    return db.query_one("SELECT * FROM job_runs WHERE job=?", (JOB_DAILY_DIGEST,))


def test_wait_before_time(db, clock, channel):
    clock.set(jst(2026, 10, 29, 6, 54))
    cog, bot, _ = make_cog(db, clock, channel)
    assert run(cog.run_once()) == "wait"
    channel.send.assert_not_awaited()
    assert job_status(db, JOB_DAILY_DIGEST, DAY) is None


def test_posted_with_content_and_no_mentions(db, clock, channel):
    cog, bot, svc = make_cog(db, clock, channel)
    svc.add_event("歯医者", jst(2026, 10, 29, 15, 0))
    svc.add_todo("牛乳を買う", due=jst(2026, 10, 29, 18, 0))
    assert run(cog.run_once()) == "posted"
    channel.send.assert_awaited_once()
    kw = channel.send.await_args.kwargs
    am = kw["allowed_mentions"]
    assert isinstance(am, discord.AllowedMentions)
    assert am.everyone is False and am.users is False and am.roles is False
    text = repr(kw["embed"].to_dict())
    assert "歯医者" in text and "牛乳を買う" in text
    assert job_status(db, JOB_DAILY_DIGEST, DAY) == "done"
    bot.notify_admin.assert_not_awaited()


def test_second_tick_same_day_is_taken(db, clock, channel):
    cog, _, svc = make_cog(db, clock, channel)
    svc.add_event("a", jst(2026, 10, 29, 15, 0))
    assert run(cog.run_once()) == "posted"
    clock.advance(timedelta(seconds=30))
    assert run(cog.run_once()) == "taken"
    assert channel.send.await_count == 1


def test_restart_does_not_double_post(db, clock, channel):
    cog1, _, svc = make_cog(db, clock, channel)
    svc.add_event("a", jst(2026, 10, 29, 15, 0))
    assert run(cog1.run_once()) == "posted"
    cog2, _, _ = make_cog(db, clock, channel)  # new instance = restart
    assert run(cog2.run_once()) == "taken"
    assert channel.send.await_count == 1


def test_next_day_posts_again(db, clock, channel):
    cog, _, _ = make_cog(db, clock, channel)
    assert run(cog.run_once()) == "posted"
    clock.set(jst(2026, 10, 30, 7, 0))
    assert run(cog.run_once()) == "posted"
    assert channel.send.await_count == 2


def test_empty_day_posts_notice_by_default(db, clock, channel):
    cog, _, _ = make_cog(db, clock, channel)
    assert run(cog.run_once()) == "posted"
    embed = channel.send.await_args.kwargs["embed"]
    assert embed.description == EMPTY_TEXT


@pytest.mark.parametrize("value", ["true", "on", "1"])
def test_empty_day_skipped_when_skip_empty(db, clock, channel, value):
    set_setting(db, KEY_SKIP_EMPTY, value)
    cog, bot, _ = make_cog(db, clock, channel)
    assert run(cog.run_once()) == "skipped"
    channel.send.assert_not_awaited()
    assert job_status(db, JOB_DAILY_DIGEST, DAY) == "skipped"
    assert detail(db)["detail"] == "empty"
    clock.advance(timedelta(minutes=1))
    assert run(cog.run_once()) == "taken"
    bot.notify_admin.assert_not_awaited()


def test_skip_empty_true_still_posts_non_empty(db, clock, channel):
    set_setting(db, KEY_SKIP_EMPTY, "true")
    cog, _, svc = make_cog(db, clock, channel)
    svc.add_todo("やること", due=jst(2026, 10, 29, 20, 0))
    assert run(cog.run_once()) == "posted"


def test_skip_empty_false_explicit_posts(db, clock, channel):
    set_setting(db, KEY_SKIP_EMPTY, "false")
    cog, _, _ = make_cog(db, clock, channel)
    assert run(cog.run_once()) == "posted"


def test_send_failure_recorded_without_secrets_then_retry_succeeds(db, clock, channel, caplog):
    cog, bot, svc = make_cog(db, clock, channel)
    svc.add_event("秘密の予定ABC", jst(2026, 10, 29, 15, 0))
    channel.send.side_effect = RuntimeError(f"boom {SECRET} 秘密の予定ABC")
    with caplog.at_level(logging.DEBUG):
        assert run(cog.run_once()) == "failed"
    assert job_status(db, JOB_DAILY_DIGEST, DAY) == "failed"
    d = detail(db)["detail"]
    assert SECRET not in d and "秘密の予定ABC" not in d
    assert d == "RuntimeError"
    bot.notify_admin.assert_awaited_once()
    admin_text = bot.notify_admin.await_args.args[0]
    assert SECRET not in admin_text and "秘密の予定ABC" not in admin_text
    assert SECRET not in caplog.text and "秘密の予定ABC" not in caplog.text

    # next tick retries and succeeds
    channel.send.side_effect = None
    clock.advance(timedelta(seconds=30))
    assert run(cog.run_once()) == "taken"  # still within backoff
    clock.advance(timedelta(minutes=5))
    assert run(cog.run_once()) == "posted"
    assert job_status(db, JOB_DAILY_DIGEST, DAY) == "done"
    assert detail(db)["attempts"] == 2
    clock.advance(timedelta(seconds=30))
    assert run(cog.run_once()) == "taken"


def test_http_error_is_failed(db, clock, channel):
    resp = MagicMock(status=403, reason="Forbidden")
    channel.send.side_effect = discord.Forbidden(resp, "Missing Permissions")
    cog, bot, _ = make_cog(db, clock, channel)
    assert run(cog.run_once()) == "failed"
    assert job_status(db, JOB_DAILY_DIGEST, DAY) == "failed"
    assert "Missing Permissions" not in detail(db)["detail"]


def test_gives_up_after_three_failures(db, clock, channel):
    channel.send.side_effect = RuntimeError("x")
    cog, bot, _ = make_cog(db, clock, channel)
    results = []
    for _ in range(5):
        results.append(run(cog.run_once()))
        clock.advance(timedelta(minutes=11))
    assert results == ["failed", "failed", "failed", "taken", "taken"]
    assert channel.send.await_count == 3
    texts = [c.args[0] for c in bot.notify_admin.await_args_list]
    assert len(texts) == 3
    assert "再試行を諦めました" not in texts[0] and "再試行を諦めました" in texts[2]


def test_channel_fetch_failure_is_failed(db, clock, channel):
    cog, bot, _ = make_cog(db, clock, channel)
    bot.get_channel.return_value = None
    bot.fetch_channel.side_effect = discord.NotFound(MagicMock(status=404, reason="x"), "gone")
    assert run(cog.run_once()) == "failed"
    assert job_status(db, JOB_DAILY_DIGEST, DAY) == "failed"


def test_channel_falls_back_to_fetch(db, clock, channel):
    cog, bot, _ = make_cog(db, clock, channel)
    bot.get_channel.return_value = None
    assert run(cog.run_once()) == "posted"
    bot.fetch_channel.assert_awaited_once_with(ENV_CHANNEL)
    channel.send.assert_awaited_once()


def test_channel_priority_settings_over_env(db, clock, channel):
    set_setting(db, KEY_NOTIFY_CHANNEL, "222")
    cog, bot, _ = make_cog(db, clock, channel)
    assert run(cog.run_once()) == "posted"
    bot.get_channel.assert_called_once_with(222)


def test_channel_env_when_no_setting(db, clock, channel):
    cog, bot, _ = make_cog(db, clock, channel)
    run(cog.run_once())
    bot.get_channel.assert_called_once_with(ENV_CHANNEL)


def test_invalid_channel_setting_uses_env(db, clock, channel):
    set_setting(db, KEY_NOTIFY_CHANNEL, "not-a-number")
    cog, bot, _ = make_cog(db, clock, channel)
    run(cog.run_once())
    bot.get_channel.assert_called_once_with(ENV_CHANNEL)


def test_expired_skips_and_notifies_once(db, clock, channel):
    clock.set(jst(2026, 10, 29, 9, 55, 1))
    cog, bot, _ = make_cog(db, clock, channel)
    assert run(cog.run_once()) == "expired"
    assert job_status(db, JOB_DAILY_DIGEST, DAY) == "skipped"
    assert detail(db)["detail"] == "grace_exceeded"
    bot.notify_admin.assert_awaited_once()
    channel.send.assert_not_awaited()
    clock.advance(timedelta(minutes=5))
    assert run(cog.run_once()) == "noop"
    cog2, bot2, _ = make_cog(db, clock, channel)
    assert run(cog2.run_once()) == "noop"
    bot.notify_admin.assert_awaited_once()
    bot2.notify_admin.assert_not_awaited()


def test_at_grace_boundary_still_posts(db, clock, channel):
    clock.set(jst(2026, 10, 29, 9, 55))
    cog, _, _ = make_cog(db, clock, channel)
    assert run(cog.run_once()) == "posted"


def test_expired_after_already_posted_is_noop(db, clock, channel):
    cog, bot, _ = make_cog(db, clock, channel)
    assert run(cog.run_once()) == "posted"
    clock.set(jst(2026, 10, 29, 12, 0))
    assert run(cog.run_once()) == "noop"
    bot.notify_admin.assert_not_awaited()


def test_custom_digest_time_from_settings(db, clock, channel):
    set_setting(db, "digest_time", "08:00")
    cog, _, _ = make_cog(db, clock, channel)
    assert run(cog.run_once()) == "wait"
    clock.set(jst(2026, 10, 29, 8, 0))
    assert run(cog.run_once()) == "posted"


def test_loop_swallows_unexpected_errors(db, clock, channel):
    cog, _, _ = make_cog(db, clock, channel)
    cog.run_once = AsyncMock(side_effect=RuntimeError("x"))
    run(cog.digest_loop.coro(cog))  # must not raise


def test_cancelled_records_failed_and_reraises(db, clock, channel):
    channel.send.side_effect = asyncio.CancelledError()
    cog, _, _ = make_cog(db, clock, channel)
    with pytest.raises(asyncio.CancelledError):
        run(cog.run_once())
    assert job_status(db, JOB_DAILY_DIGEST, DAY) == "failed"
    assert detail(db)["detail"] == "cancelled"


def test_guild_mismatch_falls_back_to_env_channel_and_notifies(db, clock):
    bad = make_channel(guild_id=999)
    good = make_channel()
    cog, bot, _ = make_cog(db, clock, good)
    set_setting(db, KEY_NOTIFY_CHANNEL, "222")
    bot.get_channel.side_effect = lambda cid: bad if cid == 222 else good
    assert run(cog.run_once()) == "posted"
    bad.send.assert_not_awaited()
    good.send.assert_awaited_once()
    bot.notify_admin.assert_awaited_once()


@pytest.mark.parametrize("perm", ["view", "send", "embed"])
def test_missing_permission_falls_back(db, clock, perm):
    bad = make_channel(**{perm: False})
    good = make_channel()
    cog, bot, _ = make_cog(db, clock, good)
    set_setting(db, KEY_NOTIFY_CHANNEL, "222")
    bot.get_channel.side_effect = lambda cid: bad if cid == 222 else good
    assert run(cog.run_once()) == "posted"
    bad.send.assert_not_awaited()
    bot.notify_admin.assert_awaited_once()


def test_non_text_channel_rejected(db, clock):
    cog, bot, _ = make_cog(db, clock, MagicMock(spec=discord.VoiceChannel))
    assert run(cog.run_once()) == "failed"
    assert job_status(db, JOB_DAILY_DIGEST, DAY) == "failed"
    assert detail(db)["detail"] == "RuntimeError"


def test_both_channels_invalid_is_failed(db, clock):
    bad = make_channel(guild_id=999)
    cog, bot, _ = make_cog(db, clock, bad)
    set_setting(db, KEY_NOTIFY_CHANNEL, "222")
    assert run(cog.run_once()) == "failed"
    bad.send.assert_not_awaited()
    assert job_status(db, JOB_DAILY_DIGEST, DAY) == "failed"


def test_started_row_past_grace_becomes_abandoned_once(db, clock, channel):
    cog, bot, _ = make_cog(db, clock, channel)
    from nomusyo.schedule.jobs import claim_job

    claim_job(db, JOB_DAILY_DIGEST, DAY, jst(2026, 10, 29, 7, 0))
    clock.set(jst(2026, 10, 29, 10, 0))
    assert run(cog.run_once()) == "expired"
    assert job_status(db, JOB_DAILY_DIGEST, DAY) == "failed"
    assert detail(db)["detail"] == "abandoned_started"
    channel.send.assert_not_awaited()
    bot.notify_admin.assert_awaited_once()
    assert run(cog.run_once()) == "noop"
    bot.notify_admin.assert_awaited_once()


def test_started_row_within_grace_is_not_reclaimed(db, clock, channel):
    from nomusyo.schedule.jobs import claim_job

    claim_job(db, JOB_DAILY_DIGEST, DAY, jst(2026, 10, 29, 7, 0))
    cog, _, _ = make_cog(db, clock, channel)
    clock.set(jst(2026, 10, 29, 9, 0))  # stale_after is grace+1h, so still taken
    assert run(cog.run_once()) == "taken"
    channel.send.assert_not_awaited()


def test_done_record_failure_does_not_resend(db, clock, channel, monkeypatch, caplog):
    # Resolve the module DigestCog really lives in: extension load/unload in other tests
    # can evict it from sys.modules, so a fresh `import` could yield a different module.
    g = DigestCog.run_once.__globals__

    real = g["finish_job"]

    def flaky(*a, **kw):
        if a[3] == "done":
            raise RuntimeError("db gone")
        return real(*a, **kw)

    monkeypatch.setitem(g, "finish_job", flaky)
    cog, bot, _ = make_cog(db, clock, channel)
    with caplog.at_level(logging.WARNING, logger=g["log"].name):
        assert run(cog.run_once()) == "posted"
    assert "recording DONE failed" in caplog.text
    channel.send.assert_awaited_once()
    clock.advance(timedelta(hours=2))
    assert run(cog.run_once()) == "taken"
    assert channel.send.await_count == 1
