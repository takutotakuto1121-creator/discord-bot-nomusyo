import asyncio
import logging
from datetime import datetime, time
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import discord
import pytest

from nomusyo.bot.checks import DENIED_MESSAGE, ERROR_REPLY
from nomusyo.bot.cogs.settings import (
    MSG_BAD_CHANNEL,
    MSG_FAILED,
    ChannelSelect,
    SettingsCog,
    SettingsView,
    ValueModal,
    settings_embed,
)
from nomusyo.bot.config import BotConfig
from nomusyo.schedule.settings import (
    KEY_DEFAULT_REMIND,
    KEY_DIGEST_TIME,
    KEY_NOTIFY_CHANNEL,
    KEY_NOTIFY_DM,
    KEY_SKIP_EMPTY,
    load_user_settings,
)
from nomusyo.storage import Database

OWNER = 100
OTHER = 999
ENV_CHANNEL = 111
SECRET = "SECRET-TOKEN-xyz"
JST = ZoneInfo("Asia/Tokyo")
NOW = datetime(2026, 10, 29, 7, 0, tzinfo=JST)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db(tmp_path):
    d = Database.open(tmp_path / "t.db")
    yield d
    d.close()


def make_cog(db):
    bot = MagicMock()
    bot.config = BotConfig(
        discord_bot_token=SECRET,
        guild_id=1,
        allowed_user_ids=frozenset({OWNER}),
        notify_channel_id=ENV_CHANNEL,
        admin_user_id=9,
        owner_user_id=OWNER,
    )
    bot.db = db
    cog = SettingsCog(bot, bot.config.allowed_user_ids)
    cog._now = lambda: NOW
    return cog


def make_inter(user_id=OWNER, *, done=False):
    inter = MagicMock()
    inter.user.id = user_id
    inter.type = discord.InteractionType.component
    inter.response.is_done.return_value = done
    inter.response.send_message = AsyncMock()
    inter.response.send_modal = AsyncMock()
    inter.followup.send = AsyncMock()
    inter.original_response = AsyncMock(return_value=MagicMock())
    inter.client.notify_admin = AsyncMock()
    return inter


def sent(inter):
    return inter.response.send_message.await_args_list + inter.followup.send.await_args_list


def text(inter):
    c = sent(inter)[-1]
    return c.args[0] if c.args else c.kwargs.get("content", "")


def assert_private(inter):
    calls = sent(inter)
    assert calls
    for c in calls:
        assert c.kwargs["ephemeral"] is True
        am = c.kwargs["allowed_mentions"]
        assert isinstance(am, discord.AllowedMentions)
        assert am.everyone is False and am.users is False and am.roles is False


def rows(db):
    return db.query("SELECT * FROM settings ORDER BY key", ())


def stored(db):
    return {r["key"]: r["value"] for r in rows(db)}


def pick(monkeypatch, channel_id):
    picked = MagicMock()
    picked.id = channel_id
    monkeypatch.setattr(ChannelSelect, "values", property(lambda self: [picked]))


def guild_with(channel, *, can_send=True):
    g = MagicMock()
    g.get_channel = MagicMock(return_value=channel)
    if channel is not None and hasattr(channel, "permissions_for"):
        channel.permissions_for.return_value = MagicMock(send_messages=can_send)
    return g


def text_channel(cid=555):
    ch = MagicMock(spec=discord.TextChannel)
    ch.id = cid
    return ch


# -- /settings command ---------------------------------------------------------------------
def test_command_shows_ephemeral_embed_with_view(db):
    cog = make_cog(db)
    inter = make_inter()

    async def go():
        await cog.settings.callback(cog, inter)

    run(go())
    kw = inter.response.send_message.await_args.kwargs
    assert kw["ephemeral"] is True
    am = kw["allowed_mentions"]
    assert am.everyone is False and am.users is False and am.roles is False
    assert isinstance(kw["view"], SettingsView)
    fields = {f.name: f.value for f in kw["embed"].fields}
    assert fields["ダイジェスト時刻"] == "06:55"
    assert fields["既定リマインド(予定)"] == "10m"
    assert fields["既定リマインド(TODO)"] == "なし"
    assert fields["通知チャンネル"] == f"<#{ENV_CHANNEL}>"


