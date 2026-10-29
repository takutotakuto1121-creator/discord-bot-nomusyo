import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands

from nomusyo.bot import app as app_mod
from nomusyo.bot.app import (
    ADMIN_NOTIFY_MIN_INTERVAL,
    ERROR_REPLY,
    NomusyoBot,
    configure_logging,
    describe_error,
)
from nomusyo.bot.checks import AllowedCommandTree
from nomusyo.bot.config import BotConfig
from nomusyo.config import Config

TOKEN = "MTIzNDU2.SECRET-BOT-TOKEN.value"
URL = "https://hc-ping.example.com/SECRET-HEARTBEAT-UUID"


def make_config(**over):
    kw = dict(
        discord_bot_token=TOKEN,
        guild_id=111,
        allowed_user_ids=frozenset({222}),
        notify_channel_id=333,
        admin_user_id=222,
        heartbeat_url="",
        news=Config(anthropic_api_key="sk-ant-x"),
    )
    kw.update(over)
    return BotConfig(**kw)


def make_bot(clock=None, **over):
    bot = NomusyoBot(make_config(**over))
    admin = MagicMock()
    admin.send = AsyncMock()
    bot.fetch_user = AsyncMock(return_value=admin)
    bot._admin = admin
    return bot


def make_interaction(*, done=False, command="news"):
    inter = MagicMock()
    inter.command.qualified_name = command
    inter.response.is_done.return_value = done
    inter.response.send_message = AsyncMock()
    inter.followup.send = AsyncMock()
    return inter


def test_intents_have_no_privileged():
    bot = NomusyoBot(make_config())
    assert bot.intents.members is False
    assert bot.intents.presences is False
    assert bot.intents.message_content is False


def test_tree_is_allowed_tree_with_config_ids():
    bot = NomusyoBot(make_config(allowed_user_ids=frozenset({1, 2})))
    assert isinstance(bot.tree, AllowedCommandTree)
    assert bot.tree.allowed_user_ids == frozenset({1, 2})


def test_mentions_disabled():
    bot = NomusyoBot(make_config())
    assert bot.allowed_mentions.everyone is False
    assert bot.allowed_mentions.users is False
    assert bot.allowed_mentions.roles is False


def test_setup_hook_registers_expected_commands(monkeypatch, tmp_path):
    bot = make_bot(db_path=tmp_path / "bot.db")
    sync = AsyncMock(return_value=[1, 2])
    monkeypatch.setattr(bot.tree, "sync", sync)

    async def go():
        await bot.setup_hook()
        names = {c.name for c in bot.tree.get_commands(guild=None)}
        for name in list(bot.extensions):
            await bot.unload_extension(name)
        return names

    names = asyncio.run(go())
    assert names == {
        "news",
        "status",
        "add",
        "list",
        "edit",
        "delete",
        "todo",
        "today",
        "week",
        "settings",
    }
    assert sync.await_count == 1
    assert sync.await_args.kwargs["guild"].id == 111
    assert "nomusyo.bot.cogs.heartbeat" not in bot.extensions


def test_describe_error_is_type_name_only():
    assert describe_error(ValueError("secret " + TOKEN)) == "ValueError"


def test_notify_admin_sends_dm():
    bot = make_bot()
    asyncio.run(bot.notify_admin("hello"))
    bot.fetch_user.assert_awaited_once_with(222)
    bot._admin.send.assert_awaited_once_with("hello")


