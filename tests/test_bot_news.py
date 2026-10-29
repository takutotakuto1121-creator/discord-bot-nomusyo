import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from nomusyo.bot.cogs.news import (
    MSG_BUSY,
    MSG_COOLDOWN,
    MSG_NO_ARTICLES,
    MSG_SUMMARY_ERROR,
    NewsCog,
    payload_to_kwargs,
)
from nomusyo.config import Config
from nomusyo.core.pipeline import NoArticlesError


class Articles(list):
    summary_error = None


def make_interaction():
    inter = MagicMock()
    inter.response.defer = AsyncMock()
    inter.response.send_message = AsyncMock()
    inter.followup.send = AsyncMock()
    inter.edit_original_response = AsyncMock()
    return inter


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def make_cog(pipeline, formatter=None, clock=None, cooldown=300.0):
    bot = MagicMock()
    bot.notify_admin = AsyncMock()
    cog = NewsCog(
        bot,
        Config(),
        pipeline=pipeline,
        formatter=formatter or (lambda arts: [{"content": "hello"}]),
        monotonic=clock or Clock(),
        cooldown_seconds=cooldown,
        message_delay=0,
    )
    return cog, bot


def call_news(cog, inter):
    return cog.news.callback(cog, inter)


def test_payload_to_kwargs():
    kw = payload_to_kwargs({"content": "hi", "embeds": [{"title": "T", "description": "D"}]})
    assert kw["content"] == "hi"
    assert len(kw["embeds"]) == 1 and isinstance(kw["embeds"][0], discord.Embed)
    assert kw["embeds"][0].title == "T"
    assert kw["allowed_mentions"].everyone is False
    assert kw["allowed_mentions"].users is False
    assert kw["allowed_mentions"].roles is False


def test_payload_to_kwargs_without_content_or_embeds():
    kw = payload_to_kwargs({"allowed_mentions": {"parse": ["everyone"]}})
    assert kw["content"] is discord.utils.MISSING
    assert kw["embeds"] == []
    # payload-supplied mentions are ignored; none are ever allowed
    assert kw["allowed_mentions"].everyone is False


def test_success_posts_all_payloads():
    seen = {}

    def pipeline(cfg):
        seen["cfg"] = cfg
        return Articles(["a"])

    payloads = [{"content": "one", "embeds": [{"title": "x"}]}, {"content": "two"}]
    cog, bot = make_cog(pipeline, lambda arts: payloads)
    inter = make_interaction()
    asyncio.run(call_news(cog, inter))

    assert seen["cfg"] is cog.news_config
    inter.response.defer.assert_awaited_once_with(thinking=True)
    assert inter.followup.send.await_count == 2
    first = inter.followup.send.await_args_list[0].kwargs
    assert first["content"] == "one" and len(first["embeds"]) == 1
    assert inter.followup.send.await_args_list[1].kwargs["content"] == "two"
    bot.notify_admin.assert_not_called()


def test_formatter_receives_pipeline_result():
    arts = Articles(["a", "b"])
    got = []

    def formatter(a):
        got.append(a)
        return [{"content": "x"}]

    cog, _ = make_cog(lambda cfg: arts, formatter)
    asyncio.run(call_news(cog, make_interaction()))
    assert got == [arts]


def test_summary_error_notes_and_notifies_admin():
    arts = Articles(["a"])
    arts.summary_error = "AuthenticationError"
    cog, bot = make_cog(lambda cfg: arts)
    inter = make_interaction()
    asyncio.run(call_news(cog, inter))

    calls = inter.followup.send.await_args_list
    assert calls[0].kwargs["content"] == "hello"
    assert calls[-1].args == (MSG_SUMMARY_ERROR,)
    assert calls[-1].kwargs == {}
    bot.notify_admin.assert_awaited_once()
    assert "AuthenticationError" not in bot.notify_admin.await_args.args[0]


def test_no_articles():
    def pipeline(cfg):
        raise NoArticlesError("none")

    cog, bot = make_cog(pipeline)
    inter = make_interaction()
    asyncio.run(call_news(cog, inter))
    inter.edit_original_response.assert_awaited_once_with(content=MSG_NO_ARTICLES)
    inter.followup.send.assert_not_called()
    bot.notify_admin.assert_not_called()