def test_command_load_failure_replies(db, caplog):
    cog = make_cog(db)
    db.close()  # force failure
    inter = make_inter()
    with caplog.at_level(logging.DEBUG):
        run(cog.settings.callback(cog, inter))
    assert "失敗" in text(inter)
    assert_private(inter)
    assert SECRET not in caplog.text


def test_embed_reflects_stored(db):
    cog = make_cog(db)
    cog.save(KEY_DIGEST_TIME, "05:30")
    cog.save(KEY_SKIP_EMPTY, "on")
    e = settings_embed(cog.load())
    d = {f.name: f.value for f in e.fields}
    assert d["ダイジェスト時刻"] == "05:30"
    assert d["予定なしの日はダイジェストを出さない"] == "オン"


# -- ChannelSelect -------------------------------------------------------------------------
def test_channel_select_saves_valid_channel(db, monkeypatch):
    cog = make_cog(db)
    inter = make_inter()
    inter.guild = guild_with(text_channel(555))
    pick(monkeypatch, 555)

    async def go():
        view = SettingsView(cog)
        sel = next(c for c in view.children if isinstance(c, ChannelSelect))
        await view.interaction_check(inter)
        await sel.callback(inter)

    run(go())
    assert stored(db) == {KEY_NOTIFY_CHANNEL: "555"}
    assert "保存" in text(inter)
    assert_private(inter)


def other_guild_case():
    # channel not found in this guild
    return guild_with(None)


def non_text_case():
    return guild_with(MagicMock(spec=discord.VoiceChannel))


def no_send_case():
    return guild_with(text_channel(), can_send=False)


@pytest.mark.parametrize("case", [other_guild_case, non_text_case, no_send_case])
def test_channel_select_rejects_and_writes_nothing(db, monkeypatch, case):
    cog = make_cog(db)
    cog.save(KEY_NOTIFY_CHANNEL, "42")
    before = rows(db)
    inter = make_inter()
    inter.guild = case()
    pick(monkeypatch, 555)

    async def go():
        view = SettingsView(cog)
        sel = next(c for c in view.children if isinstance(c, ChannelSelect))
        await sel.callback(inter)

    run(go())
    assert text(inter) == MSG_BAD_CHANNEL
    assert_private(inter)
    assert rows(db) == before


def test_channel_select_without_guild_rejected(db, monkeypatch):
    cog = make_cog(db)
    inter = make_inter()
    inter.guild = None
    pick(monkeypatch, 555)

    async def go():
        view = SettingsView(cog)
        sel = next(c for c in view.children if isinstance(c, ChannelSelect))
        await sel.callback(inter)

    run(go())
    assert text(inter) == MSG_BAD_CHANNEL
    assert rows(db) == []


def test_channel_select_bot_member_missing_rejected(db, monkeypatch):
    cog = make_cog(db)
    inter = make_inter()
    inter.guild = guild_with(text_channel())
    inter.guild.me = None
    pick(monkeypatch, 555)

    async def go():
        view = SettingsView(cog)
        sel = next(c for c in view.children if isinstance(c, ChannelSelect))
        await sel.callback(inter)

    run(go())
    assert text(inter) == MSG_BAD_CHANNEL
    assert rows(db) == []


# -- allowed users -------------------------------------------------------------------------
def test_view_denies_other_user_and_does_not_write(db):
    cog = make_cog(db)
    inter = make_inter(OTHER)

    async def go():
        view = SettingsView(cog)
        assert await view.interaction_check(inter) is False

    run(go())
    inter.response.send_message.assert_awaited_once()
    assert inter.response.send_message.await_args.args[0] == DENIED_MESSAGE
    assert inter.response.send_message.await_args.kwargs["ephemeral"] is True
    assert rows(db) == []


def test_view_allows_owner(db):
    cog = make_cog(db)

    async def go():
        return await SettingsView(cog).interaction_check(make_inter())

    assert run(go()) is True


