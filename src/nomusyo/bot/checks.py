"""Allowed-user verification shared by slash commands, buttons and modals.

* Slash commands / context menus: ``AllowedCommandTree`` (global ``interaction_check``).
* Buttons / selects: subclass ``AllowedView``.
* Modals: subclass ``AllowedModal`` (checked via ``interaction_check`` before ``on_submit``).

Denied users get an ephemeral refusal; only the user ID is logged, never message content.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Collection
from typing import Any, TypeVar

import discord
from discord import app_commands

log = logging.getLogger(__name__)

DENIED_MESSAGE = "このBotを操作する権限がありません。"
ERROR_REPLY = "内部エラーが発生しました。管理者に通知しました。"
_T = TypeVar("_T")


async def run_db(func: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
    """Run a blocking (SQLite) call in a worker thread so the event loop is not stalled."""
    return await asyncio.to_thread(func, *args, **kwargs)


async def report_ui_error(
    interaction: discord.Interaction, error: BaseException, where: str
) -> None:
    """Shared handler for View/Modal errors: type name + location only, ephemeral reply, admin DM.

    Never logs the traceback or the exception message (they could contain secrets).
    """
    from nomusyo.bot.app import describe_error, describe_location

    name = describe_error(error)
    log.error("%s failed (%s at %s)", where, name, describe_location(error))
    try:
        if interaction.response.is_done():
            await interaction.followup.send(ERROR_REPLY, ephemeral=True)
        else:
            await interaction.response.send_message(ERROR_REPLY, ephemeral=True)
    except discord.HTTPException as exc:
        log.warning("Could not send error reply (%s)", type(exc).__name__)
    notify = getattr(interaction.client, "notify_admin", None)
    if notify is not None:
        await notify(f"[NomusyoBot] {where} で未処理の例外: {name}")


# Denial logs are throttled per user_id so a denied user cannot flood the log.
DENIED_LOG_INTERVAL = 300.0
_DENIED_LOG_MAX_ENTRIES = 1024
_denied_last_logged: dict[int | None, float] = {}


def _should_log_denial(user_id: int | None) -> bool:
    now = time.monotonic()
    last = _denied_last_logged.get(user_id)
    if last is not None and now - last < DENIED_LOG_INTERVAL:
        return False
    if len(_denied_last_logged) >= _DENIED_LOG_MAX_ENTRIES:
        _denied_last_logged.clear()
    _denied_last_logged[user_id] = now
    return True


def is_allowed(user_id: int, allowed_ids: Collection[int]) -> bool:
    return user_id in allowed_ids


async def ensure_allowed(interaction: discord.Interaction, allowed_ids: Collection[int]) -> bool:
    """Return True if the interaction's user is allowed; otherwise refuse ephemerally.

    Autocomplete interactions cannot receive a message response, so they are refused (False)
    without responding.
    """
    user = interaction.user
    if user is not None and is_allowed(user.id, allowed_ids):
        return True
    user_id = getattr(user, "id", None)
    if _should_log_denial(user_id):
        log.warning("Denied interaction from user_id=%s", user_id)
    if interaction.type is discord.InteractionType.autocomplete:
        return False
    try:
        if interaction.response.is_done():
            await interaction.followup.send(DENIED_MESSAGE, ephemeral=True)
        else:
            await interaction.response.send_message(DENIED_MESSAGE, ephemeral=True)
    except discord.HTTPException as exc:
        log.warning("Could not send denial response (%s)", type(exc).__name__)
    return False


class AllowedCommandTree(app_commands.CommandTree):
    """CommandTree whose global check applies to every slash command and context menu."""

    def __init__(self, client: discord.Client, *, allowed_user_ids: Collection[int], **kwargs):
        super().__init__(client, **kwargs)
        self.allowed_user_ids = frozenset(allowed_user_ids)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await ensure_allowed(interaction, self.allowed_user_ids)


class AllowedView(discord.ui.View):
    """Base View: every button/select interaction is checked against the allowed users."""

    def __init__(self, allowed_user_ids: Collection[int], *, timeout: float | None = 180):
        super().__init__(timeout=timeout)
        self.allowed_user_ids = frozenset(allowed_user_ids)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await ensure_allowed(interaction, self.allowed_user_ids)

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        await report_ui_error(interaction, error, f"View {type(self).__name__}")


class AllowedModal(discord.ui.Modal):
    """Base Modal: submission is verified by ``interaction_check`` before ``on_submit`` runs."""

    def __init__(self, allowed_user_ids: Collection[int], *, title: str, timeout: float = 300):
        super().__init__(title=title, timeout=timeout)
        self.allowed_user_ids = frozenset(allowed_user_ids)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await ensure_allowed(interaction, self.allowed_user_ids)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await report_ui_error(interaction, error, f"Modal {type(self).__name__}")