def test_lock_is_released_after_no_articles():
    def pipeline(cfg):
        raise NoArticlesError("none")

    cog, _ = make_cog(pipeline)
    asyncio.run(call_news(cog, make_interaction()))
    assert not cog._lock.locked()


def test_busy_while_locked():
    calls = []
    cog, _ = make_cog(lambda cfg: calls.append(1) or Articles())
    inter = make_interaction()

    async def go():
        await cog._lock.acquire()
        await call_news(cog, inter)

    asyncio.run(go())
    inter.response.send_message.assert_awaited_once_with(MSG_BUSY, ephemeral=True)
    inter.response.defer.assert_not_called()
    assert calls == []


def test_concurrent_second_call_gets_busy():
    release = threading.Event()
    started = threading.Event()

    def pipeline(cfg):
        started.set()
        release.wait(5)
        return Articles(["a"])

    cog, _ = make_cog(pipeline)
    first, second = make_interaction(), make_interaction()

    async def go():
        t1 = asyncio.create_task(call_news(cog, first))
        while not started.is_set():
            await asyncio.sleep(0.01)
        await call_news(cog, second)
        release.set()
        await t1

    asyncio.run(go())
    second.response.send_message.assert_awaited_once_with(MSG_BUSY, ephemeral=True)
    first.followup.send.assert_awaited_once()


def test_cooldown_blocks_then_expires():
    clock = Clock(1000.0)
    runs = []
    cog, _ = make_cog(lambda cfg: runs.append(1) or Articles(["a"]), clock=clock, cooldown=300)

    async def go():
        await call_news(cog, make_interaction())
        clock.t = 1100.0
        blocked = make_interaction()
        await call_news(cog, blocked)
        clock.t = 1300.0
        ok = make_interaction()
        await call_news(cog, ok)
        return blocked, ok

    blocked, ok = asyncio.run(go())
    blocked.response.send_message.assert_awaited_once_with(
        MSG_COOLDOWN.format(remaining=201), ephemeral=True
    )
    blocked.response.defer.assert_not_called()
    ok.response.defer.assert_awaited_once()
    assert len(runs) == 2


def test_cooldown_remaining_initially_zero():
    cog, _ = make_cog(lambda cfg: Articles())
    assert cog.cooldown_remaining() == 0.0


def test_cooldown_applies_after_no_articles():
    def pipeline(cfg):
        raise NoArticlesError("x")

    cog, _ = make_cog(pipeline)

    async def go():
        await call_news(cog, make_interaction())
        again = make_interaction()
        await call_news(cog, again)
        return again

    again = asyncio.run(go())
    again.response.send_message.assert_awaited_once()
    assert "あと" in again.response.send_message.await_args.args[0]


def test_pipeline_runs_in_worker_thread_and_loop_not_blocked():
    release = threading.Event()
    started = threading.Event()
    thread_ids = {}

    def pipeline(cfg):
        thread_ids["pipeline"] = threading.get_ident()
        started.set()
        release.wait(5)
        return Articles(["a"])

    cog, _ = make_cog(pipeline)
    ticks = []

    async def ticker():
        for _ in range(5):
            await asyncio.sleep(0.01)
            ticks.append(1)
        release.set()

    async def go():
        thread_ids["loop"] = threading.get_ident()
        t = asyncio.create_task(ticker())
        await call_news(cog, make_interaction())
        await t

    asyncio.run(go())
    assert thread_ids["pipeline"] != thread_ids["loop"]
    assert len(ticks) == 5


def test_cooldown_not_started_when_defer_fails():
    cog, _ = make_cog(lambda cfg: Articles(["a"]))
    inter = make_interaction()
    inter.response.defer = AsyncMock(side_effect=RuntimeError("defer failed"))

    with pytest.raises(RuntimeError):
        asyncio.run(call_news(cog, inter))
    assert cog.cooldown_remaining() == 0
    assert not cog._lock.locked()