def test_modal_denies_other_user(db):
    cog = make_cog(db)
    inter = make_inter(OTHER)

    async def go():
        m = ValueModal(cog, KEY_DIGEST_TIME, "t", "l", "p")
        assert await m.interaction_check(inter) is False

    run(go())
    assert inter.response.send_message.await_args.args[0] == DENIED_MESSAGE
    assert rows(db) == []


def test_command_is_guild_only_and_tree_denies_other_user(db):
    # The command is only reachable through AllowedCommandTree (see test_bot_checks/app);
    # here we check the guild_only marker.
    assert SettingsCog.settings.guild_only is True


# -- modal / buttons -----------------------------------------------------------------------
def submit(cog, key, value, inter):
    async def go():
        m = ValueModal(cog, key, "t", "l", "p")
        m.value._value = value  # TextInput.value reads the submitted value
        await m.on_submit(inter)

    run(go())


def test_modal_saves_time(db):
    cog = make_cog(db)
    inter = make_inter()
    submit(cog, KEY_DIGEST_TIME, "06:05", inter)
    assert stored(db) == {KEY_DIGEST_TIME: "06:05"}
    assert_private(inter)
    assert "保存" in text(inter)


def test_modal_saves_remind_normalized(db):
    cog = make_cog(db)
    submit(cog, KEY_DEFAULT_REMIND, "10M", make_inter())
    assert stored(db) == {KEY_DEFAULT_REMIND: "10m"}


@pytest.mark.parametrize(
    ("key", "value"),
    [(KEY_DIGEST_TIME, "25:00"), (KEY_DEFAULT_REMIND, "zzz"), (KEY_DIGEST_TIME, "")],
)
def test_modal_invalid_value_refused_without_write(db, key, value):
    cog = make_cog(db)
    cog.save(key, "07:00" if key == KEY_DIGEST_TIME else "5m")
    before = rows(db)
    inter = make_inter()
    submit(cog, key, value, inter)
    assert rows(db) == before
    assert "保存しました" not in text(inter)
    assert_private(inter)


def test_modal_on_error_logs_type_only(db, caplog):
    cog = make_cog(db)
    inter = make_inter()

    async def go():
        m = ValueModal(cog, KEY_DIGEST_TIME, "t", "l", "p")
        await m.on_error(inter, RuntimeError(f"boom {SECRET}"))

    with caplog.at_level(logging.DEBUG):
        run(go())
    assert text(inter) == ERROR_REPLY
    assert SECRET not in caplog.text and "RuntimeError" in caplog.text
    assert SECRET not in text(inter)
    assert sent(inter)[-1].kwargs["ephemeral"] is True
    inter.client.notify_admin.assert_awaited_once()
    assert SECRET not in inter.client.notify_admin.await_args.args[0]


def find(view, label):
    return next(c for c in view.children if getattr(c, "label", None) == label)


def test_button_opens_modal(db):
    cog = make_cog(db)
    inter = make_inter()

    async def go():
        view = SettingsView(cog)
        await find(view, "ダイジェスト時刻").callback(inter)

    run(go())
    modal = inter.response.send_modal.await_args.args[0]
    assert isinstance(modal, ValueModal) and modal.key == KEY_DIGEST_TIME


@pytest.mark.parametrize(
    ("label", "key", "attr"),
    [
        ("空の日スキップ切替", KEY_SKIP_EMPTY, "skip_empty"),
        ("DM通知切替", KEY_NOTIFY_DM, "notify_dm"),
    ],
)
def test_toggle_buttons_flip_value(db, label, key, attr):
    cog = make_cog(db)

    async def press():
        inter = make_inter()
        await find(SettingsView(cog), label).callback(inter)
        return inter

    def val():
        return getattr(
            load_user_settings(db, OWNER, default_time=time(6, 55), default_channel_id=ENV_CHANNEL),
            attr,
        )

    assert val() is False
    inter = run(press())
    assert val() is True and stored(db) == {key: "true"}
    assert_private(inter)
    run(press())
    assert val() is False and stored(db) == {key: "false"}


