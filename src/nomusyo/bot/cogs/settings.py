"""``/settings``: show and change user settings (ephemeral, no mentions).

Validation and persistence live in ``nomusyo.schedule.settings``; ``DigestCog`` reads the same
rows on every tick, so changes apply from the next tick. Logs carry exception type names only.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Collection
from datetime import UTC, datetime

import discord
from discord import app_commands
from discord.ext import commands

from nomusyo.bot.app import NomusyoBot
from nomusyo.bot.checks import AllowedModal, AllowedView
from nomusyo.schedule.errors import ScheduleError
from nomusyo.schedule.settings import (
    KEY_DEFAULT_REMIND,
    KEY_DEFAULT_REMIND_TODO,
    KEY_DIGEST_TIME,
    KEY_NOTIFY_CHANNEL,
    KEY_NOTIFY_DM,
    KEY_SKIP_EMPTY,
    UserSettings,
    load_user_settings,
    set_setting,
)
from nomusyo.storage.db import Database

log = logging.getLogger(__name__)

NO_MENTIONS = discord.AllowedMentions.none()
VIEW_TIMEOUT = 180
MSG_FAILED = "設定の保存に失敗しました。しばらくしてからやり直してください。"
MSG_BAD_CHANNEL = "このサーバーのテキストチャンネルで、Bot が送信できる場所を指定してください。"


def _onoff(value: bool) -> str:
    return "オン" if value else "オフ"


def settings_embed(s: UserSettings) -> discord.Embed:
    e = discord.Embed(title="設定")
    e.add_field(name="ダイジェスト時刻", value=f"{s.digest_time:%H:%M}", inline=False)
    e.add_field(
        name="予定なしの日はダイジェストを出さない", value=_onoff(s.skip_empty), inline=False
    )
    e.add_field(name="既定リマインド(予定)", value=s.default_remind or "なし", inline=False)
    e.add_field(name="既定リマインド(TODO)", value=s.default_remind_todo or "なし", inline=False)
    e.add_field(name="通知チャンネル", value=f"<#{s.channel_id}>", inline=False)
    e.add_field(name="DM 通知", value=_onoff(s.notify_dm), inline=False)
    return e


class SettingsCog(commands.Cog):
    def __init__(
        self,
        bot: NomusyoBot,
        allowed_user_ids: Collection[int],
    ) -> None:
        self.bot = bot
        self.allowed_user_ids = frozenset(allowed_user_ids)
        self.db: Database = bot.db  # type: ignore[assignment]

    def load(self) -> UserSettings:
        cfg = self.bot.config
        return load_user_settings(
            self.db,
            cfg.owner_user_id,
            default_time=cfg.digest_time,
            default_channel_id=cfg.notify_channel_id,
        )

    def save(self, key: str, raw: str) -> str:
        return set_setting(self.db, self.bot.config.owner_user_id, key, raw, self._now())

    def _now(self):
        return datetime.now(UTC)

    async def apply(
        self, interaction: discord.Interaction, key: str, raw: str, view: SettingsView | None
    ) -> None:
        """Validate+save, then answer ephemerally. Invalid values are refused without writing."""
        try:
            await asyncio.to_thread(self.save, key, raw)
            current = await asyncio.to_thread(self.load)
        except ScheduleError as exc:
            await _reply(interaction, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - always answer the user
            log.warning("Settings save failed (%s)", type(exc).__name__)
            await _reply(interaction, MSG_FAILED)
            return
        await _reply(
            interaction, "設定を保存しました。次回の判定から反映されます。", settings_embed(current)
        )

    @app_commands.command(name="settings", description="設定を表示・変更します")
    @app_commands.guild_only()
    async def settings(self, interaction: discord.Interaction) -> None:
        try:
            current = await asyncio.to_thread(self.load)
        except Exception as exc:  # noqa: BLE001
            log.warning("Settings load failed (%s)", type(exc).__name__)
            await _reply(interaction, "設定の取得に失敗しました。")
            return
        view = SettingsView(self)
        await interaction.response.send_message(
            embed=settings_embed(current), view=view, ephemeral=True, allowed_mentions=NO_MENTIONS
        )
        view.message = await interaction.original_response()


async def _reply(
    interaction: discord.Interaction, text: str, embed: discord.Embed | None = None
) -> None:
    kwargs = {"ephemeral": True, "allowed_mentions": NO_MENTIONS}
    if embed is not None:
        kwargs["embed"] = embed
    if interaction.response.is_done():
        await interaction.followup.send(text, **kwargs)
    else:
        await interaction.response.send_message(text, **kwargs)


class ValueModal(AllowedModal):
    def __init__(self, cog: SettingsCog, key: str, title: str, label: str, placeholder: str):
        super().__init__(cog.allowed_user_ids, title=title)
        self.cog = cog
        self.key = key
        self.value = discord.ui.TextInput(
            label=label, placeholder=placeholder, max_length=100, required=key == KEY_DIGEST_TIME
        )
        self.add_item(self.value)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.apply(interaction, self.key, self.value.value, None)


class ChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, cog: SettingsCog) -> None:
        super().__init__(
            placeholder="通知チャンネルを選択",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        picked = self.values[0]
        channel = interaction.guild.get_channel(picked.id) if interaction.guild else None
        me = interaction.guild.me if interaction.guild else None
        ok = (
            isinstance(channel, discord.TextChannel)
            and me is not None
            and channel.permissions_for(me).view_channel
            and channel.permissions_for(me).send_messages
            and channel.permissions_for(me).embed_links
        )
        if not ok:
            await _reply(interaction, MSG_BAD_CHANNEL)
            return
        await self.cog.apply(interaction, KEY_NOTIFY_CHANNEL, str(channel.id), None)


class SettingsView(AllowedView):
    def __init__(self, cog: SettingsCog) -> None:
        super().__init__(cog.allowed_user_ids, timeout=VIEW_TIMEOUT)
        self.cog = cog
        self.message: discord.Message | None = None
        self.toggle_lock = asyncio.Lock()  # serialize read-modify-write toggles
        self.add_item(ChannelSelect(cog))

    async def on_timeout(self) -> None:
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="ダイジェスト時刻", style=discord.ButtonStyle.primary)
    async def digest_time(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            ValueModal(self.cog, KEY_DIGEST_TIME, "ダイジェスト時刻", "HH:MM", "06:55")
        )

    @discord.ui.button(label="既定リマインド(予定)", style=discord.ButtonStyle.primary)
    async def remind(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            ValueModal(
                self.cog, KEY_DEFAULT_REMIND, "既定リマインド(予定)", "例 10m,1h(空でなし)", "10m"
            )
        )

    @discord.ui.button(label="既定リマインド(TODO)", style=discord.ButtonStyle.primary)
    async def remind_todo(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            ValueModal(
                self.cog,
                KEY_DEFAULT_REMIND_TODO,
                "既定リマインド(TODO)",
                "例 1d@20:00(空でなし)",
                "",
            )
        )

    @discord.ui.button(label="空の日スキップ切替", style=discord.ButtonStyle.secondary)
    async def toggle_skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._toggle(interaction, KEY_SKIP_EMPTY, "skip_empty")

    @discord.ui.button(label="DM通知切替", style=discord.ButtonStyle.secondary)
    async def toggle_dm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._toggle(interaction, KEY_NOTIFY_DM, "notify_dm")

    async def _toggle(self, interaction: discord.Interaction, key: str, attr: str) -> None:
        async with self.toggle_lock:
            await self._toggle_locked(interaction, key, attr)

    async def _toggle_locked(self, interaction: discord.Interaction, key: str, attr: str) -> None:
        try:
            current = await asyncio.to_thread(self.cog.load)
        except Exception as exc:  # noqa: BLE001
            log.warning("Settings load failed (%s)", type(exc).__name__)
            await _reply(interaction, MSG_FAILED)
            return
        await self.cog.apply(interaction, key, "false" if getattr(current, attr) else "true", self)


async def setup(bot: NomusyoBot) -> None:
    if bot.db is None:
        raise RuntimeError("bot.db is not open (it is opened in NomusyoBot.setup_hook)")
    await bot.add_cog(SettingsCog(bot, bot.config.allowed_user_ids))
