import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands

from nomusyo.bot import checks
from nomusyo.bot.checks import (
    DENIED_MESSAGE,
    ERROR_REPLY,
    AllowedCommandTree,
    AllowedModal,
    AllowedView,
    ensure_allowed,
    is_allowed,
    run_db,
)

ALLOWED = {100, 200}


@pytest.fixture(autouse=True)
def _clear_denied_log_state():
    checks._denied_last_logged.clear()
    yield
    checks._denied_last_logged.clear()


def make_interaction(user_id: int | None, *, done: bool = False):
    inter = MagicMock()
    if user_id is None:
        inter.user = None
    else:
        inter.user.id = user_id
    inter.type = discord.InteractionType.application_command
    inter.response.is_done.return_value = done
    inter.response.send_message = AsyncMock()
    inter.followup.send = AsyncMock()
    return inter


def run(coro):
    return asyncio.run(coro)


def test_is_allowed():
    assert is_allowed(100, ALLOWED)
    assert not is_allowed(999, ALLOWED)
    assert not is_allowed(100, set())
    assert not is_allowed(100, frozenset())


def test_ensure_allowed_true_sends_nothing():
    inter = make_interaction(100)
    assert run(ensure_allowed(inter, ALLOWED)) is True
    inter.response.send_message.assert_not_called()
    inter.followup.send.assert_not_called()


def test_ensure_allowed_denied_ephemeral_response():
    inter = make_interaction(999)
    assert run(ensure_allowed(inter, ALLOWED)) is False
    inter.response.send_message.assert_awaited_once_with(DENIED_MESSAGE, ephemeral=True)
    inter.followup.send.assert_not_called()


def test_ensure_allowed_denied_uses_followup_when_already_responded():
    inter = make_interaction(999, done=True)
    assert run(ensure_allowed(inter, ALLOWED)) is False
    inter.followup.send.assert_awaited_once_with(DENIED_MESSAGE, ephemeral=True)
    inter.response.send_message.assert_not_called()


def test_ensure_allowed_user_none_is_denied():
    inter = make_interaction(None)
    assert run(ensure_allowed(inter, ALLOWED)) is False
    inter.response.send_message.assert_awaited_once()


def test_ensure_allowed_denied_even_if_send_fails():
    inter = make_interaction(999)
    inter.response.send_message.side_effect = discord.HTTPException(
        MagicMock(status=500, reason="x"), "boom"
    )
    assert run(ensure_allowed(inter, ALLOWED)) is False


def test_ensure_allowed_logs_user_id_only(caplog):
    inter = make_interaction(999)
    with caplog.at_level("WARNING"):
        run(ensure_allowed(inter, ALLOWED))
    assert "999" in caplog.text


def test_command_tree_interaction_check():
    async def go():
        tree = AllowedCommandTree(
            discord.Client(intents=discord.Intents.default()), allowed_user_ids=ALLOWED
        )
        assert tree.allowed_user_ids == frozenset(ALLOWED)
        ok = make_interaction(100)
        ng = make_interaction(5)
        assert await tree.interaction_check(ok) is True
        assert await tree.interaction_check(ng) is False
        ng.response.send_message.assert_awaited_once_with(DENIED_MESSAGE, ephemeral=True)

    run(go())


def test_command_tree_is_a_commandtree():
    assert issubclass(AllowedCommandTree, app_commands.CommandTree)


def test_view_interaction_check():
    async def go():
        view = AllowedView(ALLOWED)
        assert await view.interaction_check(make_interaction(200)) is True
        ng = make_interaction(5)
        assert await view.interaction_check(ng) is False
        ng.response.send_message.assert_awaited_once_with(DENIED_MESSAGE, ephemeral=True)
        done = make_interaction(5, done=True)
        assert await view.interaction_check(done) is False
        done.followup.send.assert_awaited_once_with(DENIED_MESSAGE, ephemeral=True)

    run(go())


def test_modal_interaction_check_allowed():
    async def go():
        modal = AllowedModal(ALLOWED, title="t")
        inter = make_interaction(100)
        assert await modal.interaction_check(inter) is True
        inter.response.send_message.assert_not_called()

    run(go())