# -- failures / logs -----------------------------------------------------------------------
def test_save_exception_gives_failure_message_and_safe_log(db, caplog):
    cog = make_cog(db)

    def boom(key, raw):
        raise RuntimeError(f"disk {SECRET} {raw}")

    cog.save = boom
    inter = make_inter()
    with caplog.at_level(logging.DEBUG):
        run(cog.apply(inter, KEY_DIGEST_TIME, "06:05", None))
    assert text(inter) == MSG_FAILED
    assert_private(inter)
    assert SECRET not in caplog.text and "06:05" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_toggle_load_failure_replies_failed(db):
    cog = make_cog(db)

    def boom():
        raise RuntimeError("x")

    cog.load = boom
    inter = make_inter()
    run(find(SettingsView(cog), "DM通知切替").callback(inter))  # noqa: RUF006
    assert text(inter) == MSG_FAILED


def test_followup_used_when_response_done(db):
    cog = make_cog(db)
    inter = make_inter(done=True)
    run(cog.apply(inter, KEY_NOTIFY_DM, "yes", None))
    inter.followup.send.assert_awaited_once()
    inter.response.send_message.assert_not_awaited()
    assert_private(inter)


def test_invalid_value_via_apply_no_log_of_value(db, caplog):
    cog = make_cog(db)
    inter = make_inter()
    with caplog.at_level(logging.DEBUG):
        run(cog.apply(inter, KEY_NOTIFY_CHANNEL, "not-a-number-xyz", None))
    assert rows(db) == []
    assert "not-a-number-xyz" not in caplog.text
    assert_private(inter)


def test_view_on_timeout_removes_view(db):
    cog = make_cog(db)

    async def go():
        view = SettingsView(cog)
        view.message = MagicMock()
        view.message.edit = AsyncMock()
        await view.on_timeout()
        view.message.edit.assert_awaited_once_with(view=None)
        view.message.edit = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "x"))
        await view.on_timeout()  # swallowed

    run(go())


@pytest.mark.parametrize("missing", ["view_channel", "send_messages", "embed_links"])
def test_channel_select_requires_view_send_and_embed_links(db, monkeypatch, missing):
    cog = make_cog(db)
    inter = make_inter()
    ch = text_channel(555)
    perms = dict(view_channel=True, send_messages=True, embed_links=True)
    perms[missing] = False
    ch.permissions_for.return_value = MagicMock(**perms)
    g = MagicMock()
    g.get_channel = MagicMock(return_value=ch)
    inter.guild = g
    pick(monkeypatch, 555)

    async def go():
        view = SettingsView(cog)
        sel = next(c for c in view.children if isinstance(c, ChannelSelect))
        await sel.callback(inter)

    run(go())
    assert stored(db) == {}
    assert_private(inter)


def test_channel_select_with_all_permissions_saves(db, monkeypatch):
    cog = make_cog(db)
    inter = make_inter()
    ch = text_channel(555)
    ch.permissions_for.return_value = MagicMock(
        view_channel=True, send_messages=True, embed_links=True
    )
    g = MagicMock()
    g.get_channel = MagicMock(return_value=ch)
    inter.guild = g
    pick(monkeypatch, 555)

    async def go():
        sel = next(c for c in SettingsView(cog).children if isinstance(c, ChannelSelect))
        await sel.callback(inter)

    run(go())
    assert stored(db) == {KEY_NOTIFY_CHANNEL: "555"}


def test_concurrent_toggles_are_serialized(db):
    cog = make_cog(db)

    async def go():
        view = SettingsView(cog)
        assert isinstance(view.toggle_lock, asyncio.Lock)
        btn = find(view, "空の日スキップ切替")
        await asyncio.gather(*(btn.callback(make_inter()) for _ in range(4)))
        assert not view.toggle_lock.locked()

    run(go())
    # four flips from the default (False) end where they started: no lost update
    assert stored(db) == {KEY_SKIP_EMPTY: "false"}
