"""``/today`` ``/week``: events and TODOs for today / the next 7 days (ephemeral, no mentions).

Aggregation lives in ``nomusyo.schedule.agenda``; this module only renders and replies.
Commands are checked by ``AllowedCommandTree``.
"""

from __future__ import annotations

import logging
from collections.abc import Collection

import discord
from discord import app_commands
from discord.ext import commands

from nomusyo.bot import embeds
from nomusyo.bot.app import NomusyoBot
from nomusyo.bot.checks import run_db
from nomusyo.schedule.agenda import Holiday, build_agenda
from nomusyo.schedule.errors import ScheduleError
from nomusyo.schedule.service import ScheduleService

log = logging.getLogger(__name__)

NO_MENTIONS = discord.AllowedMentions.none()
MSG_FAILED = "取得に失敗しました。しばらくしてからやり直してください。"


class AgendaCog(commands.Cog):
    def __init__(
        self,
        bot: NomusyoBot,
        service: ScheduleService,
        allowed_user_ids: Collection[int],
    ) -> None:
        self.bot = bot
        self.service = service
        self.allowed_user_ids = frozenset(allowed_user_ids)

    def now(self):
        return self.service.clock.now()

    def holidays(self) -> list[Holiday]:
        return []  # V2-62

    def render(self, days: int, heading: str) -> discord.Embed:
        agenda = build_agenda(self.service, days=days, holidays=self.holidays())
        return embeds.agenda_embed(agenda, heading=heading, now=self.now())

    async def _send(self, interaction: discord.Interaction, days: int, heading: str) -> None:
        try:
            embed = await run_db(self.render, days, heading)
        except ScheduleError as exc:
            text = str(exc)
        except Exception as exc:  # noqa: BLE001 - always answer the user
            log.warning("Agenda failed (%s)", type(exc).__name__)
            text = MSG_FAILED
        else:
            await interaction.response.send_message(
                embed=embed, ephemeral=True, allowed_mentions=NO_MENTIONS
            )
            return
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS)

    @app_commands.command(name="today", description="今日の予定と TODO を表示します")
    @app_commands.guild_only()
    async def today(self, interaction: discord.Interaction) -> None:
        await self._send(interaction, 1, "今日の予定とTODO")

    @app_commands.command(name="week", description="今後7日間の予定と TODO を表示します")
    @app_commands.guild_only()
    async def week(self, interaction: discord.Interaction) -> None:
        await self._send(interaction, 7, "今後7日間の予定とTODO")


async def setup(bot: NomusyoBot) -> None:
    db = bot.db
    if db is None:
        raise RuntimeError("bot.db is not open (it is opened in NomusyoBot.setup_hook)")
    service = ScheduleService(db, bot.config.owner_user_id, default_tz=bot.config.tz_default)
    await bot.add_cog(AgendaCog(bot, service, bot.config.allowed_user_ids))
