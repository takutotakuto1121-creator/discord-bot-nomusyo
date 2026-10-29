"""``/status``: uptime and version info (allowed users only, ephemeral)."""

from __future__ import annotations

import math
import platform
import time

import discord
from discord import app_commands
from discord.ext import commands

from nomusyo.bot.app import NomusyoBot, get_version


def format_uptime(seconds: float) -> str:
    total = int(max(seconds, 0))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}日")
    if days or hours:
        parts.append(f"{hours}時間")
    parts.append(f"{minutes}分{secs}秒")
    return "".join(parts)


def build_status_text(
    *, uptime_seconds: float, version: str, latency: float, guild_count: int
) -> str:
    latency_text = "-" if math.isnan(latency) or math.isinf(latency) else f"{latency * 1000:.0f}ms"
    return "\n".join(
        [
            "**NomusyoBot 状態**",
            f"稼働時間: {format_uptime(uptime_seconds)}",
            f"バージョン: {version}",
            f"discord.py: {discord.__version__} / Python {platform.python_version()}",
            f"Gateway 遅延: {latency_text}",
            f"参加サーバー数: {guild_count}",
        ]
    )


class AdminCog(commands.Cog):
    def __init__(self, bot: NomusyoBot) -> None:
        self.bot = bot

    @app_commands.command(name="status", description="Bot の稼働状態を表示します")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def status(self, interaction: discord.Interaction) -> None:
        text = build_status_text(
            uptime_seconds=time.monotonic() - self.bot.started_at,
            version=get_version(),
            latency=self.bot.latency,
            guild_count=len(self.bot.guilds),
        )
        await interaction.response.send_message(text, ephemeral=True)


async def setup(bot: NomusyoBot) -> None:
    await bot.add_cog(AdminCog(bot))
