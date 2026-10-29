import logging
from unittest.mock import MagicMock

import pytest

from nomusyo.bot.cogs.heartbeat import HEARTBEAT_TIMEOUT_SECONDS, send_ping

URL = "https://hc-ping.example.com/SECRET-HEARTBEAT-UUID"


def getter(status=200, exc=None):
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        if exc:
            raise exc
        return MagicMock(status_code=status)

    get.calls = calls
    return get


@pytest.mark.parametrize("status", [200, 204, 299])
def test_success(status):
    get = getter(status)
    assert send_ping(URL, get=get) is True
    url, kwargs = get.calls[0]
    assert url == URL
    assert kwargs["timeout"] == HEARTBEAT_TIMEOUT_SECONDS
    assert kwargs["allow_redirects"] is False


@pytest.mark.parametrize("status", [301, 404, 500, 199])
def test_http_error_returns_false(status, caplog):
    with caplog.at_level(logging.DEBUG):
        assert send_ping(URL, get=getter(status)) is False
    assert str(status) in caplog.text
    assert "SECRET" not in caplog.text


def test_exception_does_not_raise_or_leak_url(caplog):
    exc = RuntimeError(f"connection failed to {URL}")
    with caplog.at_level(logging.DEBUG):
        assert send_ping(URL, get=getter(exc=exc)) is False
    assert "RuntimeError" in caplog.text
    assert "SECRET" not in caplog.text
    assert URL not in caplog.text


def test_success_logs_nothing_with_url(caplog):
    with caplog.at_level(logging.DEBUG):
        send_ping(URL, get=getter(200))
    assert "SECRET" not in caplog.text


# ---- cog: gateway-state gating ---------------------------------------------------------------
import asyncio  # noqa: E402

import nomusyo.bot.cogs.heartbeat as hb  # noqa: E402


def make_cog(monkeypatch, closed=False):
    bot = MagicMock()
    bot.config.heartbeat_url = URL
    bot.is_closed.return_value = closed
    pings = []
    monkeypatch.setattr(hb, "send_ping", lambda url: pings.append(url) or True)
    return hb.HeartbeatCog(bot), pings


def tick(cog):
    asyncio.run(cog.ping_loop.coro(cog))


def test_no_ping_before_ready(monkeypatch):
    cog, pings = make_cog(monkeypatch)
    tick(cog)
    assert pings == []


def test_ping_after_ready(monkeypatch):
    cog, pings = make_cog(monkeypatch)
    asyncio.run(cog.on_ready())
    tick(cog)
    assert pings == [URL]


def test_no_ping_after_disconnect_and_resumes_on_resumed(monkeypatch):
    cog, pings = make_cog(monkeypatch)
    asyncio.run(cog.on_ready())
    asyncio.run(cog.on_disconnect())
    tick(cog)
    assert pings == []
    asyncio.run(cog.on_resumed())
    tick(cog)
    assert pings == [URL]


def test_no_ping_when_bot_closed(monkeypatch):
    cog, pings = make_cog(monkeypatch, closed=True)
    asyncio.run(cog.on_ready())
    tick(cog)
    assert pings == []


def test_cog_load_starts_loop_and_unload_cancels(monkeypatch):
    cog, _ = make_cog(monkeypatch)
    cog.ping_loop = MagicMock()
    asyncio.run(cog.cog_load())
    cog.ping_loop.start.assert_called_once_with()
    asyncio.run(cog.cog_unload())
    cog.ping_loop.cancel.assert_called_once_with()
