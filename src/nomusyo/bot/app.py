"""Bot subclass: setup_hook, guild command sync, error handling and admin notification."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import traceback
from collections.abc import Iterable
from importlib import metadata

import discord
from discord import app_commands
from discord.ext import commands

from nomusyo.bot.checks import ERROR_REPLY, AllowedCommandTree
from nomusyo.bot.config import BotConfig
from nomusyo.storage.db import Database
from nomusyo.storage.errors import InstanceLockError
from nomusyo.storage.lock import (
    heartbeat_instance_lock,
    new_holder_id,
    open_database_with_lock,
    release_instance_lock,
)

log = logging.getLogger(__name__)

# Third-party loggers that can emit request URLs / payloads (webhook or token material) at DEBUG.
QUIET_LOGGERS = (
    "urllib3",
    "httpx",
    "httpcore",
    "anthropic",
    "requests",
    "aiohttp",
    "discord.http",
    "discord.gateway",
)
ALLOWED_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
ADMIN_NOTIFY_MIN_INTERVAL = 60.0
# Secrets shorter than this are not redacted: replacing e.g. "a" everywhere would wreck the log.
MIN_REDACT_LENGTH = 8
REDACTED = "***"

COG_MODULES = (
    "nomusyo.bot.cogs.news",
    "nomusyo.bot.cogs.admin",
    "nomusyo.bot.cogs.schedule",
    "nomusyo.bot.cogs.todo",
    "nomusyo.bot.cogs.agenda",
    "nomusyo.bot.cogs.digest",
    "nomusyo.bot.cogs.settings",
)
HEARTBEAT_COG = "nomusyo.bot.cogs.heartbeat"
ALIVE_COG = "nomusyo.bot.cogs.alive"
LOCK_HEARTBEAT_SECONDS = 30
# 3 consecutive failures (~90 s) is close to the 120 s stale limit: assume the lock is gone.
LOCK_HEARTBEAT_MAX_FAILURES = 3


def get_version() -> str:
    try:
        return metadata.version("nomusyo")
    except metadata.PackageNotFoundError:
        return "unknown"


def collect_secrets(config: BotConfig) -> tuple[str, ...]:
    """Known secret values (bot token, heartbeat URL, Anthropic API key) for log redaction."""
    return (
        config.discord_bot_token,
        config.heartbeat_url,
        config.news.anthropic_api_key,
        config.news.discord_webhook_url,
    )


class RedactingFilter(logging.Filter):
    """Replace known secret values with ``***`` in the final message and the traceback text."""

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        unique = {s for s in secrets if s and len(s) >= MIN_REDACT_LENGTH}
        # Longest first so that a secret containing another one is replaced as a whole.
        self._secrets = tuple(sorted(unique, key=len, reverse=True))

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - bad format args must not break logging
            message = str(record.msg)
        record.msg = self.redact(message)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self.redact(record.exc_text)
        if record.stack_info:
            record.stack_info = self.redact(record.stack_info)
        return True


def configure_logging(level_name: str, secrets: Iterable[str] = ()) -> None:
    name = (level_name or "").strip().upper()
    level = getattr(logging, name) if name in ALLOWED_LOG_LEVELS else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    redactor = RedactingFilter(secrets)
    for handler in logging.getLogger().handlers:
        # Handler-level filters see every propagated record (logger-level ones would not).
        for old in [f for f in handler.filters if isinstance(f, RedactingFilter)]:
            handler.removeFilter(old)
        handler.addFilter(redactor)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def describe_error(error: BaseException) -> str:
    """Type name only: exception messages could contain tokens or URLs."""
    return type(error).__name__


def describe_location(error: BaseException) -> str:
    """``file.py:LINE:function`` of the innermost traceback frame (no message, no path)."""
    tb = error.__traceback__
    if tb is None:
        return "unknown"
    frame = traceback.extract_tb(tb)[-1]
    return f"{os.path.basename(frame.filename)}:{frame.lineno}:{frame.name}"


class NomusyoBot(commands.Bot):
    def __init__(self, config: BotConfig, *, intents: discord.Intents | None = None) -> None:
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents or _default_intents(),
            tree_cls=_tree_factory(config),
            allowed_mentions=discord.AllowedMentions.none(),
            help_command=None,
        )
        self.config = config
        self.started_at = time.monotonic()
        self.db: Database | None = None  # opened in setup_hook, closed in close()
        self.lock_holder = new_holder_id()
        self.lock_lost = False  # set when the instance lock was lost (exit code non-zero)
        self._lock_held = False
        self._lock_task: asyncio.Task[None] | None = None
        self._last_admin_notice = float("-inf")
        self._suppressed_notices = 0
        self.tree.on_error = self.on_tree_error  # type: ignore[method-assign]

    # -- lifecycle ---------------------------------------------------------------------
    async def setup_hook(self) -> None:
        if self.db is None:
            try:
                # Lock first, migrate after (resident-bot.md 5.3 (b)).
                self.db = await asyncio.to_thread(
                    open_database_with_lock, self.config.db_path, self.lock_holder
                )
                self._lock_held = True
            except InstanceLockError:
                log.error(
                    "Another instance is already running on %s (InstanceLockError);"
                    " exiting. After a crash, retry once the old heartbeat is stale (~2 min)",
                    self.config.db_path,
                )
                raise
            except Exception as exc:
                # Fail startup loudly; type name and path only (never the message).
                log.error(
                    "Cannot open database %s (%s); aborting startup",
                    self.config.db_path,
                    type(exc).__name__,
                )
                raise
            self._lock_task = asyncio.create_task(self._lock_heartbeat_loop())
        for module in COG_MODULES:
            await self.load_extension(module)
        await self.load_extension(ALIVE_COG)
        if self.config.heartbeat_url:
            await self.load_extension(HEARTBEAT_COG)
        guild = discord.Object(id=self.config.guild_id)
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        log.info("Synced %d guild command(s)", len(synced))

    async def _lock_heartbeat_loop(self) -> None:
        """Refresh the instance lock; independent of the gateway state (``_connected``)."""
        failures = 0
        while not self.is_closed():
            await asyncio.sleep(LOCK_HEARTBEAT_SECONDS)
            db = self.db
            if db is None:
                return
            try:
                ok = await asyncio.to_thread(heartbeat_instance_lock, db, self.lock_holder)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                log.warning("Instance lock heartbeat failed (%s)", type(exc).__name__)
                ok = failures < LOCK_HEARTBEAT_MAX_FAILURES
            else:
                failures = 0
            if not ok:
                log.error("Instance lock lost; shutting down to avoid duplicate instances")
                self.lock_lost = True
                self._lock_held = False  # do not delete a row someone else may now own
                await self.close()
                return

    async def close(self) -> None:
        task, self._lock_task = self._lock_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        try:
            await super().close()
        finally:
            db, self.db = self.db, None
            if db is not None:
                if self._lock_held:
                    self._lock_held = False
                    try:
                        await asyncio.to_thread(release_instance_lock, db, self.lock_holder)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("Failed to release instance lock (%s)", type(exc).__name__)
                try:
                    await asyncio.to_thread(db.close)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Failed to close database (%s)", type(exc).__name__)

    async def on_ready(self) -> None:
        # May fire several times (reconnects); do initialization in setup_hook instead.
        log.info("Bot ready (guilds=%d)", len(self.guilds))
        left = [g for g in list(self.guilds) if await self._leave_if_foreign(g)]
        if left:
            await self.notify_admin(
                f"[NomusyoBot] 想定外のサーバー {len(left)} 件から退出しました: "
                + ", ".join(str(g.id) for g in left[:10])
            )

    async def on_guild_join(self, guild: discord.Guild) -> None:
        if await self._leave_if_foreign(guild):
            await self.notify_admin(
                f"[NomusyoBot] 想定外のサーバー(ID: {guild.id})に参加したため退出しました"
            )

    async def _leave_if_foreign(self, guild: discord.Guild) -> bool:
        """Leave any guild other than the configured one. Returns True if it was left."""
        if guild.id == self.config.guild_id:
            return False
        try:
            await guild.leave()
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to leave unexpected guild %s (%s)", guild.id, type(exc).__name__)
            return False
        log.warning("Left unexpected guild %s", guild.id)
        return True

    # -- error handling ----------------------------------------------------------------
    async def notify_admin(self, text: str) -> None:
        """DM the admin (rate-limited). Never include exception messages in ``text``."""
        now = time.monotonic()
        if now - self._last_admin_notice < ADMIN_NOTIFY_MIN_INTERVAL:
            self._suppressed_notices += 1
            return
        suppressed = self._suppressed_notices
        if suppressed:
            text = f"{text}\n(他 {suppressed} 件を抑制)"
        try:
            user = await self.fetch_user(self.config.admin_user_id)
            await user.send(text)
            # Only a successful send starts the rate-limit window (failures can be retried).
            self._last_admin_notice = now
            self._suppressed_notices = max(0, self._suppressed_notices - suppressed)
        except Exception as exc:  # noqa: BLE001 - notification must never raise
            log.warning("Failed to notify admin (%s)", type(exc).__name__)

    async def on_error(self, event_method: str, /, *args, **kwargs) -> None:
        error = sys.exc_info()[1]
        name = describe_error(error) if error else "unknown"
        where = describe_location(error) if error else "unknown"
        # No exc_info: the exception message / traceback text could contain secrets.
        log.error("Unhandled error in event %s (%s at %s)", event_method, name, where)
        await self.notify_admin(f"[NomusyoBot] イベント {event_method} で未処理の例外: {name}")

    async def on_tree_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            return  # refusal already sent by the allowed-user check
        original = getattr(error, "original", error)
        name = describe_error(original)
        command = interaction.command.qualified_name if interaction.command else "?"
        where = describe_location(original)
        log.error("Command /%s failed (%s at %s)", command, name, where)
        try:
            if interaction.response.is_done():
                # After a public defer (/news) Discord ignores ephemeral for the first followup
                # (it replaces the "thinking" message); later followups are ephemeral.
                await interaction.followup.send(ERROR_REPLY, ephemeral=True)
            else:
                await interaction.response.send_message(ERROR_REPLY, ephemeral=True)
        except discord.HTTPException as exc:
            log.warning("Could not send error reply (%s)", type(exc).__name__)
        await self.notify_admin(f"[NomusyoBot] /{command} で未処理の例外: {name}")


def _default_intents() -> discord.Intents:
    """Non-privileged minimum: guild state only (no members/presences/message content)."""
    intents = discord.Intents.none()
    intents.guilds = True
    return intents


def _tree_factory(config: BotConfig):
    def factory(client: discord.Client) -> AllowedCommandTree:
        return AllowedCommandTree(client, allowed_user_ids=config.allowed_user_ids)

    return factory