def test_notify_admin_rate_limited(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(app_mod.time, "monotonic", lambda: now[0])
    bot = make_bot()

    async def go():
        await bot.notify_admin("1")
        now[0] += ADMIN_NOTIFY_MIN_INTERVAL - 1
        await bot.notify_admin("2")
        now[0] += 2
        await bot.notify_admin("3")

    asyncio.run(go())
    assert [c.args[0] for c in bot._admin.send.await_args_list] == [
        "1",
        "3\n(他 1 件を抑制)",
    ]


def test_notify_admin_swallows_exceptions_and_logs_type_only(caplog):
    bot = make_bot()
    bot.fetch_user = AsyncMock(side_effect=RuntimeError("leak " + TOKEN))
    with caplog.at_level(logging.DEBUG):
        asyncio.run(bot.notify_admin("x"))
    assert "RuntimeError" in caplog.text
    assert TOKEN not in caplog.text


def test_notify_admin_swallows_send_failure():
    bot = make_bot()
    bot._admin.send.side_effect = discord.Forbidden(MagicMock(status=403, reason="x"), "dm closed")
    asyncio.run(bot.notify_admin("x"))  # must not raise


def test_tree_error_check_failure_ignored():
    bot = make_bot()
    inter = make_interaction()
    asyncio.run(bot.on_tree_error(inter, app_commands.CheckFailure("denied")))
    inter.response.send_message.assert_not_called()
    inter.followup.send.assert_not_called()
    bot._admin.send.assert_not_called()


def test_tree_error_replies_fixed_text_and_notifies_admin():
    bot = make_bot()
    inter = make_interaction(command="news")
    original = RuntimeError(f"boom {TOKEN} {URL}")
    err = app_commands.CommandInvokeError(MagicMock(), original)
    asyncio.run(bot.on_tree_error(inter, err))

    inter.response.send_message.assert_awaited_once_with(ERROR_REPLY, ephemeral=True)
    sent = bot._admin.send.await_args.args[0]
    assert "/news" in sent and "RuntimeError" in sent
    assert TOKEN not in sent and "SECRET" not in sent


def test_tree_error_uses_followup_when_already_responded():
    bot = make_bot()
    inter = make_interaction(done=True)
    asyncio.run(bot.on_tree_error(inter, RuntimeError("x")))
    inter.followup.send.assert_awaited_once_with(ERROR_REPLY, ephemeral=True)
    inter.response.send_message.assert_not_called()


def test_tree_error_reply_failure_does_not_raise_and_still_notifies():
    bot = make_bot()
    inter = make_interaction()
    inter.response.send_message.side_effect = discord.HTTPException(
        MagicMock(status=500, reason="x"), "fail"
    )
    asyncio.run(bot.on_tree_error(inter, RuntimeError("x")))
    bot._admin.send.assert_awaited_once()


def test_tree_error_log_does_not_contain_message(caplog):
    bot = make_bot()
    inter = make_interaction()
    err = app_commands.CommandInvokeError(MagicMock(), RuntimeError(f"boom {TOKEN} {URL}"))
    with caplog.at_level(logging.DEBUG):
        asyncio.run(bot.on_tree_error(inter, err))
    assert "RuntimeError" in caplog.text
    assert TOKEN not in caplog.text
    assert "SECRET" not in caplog.text


def test_on_error_notifies_with_type_name_only(caplog):
    bot = make_bot()

    async def go():
        try:
            raise ValueError(f"leak {TOKEN}")
        except ValueError:
            await bot.on_error("on_message")

    with caplog.at_level(logging.DEBUG):
        asyncio.run(go())
    sent = bot._admin.send.await_args.args[0]
    assert "on_message" in sent and "ValueError" in sent
    assert TOKEN not in sent
    assert TOKEN not in caplog.text


def test_configure_logging_quiets_noisy_loggers():
    configure_logging("DEBUG")
    for name in ("httpx", "urllib3", "discord.http", "anthropic", "requests"):
        assert logging.getLogger(name).level == logging.WARNING


@pytest.mark.parametrize("level", ["BOGUS", "INFO"])
def test_configure_logging_unknown_level_does_not_raise(level):
    configure_logging(level)


# ---- new spec: redaction, location, guild leave, notify_admin, setup ----------------------
import ast  # noqa: E402
import pathlib  # noqa: E402

from nomusyo.bot.app import (  # noqa: E402
    RedactingFilter,
    collect_secrets,
    describe_location,
)
from nomusyo.bot.cogs import admin as admin_cog  # noqa: E402
from nomusyo.bot.cogs import news as news_cog  # noqa: E402
from nomusyo.bot.cogs.alive import touch_alive  # noqa: E402

LONG = "supersecretvalue123"


def make_record(msg, args=None, exc_info=None):
    return logging.LogRecord("t", logging.ERROR, __file__, 1, msg, args, exc_info)


@pytest.fixture
def clean_root_handlers():
    root = logging.getLogger()
    saved_h, saved_l = list(root.handlers), root.level
    for h in saved_h:
        root.removeHandler(h)
    yield root
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in saved_h:
        root.addHandler(h)
    root.setLevel(saved_l)


def test_filter_redacts_expanded_message():
    f = RedactingFilter([LONG])
    rec = make_record("token=%s ok", (LONG,))
    assert f.filter(rec) is True
    assert rec.getMessage() == "token=*** ok"


def test_filter_redacts_exc_text():
    f = RedactingFilter([LONG])
    rec = make_record("x")
    rec.exc_text = f"Traceback ... ValueError: {LONG}"
    f.filter(rec)
    assert LONG not in rec.exc_text and "***" in rec.exc_text


def test_filter_ignores_empty_and_short_secrets():
    f = RedactingFilter(["", "short", "1234567", LONG])
    rec = make_record("short 1234567 and " + LONG)
    f.filter(rec)
    assert rec.getMessage() == "short 1234567 and ***"


def test_filter_replaces_longer_secret_first():
    long_ = LONG + "-extra"
    f = RedactingFilter([LONG, long_])
    rec = make_record(f"a {long_} b")
    f.filter(rec)
    assert rec.getMessage() == "a *** b"


def test_filter_bad_format_args_do_not_raise():
    f = RedactingFilter([LONG])
    rec = make_record("%d only", ("notanint",))
    assert f.filter(rec) is True


def test_configure_logging_no_duplicate_filter(clean_root_handlers):
    configure_logging("INFO", [LONG])
    configure_logging("INFO", [LONG])
    for h in clean_root_handlers.handlers:
        assert sum(isinstance(x, RedactingFilter) for x in h.filters) == 1


def test_configure_logging_redacts_traceback_in_output(clean_root_handlers):
    import io

    configure_logging("INFO", [LONG])
    stream = io.StringIO()
    handler = clean_root_handlers.handlers[0]
    handler.setStream(stream)
    log = logging.getLogger("redact.test")
    try:
        raise RuntimeError(f"boom {LONG}")
    except RuntimeError:
        log.exception("failed %s", LONG)
    out = stream.getvalue()
    assert "RuntimeError" in out
    assert LONG not in out
    assert "***" in out


def test_collect_secrets():
    cfg = make_config(
        heartbeat_url=URL,
        news=Config(anthropic_api_key="sk-ant-key", discord_webhook_url="https://wh/secret"),
    )
    assert set(collect_secrets(cfg)) == {TOKEN, URL, "sk-ant-key", "https://wh/secret"}


def test_describe_location_without_traceback():
    assert describe_location(ValueError("x")) == "unknown"


def test_describe_location_with_traceback_excludes_message():
    def inner():
        raise ValueError("leak " + TOKEN)

    try:
        inner()
    except ValueError as exc:
        loc = describe_location(exc)
    assert loc.startswith("test_bot_app.py:") and loc.endswith(":inner")
    assert TOKEN not in loc and "leak" not in loc


# -- guild leave ---------------------------------------------------------------------------
def make_guild(gid, leave=None):
    g = MagicMock()
    g.id = gid
    g.leave = leave or AsyncMock()
    return g


def test_leave_if_foreign_leaves_other_guild():
    bot = make_bot()
    g = make_guild(999)
    assert asyncio.run(bot._leave_if_foreign(g)) is True
    g.leave.assert_awaited_once()


def test_leave_if_foreign_keeps_own_guild():
    bot = make_bot()
    g = make_guild(111)
    assert asyncio.run(bot._leave_if_foreign(g)) is False
    g.leave.assert_not_called()


def test_leave_if_foreign_failure_does_not_raise():
    bot = make_bot()
    g = make_guild(999, AsyncMock(side_effect=discord.HTTPException(MagicMock(status=500), "x")))
    assert asyncio.run(bot._leave_if_foreign(g)) is False


def test_on_guild_join_foreign_leaves_and_notifies():
    bot = make_bot()
    g = make_guild(999)
    asyncio.run(bot.on_guild_join(g))
    g.leave.assert_awaited_once()
    sent = bot._admin.send.await_args.args[0]
    assert "999" in sent


def test_on_guild_join_own_guild_noop():
    bot = make_bot()
    g = make_guild(111)
    asyncio.run(bot.on_guild_join(g))
    g.leave.assert_not_called()
    bot._admin.send.assert_not_called()


def test_on_ready_single_summary_notification(monkeypatch):
    bot = make_bot()
    guilds = [make_guild(111), make_guild(998), make_guild(999)]
    monkeypatch.setattr(NomusyoBot, "guilds", property(lambda self: guilds))
    asyncio.run(bot.on_ready())
    guilds[0].leave.assert_not_called()
    guilds[1].leave.assert_awaited_once()
    guilds[2].leave.assert_awaited_once()
    assert bot._admin.send.await_count == 1
    sent = bot._admin.send.await_args.args[0]
    assert "2" in sent and "998" in sent and "999" in sent


def test_on_ready_no_foreign_no_notification(monkeypatch):
    bot = make_bot()
    monkeypatch.setattr(NomusyoBot, "guilds", property(lambda self: [make_guild(111)]))
    asyncio.run(bot.on_ready())
    bot._admin.send.assert_not_called()


# -- notify_admin --------------------------------------------------------------------------
def test_notify_admin_failure_does_not_start_window_and_retry_works(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(app_mod.time, "monotonic", lambda: now[0])
    bot = make_bot()
    bot._admin.send.side_effect = [RuntimeError("x"), None]

    async def go():
        await bot.notify_admin("a")
        now[0] += 1
        await bot.notify_admin("b")

    asyncio.run(go())
    assert [c.args[0] for c in bot._admin.send.await_args_list] == ["a", "b"]


def test_notify_admin_suppressed_count_resets_after_success(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(app_mod.time, "monotonic", lambda: now[0])
    bot = make_bot()

    async def go():
        await bot.notify_admin("1")
        now[0] += 1
        await bot.notify_admin("2")  # suppressed
        now[0] += ADMIN_NOTIFY_MIN_INTERVAL
        await bot.notify_admin("3")  # carries count
        now[0] += ADMIN_NOTIFY_MIN_INTERVAL
        await bot.notify_admin("4")  # no count

    asyncio.run(go())
    assert [c.args[0] for c in bot._admin.send.await_args_list] == [
        "1",
        "3\n(他 1 件を抑制)",
        "4",
    ]


def test_notify_admin_text_has_no_secret_from_error_path():
    bot = make_bot()
    inter = make_interaction()
    err = app_commands.CommandInvokeError(MagicMock(), RuntimeError(f"x {TOKEN} {URL}"))
    asyncio.run(bot.on_tree_error(inter, err))
    sent = bot._admin.send.await_args.args[0]
    assert TOKEN not in sent and URL not in sent


# -- intents / setup / misc ----------------------------------------------------------------
def test_intents_guilds_only():
    bot = NomusyoBot(make_config())
    assert bot.intents.guilds is True
    on = {name for name, value in bot.intents if value}
    assert on == {"guilds"}


def test_setup_hook_loads_expected_cogs(monkeypatch, tmp_path):
    bot = make_bot(db_path=tmp_path / "bot.db")
    monkeypatch.setattr(bot.tree, "sync", AsyncMock(return_value=[]))

    async def go():
        await bot.setup_hook()
        assert set(bot.cogs) == {
            "NewsCog",
            "AdminCog",
            "ScheduleCog",
            "TodoCog",
            "AgendaCog",
            "DigestCog",
            "SettingsCog",
            "AliveCog",
        }
        for name in list(bot.extensions):
            await bot.unload_extension(name)

    asyncio.run(go())


def test_setup_hook_loads_heartbeat_when_url_set(monkeypatch, tmp_path):
    bot = make_bot(heartbeat_url=URL, db_path=tmp_path / "bot.db")
    monkeypatch.setattr(bot.tree, "sync", AsyncMock(return_value=[]))

    async def go():
        await bot.setup_hook()
        assert "nomusyo.bot.cogs.heartbeat" in bot.extensions
        assert "nomusyo.bot.cogs.alive" in bot.extensions
        for name in list(bot.extensions):
            await bot.unload_extension(name)

    asyncio.run(go())


def test_setup_hook_fails_when_db_cannot_be_opened_and_logs_no_message(
    monkeypatch, tmp_path, caplog
):
    db_path = tmp_path / "bot.db"
    bot = make_bot(db_path=db_path)
    monkeypatch.setattr(bot.tree, "sync", AsyncMock(return_value=[]))

    def boom(path, holder, **kw):
        raise OSError(f"leak {TOKEN}")

    monkeypatch.setattr(app_mod, "open_database_with_lock", boom)

    async def go():
        await bot.setup_hook()

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(OSError):
            asyncio.run(go())
    assert bot.db is None
    assert "OSError" in caplog.text
    assert str(db_path) in caplog.text
    assert TOKEN not in caplog.text
    assert "leak" not in caplog.text
    assert "nomusyo.bot.cogs.schedule" not in bot.extensions


def test_touch_alive_creates_file(tmp_path):
    p = tmp_path / "alive"
    assert touch_alive(p) is True
    assert p.exists()


def test_touch_alive_failure_returns_false(tmp_path):
    assert touch_alive(tmp_path / "missing_dir" / "alive") is False


def test_default_permissions_admin_only():
    assert news_cog.NewsCog.news.default_permissions.administrator is True
    assert admin_cog.AdminCog.status.default_permissions.administrator is True


def test_no_direct_view_or_modal_subclass_outside_checks():
    root = pathlib.Path(app_mod.__file__).parent
    offenders = []
    for path in root.rglob("*.py"):
        if path.name == "checks.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ClassDef):
                continue
            for base in node.bases:
                dotted = ast.unparse(base)
                if dotted in {"discord.ui.View", "discord.ui.Modal", "ui.View", "ui.Modal"}:
                    offenders.append(f"{path.name}:{node.name}")
    assert offenders == []


@pytest.mark.parametrize("bad", ["basicConfig", "NOTSET", "bogus", "", "  "])
def test_configure_logging_invalid_level_falls_back_to_info(monkeypatch, bad):
    seen = {}
    monkeypatch.setattr(logging, "basicConfig", lambda **kw: seen.update(kw))
    configure_logging(bad)
    assert seen["level"] == logging.INFO


def test_setup_hook_opens_db_once_and_close_closes_once(monkeypatch, tmp_path):
    bot = make_bot(db_path=tmp_path / "bot.db")
    monkeypatch.setattr(bot.tree, "sync", AsyncMock(return_value=[]))
    db = MagicMock()
    opened = MagicMock(return_value=db)
    monkeypatch.setattr(app_mod, "open_database_with_lock", opened)

    async def go():
        await bot.setup_hook()
        assert bot.db is db
        await bot.close()
        await bot.close()

    asyncio.run(go())
    opened.assert_called_once()
    db.close.assert_called_once()
    assert bot.db is None


def test_close_closes_db_after_super_close(monkeypatch):
    from discord.ext import commands

    bot = make_bot()
    order = []
    db = MagicMock()
    db.close.side_effect = lambda: order.append("db")
    bot.db = db

    async def fake_super_close(self):
        order.append("super")

    monkeypatch.setattr(commands.Bot, "close", fake_super_close)
    asyncio.run(bot.close())
    assert order == ["super", "db"]


def test_close_db_error_is_logged_type_only(monkeypatch, caplog):
    from discord.ext import commands

    bot = make_bot()
    db = MagicMock()
    db.close.side_effect = OSError(f"leak {TOKEN}")
    bot.db = db
    monkeypatch.setattr(commands.Bot, "close", AsyncMock())
    with caplog.at_level(logging.DEBUG):
        asyncio.run(bot.close())
    assert "OSError" in caplog.text and TOKEN not in caplog.text