def test_modal_interaction_check_denied_ephemeral():
    async def go():
        modal = AllowedModal(ALLOWED, title="t")
        inter = make_interaction(5)
        assert await modal.interaction_check(inter) is False
        inter.response.send_message.assert_awaited_once_with(DENIED_MESSAGE, ephemeral=True)

    run(go())


def test_modal_no_handle_submit_anymore():
    assert not hasattr(AllowedModal, "handle_submit")


def test_ensure_allowed_autocomplete_denied_without_response():
    inter = make_interaction(999)
    inter.type = discord.InteractionType.autocomplete
    assert run(ensure_allowed(inter, ALLOWED)) is False
    inter.response.send_message.assert_not_called()
    inter.followup.send.assert_not_called()


def test_denied_log_throttled_per_user(caplog):
    with caplog.at_level("WARNING"):
        run(ensure_allowed(make_interaction(999), ALLOWED))
        run(ensure_allowed(make_interaction(999), ALLOWED))
        run(ensure_allowed(make_interaction(998), ALLOWED))
    assert caplog.text.count("user_id=999") == 1
    assert caplog.text.count("user_id=998") == 1


def test_empty_allowlist_denies_everything():
    async def go():
        view = AllowedView(set())
        assert await view.interaction_check(make_interaction(100)) is False

    run(go())


SECRET = "sk-ant-SECRET-VALUE"


def with_client(inter):
    inter.client.notify_admin = AsyncMock()
    return inter


def check_error_reply(inter, caplog, where):
    inter.response.send_message.assert_awaited_once_with(ERROR_REPLY, ephemeral=True)
    inter.client.notify_admin.assert_awaited_once()
    note = inter.client.notify_admin.await_args.args[0]
    assert "RuntimeError" in caplog.text and where in caplog.text
    for out in (caplog.text, note, ERROR_REPLY):
        assert SECRET not in out


def test_view_on_error_ephemeral_no_message_and_notifies_admin(caplog):
    inter = with_client(make_interaction(100))

    async def go():
        view = AllowedView(ALLOWED)
        await view.on_error(inter, RuntimeError(f"boom {SECRET}"), MagicMock())

    with caplog.at_level(logging.DEBUG):
        run(go())
    check_error_reply(inter, caplog, "View AllowedView")


def test_modal_on_error_ephemeral_no_message_and_notifies_admin(caplog):
    inter = with_client(make_interaction(100))

    async def go():
        modal = AllowedModal(ALLOWED, title="t")
        await modal.on_error(inter, RuntimeError(f"boom {SECRET}"))

    with caplog.at_level(logging.DEBUG):
        run(go())
    check_error_reply(inter, caplog, "Modal AllowedModal")


def test_on_error_uses_followup_when_already_responded():
    inter = with_client(make_interaction(100, done=True))
    run(AllowedView(ALLOWED).on_error(inter, RuntimeError("x"), MagicMock()))
    inter.followup.send.assert_awaited_once_with(ERROR_REPLY, ephemeral=True)
    inter.response.send_message.assert_not_called()
    inter.client.notify_admin.assert_awaited_once()


def test_on_error_survives_reply_failure_and_still_notifies():
    inter = with_client(make_interaction(100))
    resp = MagicMock(status=500, reason="x")
    inter.response.send_message = AsyncMock(side_effect=discord.HTTPException(resp, "fail"))
    run(AllowedModal(ALLOWED, title="t").on_error(inter, RuntimeError("x")))
    inter.client.notify_admin.assert_awaited_once()


def test_on_error_without_notify_admin_does_not_raise():
    inter = make_interaction(100)
    inter.client = object()
    run(AllowedModal(ALLOWED, title="t").on_error(inter, RuntimeError("x")))
    inter.response.send_message.assert_awaited_once_with(ERROR_REPLY, ephemeral=True)


def test_run_db_runs_in_worker_thread_and_returns():
    import threading

    def work(a, b=0):
        return (a + b, threading.get_ident())

    value, tid = run(run_db(work, 1, b=2))
    assert value == 3 and tid != threading.get_ident()


def test_run_db_propagates_exceptions():
    def boom():
        raise ValueError("x")

    with pytest.raises(ValueError):
        run(run_db(boom))
