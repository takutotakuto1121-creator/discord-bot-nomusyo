"""Morning digest: today's agenda posted once a day to the notify channel (separate from news).

Timing/decision logic is in ``nomusyo.schedule.digest``; idempotency via ``job_runs``.
Logs carry exception type names only.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import discord
from discord.ext import commands, tasks

from nomusyo.bot import embeds
from nomusyo.bot.app import NomusyoBot, describe_error
from nomusyo.schedule.agenda import build_agenda
from nomusyo.schedule.digest import (
    DIGEST_GRACE,
    JOB_DAILY_DIGEST,
    DigestSettings,
    digest_timing,
    load_digest_settings,
    should_skip_empty,
)
from nomusyo.schedule.jobs import (
    JOB_MAX_ATTEMPTS,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_SKIPPED,
    STATUS_STARTED,
    claim_job,
    finish_job,
    job_attempts,
    job_status,
    record_skipped,
)
from nomusyo.schedule.service import ScheduleService
from nomusyo.storage.db import Database

log = logging.getLogger(__name__)

NO_MENTIONS = discord.AllowedMentions.none()
TICK_SECONDS = 30
HEADING = "今日の予定とTODO"


class DigestCog(commands.Cog):
    def __init__(self, bot: NomusyoBot, service: ScheduleService) -> None:
        self.bot = bot
        self.service = service
        self.db: Database = bot.db  # type: ignore[assignment]

    async def cog_load(self) -> None:
        self.digest_loop.start()

    async def cog_unload(self) -> None:
        self.digest_loop.cancel()

    def settings(self) -> DigestSettings:
        cfg = self.bot.config
        return load_digest_settings(
            self.db,
            cfg.owner_user_id,
            default_time=cfg.digest_time,
            default_channel_id=cfg.notify_channel_id,
        )

    def _render(self) -> tuple[bool, discord.Embed]:
        agenda = build_agenda(self.service, days=1)
        return agenda.empty, embeds.agenda_embed(
            agenda, heading=HEADING, now=self.service.clock.now()
        )

    async def run_once(self) -> str:
        """One tick: returns ``wait|posted|skipped|failed|taken|expired|noop`` (for tests/logs)."""
        now = self.service.clock.now()
        st = await asyncio.to_thread(self.settings)
        timing = digest_timing(now, st.digest_time, self.bot.config.tz_default)
        if timing.state == "wait":
            return "wait"
        job, date = JOB_DAILY_DIGEST, timing.run_date
        if timing.state == "expired":
            if await asyncio.to_thread(record_skipped, self.db, job, date, now, "grace_exceeded"):
                log.warning("Digest skipped: past grace period (%s)", date)
                await self.bot.notify_admin(
                    f"[NomusyoBot] ダイジェストを猶予超過でスキップ: {date}"
                )
                return "expired"
            # A row left 'started' past the grace period is a crash trace. It may or may not
            # have posted, so it is never re-run (duplicate vs. missing: missing is preferred).
            status = await asyncio.to_thread(job_status, self.db, job, date)
            if status == STATUS_STARTED and await asyncio.to_thread(
                finish_job, self.db, job, date, STATUS_FAILED, now, "abandoned_started"
            ):
                log.error("Digest abandoned: left 'started' past grace (%s)", date)
                await self.bot.notify_admin(
                    f"[NomusyoBot] ダイジェスト({date})は実行中のまま中断された可能性があり、"
                    "二重投稿を避けるため再実行しませんでした。投稿済みか確認してください"
                )
                return "expired"
            return "noop"
        # stale_after beyond the grace period: a 'started' row is never re-claimed in time.
        if not await asyncio.to_thread(
            claim_job, self.db, job, date, now, stale_after=DIGEST_GRACE + timedelta(hours=1)
        ):
            return "taken"
        try:
            is_empty, embed = await asyncio.to_thread(self._render)
            if should_skip_empty(is_empty, st.skip_empty):
                await asyncio.to_thread(
                    finish_job, self.db, job, date, STATUS_SKIPPED, now, "empty", claimed_at=now
                )
                return "skipped"
            channel = await self._resolve_channel(st.channel_id)
            await channel.send(embed=embed, allowed_mentions=NO_MENTIONS)
        except asyncio.CancelledError:
            # Shutdown mid-run: record it (shielded) so a retry is possible, then propagate.
            await asyncio.shield(
                asyncio.to_thread(
                    finish_job,
                    self.db,
                    job,
                    date,
                    STATUS_FAILED,
                    self.service.clock.now(),
                    "cancelled",
                    claimed_at=now,
                )
            )
            raise
        except Exception as exc:  # noqa: BLE001 - recorded, never raised into the loop
            name = describe_error(exc)
            log.error("Digest failed (%s)", name)
            await asyncio.to_thread(
                finish_job,
                self.db,
                job,
                date,
                STATUS_FAILED,
                self.service.clock.now(),
                name[:60],
                claimed_at=now,
            )
            attempts = await asyncio.to_thread(job_attempts, self.db, job, date)
            if attempts >= JOB_MAX_ATTEMPTS:
                await self.bot.notify_admin(
                    f"[NomusyoBot] ダイジェスト投稿に失敗: {name}(再試行を諦めました: {date})"
                )
            else:
                await self.bot.notify_admin(f"[NomusyoBot] ダイジェスト投稿に失敗: {name}")
            return "failed"
        # Posted. Never re-send because recording failed: a warning is enough.
        try:
            await asyncio.to_thread(
                finish_job,
                self.db,
                job,
                date,
                STATUS_DONE,
                self.service.clock.now(),
                None,
                claimed_at=now,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Digest posted but recording DONE failed (%s)", describe_error(exc))
        return "posted"

    async def _resolve_channel(self, channel_id: int) -> discord.TextChannel:
        """The configured channel if it is a sendable text channel of our guild, else the env one
        (admin notified)."""
        cfg = self.bot.config
        candidates = [channel_id]
        if cfg.notify_channel_id != channel_id:
            candidates.append(cfg.notify_channel_id)
        for i, cid in enumerate(candidates):
            channel = self.bot.get_channel(cid)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(cid)
                except discord.HTTPException:
                    continue
            if self._sendable(channel):
                if i > 0:
                    log.warning("Digest channel invalid; falling back to the default channel")
                    await self.bot.notify_admin(
                        "[NomusyoBot] 設定された通知チャンネルが無効なため、"
                        "既定の通知チャンネルへ送信しました。/settings を確認してください"
                    )
                return channel  # type: ignore[return-value]
        raise RuntimeError("no_valid_digest_channel")

    def _sendable(self, channel: object) -> bool:
        if not isinstance(channel, discord.TextChannel):
            return False
        if channel.guild.id != self.bot.config.guild_id:
            return False
        perms = channel.permissions_for(channel.guild.me)
        return perms.view_channel and perms.send_messages and perms.embed_links

    @tasks.loop(seconds=TICK_SECONDS)
    async def digest_loop(self) -> None:
        try:
            await self.run_once()
        except Exception as exc:  # noqa: BLE001 - keep the loop alive
            log.error("Digest tick failed (%s)", describe_error(exc))

    @digest_loop.before_loop
    async def _before_digest(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: NomusyoBot) -> None:
    service = ScheduleService(bot.db, bot.config.owner_user_id, default_tz=bot.config.tz_default)
    await bot.add_cog(DigestCog(bot, service))
