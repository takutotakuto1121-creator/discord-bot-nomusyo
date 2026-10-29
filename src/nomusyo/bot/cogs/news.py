"""``/news``: run the existing pipeline on demand and post the digest to the channel."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from nomusyo.bot.app import NomusyoBot
from nomusyo.config import Config
from nomusyo.core.pipeline import NoArticlesError, run_pipeline
from nomusyo.discord_io.formatter import format_payloads

log = logging.getLogger(__name__)

COOLDOWN_SECONDS = 300.0
INTER_MESSAGE_DELAY = 1.0

MSG_BUSY = "ニュースを生成中です。完了までお待ちください。"
MSG_COOLDOWN = "連続実行はできません。あと {remaining} 秒お待ちください。"
MSG_NO_ARTICLES = "記事を取得できませんでした。時間をおいて再度お試しください。"
MSG_SUMMARY_ERROR = "要約に失敗したため RSS 概要で投稿しました。管理者に通知しました。"


def payload_to_kwargs(payload: dict[str, Any]) -> dict[str, Any]:
    """Convert a ``format_payloads`` dict into ``send`` keyword arguments."""
    return {
        "content": payload.get("content") or discord.utils.MISSING,
        "embeds": [discord.Embed.from_dict(e) for e in payload.get("embeds", [])],
        "allowed_mentions": discord.AllowedMentions.none(),
    }


class NewsCog(commands.Cog):
    def __init__(
        self,
        bot: NomusyoBot,
        news_config: Config,
        *,
        pipeline: Callable[..., Any] = run_pipeline,
        formatter: Callable[..., list[dict[str, Any]]] = format_payloads,
        monotonic: Callable[[], float] = time.monotonic,
        cooldown_seconds: float = COOLDOWN_SECONDS,
        message_delay: float = INTER_MESSAGE_DELAY,
    ) -> None:
        self.bot = bot
        self.news_config = news_config
        self._pipeline = pipeline
        self._formatter = formatter
        self._monotonic = monotonic
        self._cooldown = cooldown_seconds
        self._message_delay = message_delay
        self._lock = asyncio.Lock()
        self._last_started: float | None = None

    def cooldown_remaining(self) -> float:
        if self._last_started is None:
            return 0.0
        return max(0.0, self._cooldown - (self._monotonic() - self._last_started))

    @app_commands.command(name="news", description="今日のニュースを要約して投稿します")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def news(self, interaction: discord.Interaction) -> None:
        if self._lock.locked():
            await interaction.response.send_message(MSG_BUSY, ephemeral=True)
            return
        remaining = self.cooldown_remaining()
        if remaining > 0:
            await interaction.response.send_message(
                MSG_COOLDOWN.format(remaining=int(remaining) + 1), ephemeral=True
            )
            return
        # No await between the check above and acquire(), so only one command gets in.
        async with self._lock:
            await interaction.response.defer(thinking=True)
            # Start the cooldown only once the command is actually running (defer succeeded).
            self._last_started = self._monotonic()
            await self._run(interaction)

    async def _run(self, interaction: discord.Interaction) -> None:
        try:
            articles = await asyncio.to_thread(self._pipeline, self.news_config)
        except NoArticlesError:
            # The public "thinking" message cannot become ephemeral: replace it instead.
            await interaction.edit_original_response(content=MSG_NO_ARTICLES)
            return
        payloads = self._formatter(articles)
        summary_error = getattr(articles, "summary_error", None)
        for i, payload in enumerate(payloads):
            if i:
                await asyncio.sleep(self._message_delay)
            await interaction.followup.send(**payload_to_kwargs(payload))
        log.info("Posted %d news message(s)", len(payloads))
        if summary_error:
            # ``summary_error`` is the error class name set by the summarizer (no message text).
            log.error("Summarization failed permanently (%s)", summary_error)
            # ephemeral is ignored after a public defer, so this is a plain public notice.
            await interaction.followup.send(MSG_SUMMARY_ERROR)
            await self.bot.notify_admin("[NomusyoBot] /news: 要約が恒久的に失敗しました")


async def setup(bot: NomusyoBot) -> None:
    await bot.add_cog(NewsCog(bot, bot.config.news))
