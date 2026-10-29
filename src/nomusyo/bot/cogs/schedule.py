"""``/add`` ``/list`` ``/edit`` ``/delete``: schedule commands backed by ``ScheduleService``.

Business rules live in ``nomusyo.schedule``; this module only translates between Discord
interactions and the service. Every command, button and modal goes through the allowed-user
check (``AllowedCommandTree`` / ``AllowedView`` / ``AllowedModal``; autocomplete checks
explicitly). Responses are ephemeral. User input is never echoed in log messages.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

from nomusyo.bot import embeds
from nomusyo.bot.app import NomusyoBot
from nomusyo.bot.checks import AllowedModal, AllowedView, ensure_allowed, run_db
from nomusyo.schedule import service as svc
from nomusyo.schedule.errors import ScheduleError, ValidationError
from nomusyo.schedule.models import (
    Event,
    Occurrence,
    ReminderSpec,
    Window,
    get_zone,
    local_midnight,
)
from nomusyo.schedule.parsing import parse_duration, parse_reminders, parse_when
from nomusyo.schedule.service import ScheduleService

log = logging.getLogger(__name__)

PREVIEW_TIMEOUT = 180.0
AUTOCOMPLETE_LIMIT = 25
AUTOCOMPLETE_PAST_DAYS = 30
AUTOCOMPLETE_FUTURE_DAYS = 365
RANGE_DAYS = {"today": 1, "week": 7, "month": 30, "all": 365}
RANGE_LABELS = {"today": "今日", "week": "今後7日", "month": "今後30日", "all": "今後1年"}

MSG_EXPIRED = "時間切れのため破棄しました(未保存)。"
MSG_CANCELLED = "キャンセルしました。"
MSG_BAD_REF = "予定は候補から選択してください。"
NO_MENTIONS = discord.AllowedMentions.none()
MSG_DONE = "この操作はすでに完了しています。"


@dataclass(frozen=True)
class EventDraft:
    """A validated, not yet saved event (what the preview shows)."""

    title: str
    start: datetime
    all_day: bool
    duration_min: int | None
    place: str | None
    note: str | None
    reminders: tuple[ReminderSpec, ...]
    tz: str

    def embed(self, heading: str | None = None) -> discord.Embed:
        end = None
        if self.duration_min or self.all_day:
            probe = Event(
                id=0, owner_id=0, title="x", start=self.start, tz=self.tz,
                all_day=self.all_day, duration_min=self.duration_min,
            )  # fmt: skip
            end = probe.end
        return embeds.event_embed(
            title=self.title,
            start=self.start,
            end=end,
            all_day=self.all_day,
            place=self.place,
            note=self.note,
            reminders=self.reminders,
            heading=heading,
            tz=self.tz,
        )


def _clean(value: str | None) -> str | None:
    v = (value or "").strip()
    return v or None


def _check_len(value: str | None, what: str, maximum: int) -> None:
    if value is not None and len(value) > maximum:
        raise ValidationError(f"{what}は{maximum}文字以内にしてください")


def when_text(ev: Event) -> str:
    """Editable representation of an event start (inverse of ``parse_when``)."""
    return f"{ev.start:%Y-%m-%d}" if ev.all_day else f"{ev.start:%Y-%m-%d %H:%M}"


def reminders_text(specs: Collection[ReminderSpec]) -> str:
    parts = []
    for s in specs:
        if s.at_local is not None:
            parts.append(f"{s.day_offset}d@{s.at_local}")
        else:
            parts.append(f"{s.offset_min}m")
    return ",".join(parts)


def event_choice_name(o: Occurrence) -> str:
    when = "終日" if o.all_day else f"{o.start:%H:%M}"
    return embeds.truncate(f"#{o.event_id} {o.start:%m/%d} {when} {o.title}", 100)


def _matches(o: Occurrence, query: str) -> bool:
    q = query.strip().lstrip("#").casefold()
    if not q:
        return True
    hay = " ".join([str(o.event_id), o.title, o.place or "", o.note or ""]).casefold()
    return q in hay


async def _reply(interaction: discord.Interaction, text: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_MENTIONS)
    else:
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS)


class ScheduleCog(commands.Cog):
    def __init__(
        self,
        bot: NomusyoBot,
        service: ScheduleService,
        allowed_user_ids: Collection[int],
    ) -> None:
        self.bot = bot
        self.service = service
        self.allowed_user_ids = frozenset(allowed_user_ids)

    # ---- helpers ---------------------------------------------------------------------
    def now(self) -> datetime:
        return self.service.clock.now()

    def build_draft(
        self,
        *,
        when: str,
        title: str,
        duration: str | None = None,
        place: str | None = None,
        note: str | None = None,
        remind: str | None = None,
    ) -> EventDraft:
        """Parse and validate raw input. Raises ``ScheduleError`` (message safe to show)."""
        tz = self.service.default_tz
        title_v = _clean(title)
        if title_v is None:
            raise ValidationError("タイトルを入力してください")
        place_v, note_v = _clean(place), _clean(note)
        _check_len(title_v, "タイトル", svc.MAX_TITLE)
        _check_len(place_v, "場所", svc.MAX_PLACE)
        _check_len(note_v, "メモ", svc.MAX_NOTE)
        parsed = parse_when(when, now=self.now(), tz=get_zone(tz))
        duration_v = _clean(duration)
        return EventDraft(
            title=title_v,
            start=parsed.start,
            all_day=parsed.all_day,
            duration_min=parse_duration(duration_v) if duration_v else None,
            place=place_v,
            note=note_v,
            reminders=parse_reminders(remind or ""),
            tz=tz,
        )

    def resolve_event_id(self, raw: str) -> int:
        text = (raw or "").strip().lstrip("#").strip()
        if not text.isascii() or not text.isdigit():
            raise ValidationError(MSG_BAD_REF)
        return int(text)

    def window_for(self, range_: str) -> tuple[Window, str]:
        zone = get_zone(self.service.default_tz)
        today = self.now().astimezone(zone).date()
        start = local_midnight(today, zone)
        end = local_midnight(today + timedelta(days=RANGE_DAYS[range_]), zone)
        return Window(start, end), RANGE_LABELS[range_]

    # ---- /add ------------------------------------------------------------------------
    @app_commands.command(name="add", description="予定を登録します(保存前にプレビューを確認)")
    @app_commands.describe(
        when="日時(例: 2026-11-03 15:00 / 11/3 15:00 / 明日 9:00 / 11/3 は終日)",
        title="タイトル",
        duration="所要時間(例: 90m / 2h / 1h30m / 1d)",
        place="場所",
        note="メモ",
        remind="リマインド(例: 10m,1h,1d@20:00)",
    )
    @app_commands.guild_only()
    async def add(
        self,
        interaction: discord.Interaction,
        when: str,
        title: str,
        duration: str | None = None,
        place: str | None = None,
        note: str | None = None,
        remind: str | None = None,
    ) -> None:
        try:
            draft = self.build_draft(
                when=when, title=title, duration=duration, place=place, note=note, remind=remind
            )
        except ScheduleError as exc:
            await _reply(interaction, str(exc))
            return
        view = AddConfirmView(self, draft)
        await interaction.response.send_message(
            "この内容で保存しますか?",
            embed=draft.embed("プレビュー"),
            view=view,
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )
        view.message = await interaction.original_response()

    # ---- /list -----------------------------------------------------------------------
    @app_commands.command(name="list", description="予定の一覧を表示します")
    @app_commands.describe(range="期間(既定: 今後7日)", query="タイトル・場所・メモの検索語")
    @app_commands.guild_only()
    async def list_events(
        self,
        interaction: discord.Interaction,
        range: Literal["today", "week", "month", "all"] = "week",  # noqa: A002
        query: str | None = None,
    ) -> None:
        window, label = self.window_for(range)
        try:
            occurrences = await run_db(self.service.expand, window)
        except ScheduleError as exc:
            await _reply(interaction, str(exc))
            return
        q = _clean(query)
        if q:
            occurrences = [o for o in occurrences if _matches(o, q)]
        heading = f"予定一覧({label})"
        view = ListView(self.allowed_user_ids, occurrences, heading)
        await interaction.response.send_message(
            embed=view.current_embed(),
            view=view if view.pages > 1 else discord.utils.MISSING,
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )
        if view.pages > 1:
            view.message = await interaction.original_response()

    # ---- /edit -----------------------------------------------------------------------
    @app_commands.command(name="edit", description="予定を編集します")
    @app_commands.describe(event="編集する予定(候補から選択)")
    @app_commands.guild_only()
    async def edit(self, interaction: discord.Interaction, event: str) -> None:
        try:
            ev = await run_db(self.service.get_event, self.resolve_event_id(event))
            modal = EditModal(self, ev)
        except ScheduleError as exc:
            await _reply(interaction, str(exc))
            return
        await interaction.response.send_modal(modal)

    # ---- /delete ---------------------------------------------------------------------
    @app_commands.command(name="delete", description="予定を削除します(確認あり)")
    @app_commands.describe(event="削除する予定(候補から選択)")
    @app_commands.guild_only()
    async def delete(self, interaction: discord.Interaction, event: str) -> None:
        try:
            ev = await run_db(self.service.get_event, self.resolve_event_id(event))
        except ScheduleError as exc:
            await _reply(interaction, str(exc))
            return
        view = DeleteConfirmView(self, ev)
        await interaction.response.send_message(
            "この予定を削除しますか?",
            embed=embeds.embed_from_event(ev, heading="削除確認", color=embeds.COLOR_DANGER),
            view=view,
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )
        view.message = await interaction.original_response()

    # ---- autocomplete ----------------------------------------------------------------
    async def _event_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        if not await ensure_allowed(interaction, self.allowed_user_ids):
            return []
        zone = get_zone(self.service.default_tz)
        today = self.now().astimezone(zone).date()
        window = Window(
            local_midnight(today - timedelta(days=AUTOCOMPLETE_PAST_DAYS), zone),
            local_midnight(today + timedelta(days=AUTOCOMPLETE_FUTURE_DAYS), zone),
        )
        try:
            occurrences = await run_db(self.service.expand, window)
        except Exception as exc:  # noqa: BLE001 - autocomplete must not raise
            log.warning("Autocomplete failed (%s)", type(exc).__name__)
            return []
        # Upcoming first, then recent past (most recent first).
        now = self.now()
        upcoming = [o for o in occurrences if o.end >= now or o.start >= now]
        past = [o for o in occurrences if o not in upcoming][::-1]
        picked = [o for o in upcoming + past if _matches(o, current)][:AUTOCOMPLETE_LIMIT]
        return [
            app_commands.Choice(name=event_choice_name(o), value=str(o.event_id)) for o in picked
        ]

    @edit.autocomplete("event")
    async def edit_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._event_autocomplete(interaction, current)

    @delete.autocomplete("event")
    async def delete_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._event_autocomplete(interaction, current)


class _TimeoutMixin:
    """Disable the message's components when the view times out."""

    message: discord.Message | None = None
    timeout_text = MSG_EXPIRED
    finished = False  # completed (saved / deleted / cancelled)
    expired = False  # timed out without completing

    async def _expire(self) -> None:
        if self.message is None:
            return
        try:
            await self.message.edit(content=self.timeout_text, embed=None, view=None)
        except discord.HTTPException as exc:
            log.warning("Could not update expired message (%s)", type(exc).__name__)


class AddConfirmView(_TimeoutMixin, AllowedView):
    """Preview with save / edit / cancel. Nothing is stored until 保存 is pressed."""

    def __init__(self, cog: ScheduleCog, draft: EventDraft) -> None:
        super().__init__(cog.allowed_user_ids, timeout=PREVIEW_TIMEOUT)
        self.cog = cog
        self.draft = draft

    async def on_timeout(self) -> None:
        if not self.finished:
            self.expired = True
            await self._expire()

    @discord.ui.button(label="保存", style=discord.ButtonStyle.success)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.finished or self.expired:
            await _reply(interaction, MSG_DONE)
            return
        self.message = interaction.message or self.message
        d = self.draft
        self.finished = True  # claim before the first await: no double save
        try:
            ev = await run_db(
                self.cog.service.add_event,
                d.title,
                d.start,
                all_day=d.all_day,
                duration_min=d.duration_min,
                note=d.note,
                place=d.place,
                tz=d.tz,
                reminders=d.reminders,
            )
        except ScheduleError as exc:
            self.finished = False
            await _reply(interaction, str(exc))
            return
        except Exception:
            self.finished = False  # unexpected: allow a retry; on_error answers the user
            raise
        self.stop()
        await interaction.response.edit_message(
            content="保存しました。",
            embed=embeds.embed_from_event(ev),
            view=None,
            allowed_mentions=NO_MENTIONS,
        )

    @discord.ui.button(label="修正", style=discord.ButtonStyle.primary)
    async def revise(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.finished or self.expired:
            await _reply(interaction, MSG_DONE)
            return
        self.message = interaction.message or self.message
        try:
            modal = DraftModal(self.cog, self)
        except ScheduleError as exc:
            await _reply(interaction, str(exc))
            return
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.finished = True
        self.stop()
        await interaction.response.edit_message(
            content=MSG_CANCELLED, embed=None, view=None, allowed_mentions=NO_MENTIONS
        )


def _text_input(label: str, default: str | None, maximum: int, *, required: bool = False,
                paragraph: bool = False) -> discord.ui.TextInput:  # fmt: skip
    if default is not None and len(default) > maximum:
        # Never truncate silently: saving a cut-off value would lose data.
        raise ValidationError(f"{label}が長すぎるため、この画面では編集できません")
    return discord.ui.TextInput(
        label=label,
        default=default or None,
        max_length=maximum,
        required=required,
        style=discord.TextStyle.paragraph if paragraph else discord.TextStyle.short,
    )


class DraftModal(AllowedModal):
    """Revise a preview (title / when / duration / place / remind; the note is kept)."""

    def __init__(self, cog: ScheduleCog, view: AddConfirmView) -> None:
        super().__init__(cog.allowed_user_ids, title="予定を修正", timeout=PREVIEW_TIMEOUT)
        self.cog = cog
        self.view = view
        d = view.draft
        when_default = f"{d.start:%Y-%m-%d}" if d.all_day else f"{d.start:%Y-%m-%d %H:%M}"
        self.f_title = _text_input("タイトル", d.title, svc.MAX_TITLE, required=True)
        self.f_when = _text_input("日時", when_default, 40, required=True)
        self.f_duration = _text_input("所要時間(例: 90m / 2h)", format_dur(d.duration_min), 20)
        self.f_place = _text_input("場所", d.place, svc.MAX_PLACE)
        self.f_remind = _text_input(
            "リマインド(例: 10m,1d@20:00)", reminders_text(d.reminders), 100
        )
        for item in (self.f_title, self.f_when, self.f_duration, self.f_place, self.f_remind):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self.view.finished or self.view.expired:
            await _reply(interaction, MSG_DONE)
            return
        try:
            draft = self.cog.build_draft(
                when=self.f_when.value,
                title=self.f_title.value,
                duration=self.f_duration.value,
                place=self.f_place.value,
                note=self.view.draft.note,
                remind=self.f_remind.value,
            )
        except ScheduleError as exc:
            await _reply(interaction, f"{exc}(プレビューは変更されていません)")
            return
        self.view.draft = draft
        await interaction.response.edit_message(
            embed=draft.embed("プレビュー"), view=self.view, allowed_mentions=NO_MENTIONS
        )


def format_dur(minutes: int | None) -> str | None:
    return embeds.format_duration(minutes) if minutes else None


class EditModal(AllowedModal):
    """Edit an existing event (title / when / duration / place / note; reminders unchanged)."""

    def __init__(self, cog: ScheduleCog, event: Event) -> None:
        super().__init__(
            cog.allowed_user_ids,
            title=embeds.truncate(f"予定を編集 #{event.id}", 45),
            timeout=PREVIEW_TIMEOUT,
        )
        self.cog = cog
        self.event = event
        self._when_default = when_text(event)
        self.f_title = _text_input("タイトル", event.title, svc.MAX_TITLE, required=True)
        self.f_when = _text_input("日時", self._when_default, 40, required=True)
        self.f_duration = _text_input("所要時間(空欄で解除)", format_dur(event.duration_min), 20)
        self.f_place = _text_input("場所(空欄で解除)", event.place, svc.MAX_PLACE)
        self.f_note = _text_input("メモ(空欄で解除)", event.note, svc.MAX_NOTE, paragraph=True)
        for item in (self.f_title, self.f_when, self.f_duration, self.f_place, self.f_note):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            changes = self._changes()
            ev = await run_db(self.cog.service.update_event, self.event.id, **changes)
        except ScheduleError as exc:
            await _reply(interaction, str(exc))
            return
        await interaction.response.send_message(
            "更新しました。",
            embed=embeds.embed_from_event(ev),
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )

    def _changes(self) -> dict:
        title = _clean(self.f_title.value)
        if title is None:
            raise ValidationError("タイトルを入力してください")
        _check_len(title, "タイトル", svc.MAX_TITLE)
        place, note = _clean(self.f_place.value), _clean(self.f_note.value)
        _check_len(place, "場所", svc.MAX_PLACE)
        _check_len(note, "メモ", svc.MAX_NOTE)
        duration_text = _clean(self.f_duration.value)
        changes: dict = {
            "title": title,
            "place": place,
            "note": note,
            "duration_min": parse_duration(duration_text) if duration_text else None,
        }
        when = self.f_when.value.strip()
        if when != self._when_default:  # unchanged start is not re-validated (may be in the past)
            parsed = parse_when(when, now=self.cog.now(), tz=get_zone(self.event.tz))
            changes["start"] = parsed.start
            changes["all_day"] = parsed.all_day
        return changes


class DeleteConfirmView(_TimeoutMixin, AllowedView):
    """Soft delete only after the confirm button."""

    def __init__(self, cog: ScheduleCog, event: Event) -> None:
        super().__init__(cog.allowed_user_ids, timeout=PREVIEW_TIMEOUT)
        self.cog = cog
        self.event = event
        self.timeout_text = "時間切れのため削除しませんでした。"

    async def on_timeout(self) -> None:
        if not self.finished:
            self.expired = True
            await self._expire()

    @discord.ui.button(label="削除する", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.finished or self.expired:
            await _reply(interaction, MSG_DONE)
            return
        self.message = interaction.message or self.message
        self.finished = True  # claim before the first await
        try:
            await run_db(self.cog.service.delete_event, self.event.id)
        except ScheduleError as exc:
            self.finished = False
            await _reply(interaction, str(exc))
            return
        except Exception:
            self.finished = False
            raise
        self.stop()
        await interaction.response.edit_message(
            content=f"削除しました(#{self.event.id})。", embed=None, view=None,
            allowed_mentions=NO_MENTIONS,
        )  # fmt: skip

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.finished = True
        self.stop()
        await interaction.response.edit_message(
            content=MSG_CANCELLED, embed=None, view=None, allowed_mentions=NO_MENTIONS
        )


class ListView(_TimeoutMixin, AllowedView):
    """Paged list with prev / next buttons."""

    def __init__(
        self, allowed_user_ids: Collection[int], occurrences: list[Occurrence], heading: str
    ) -> None:
        super().__init__(allowed_user_ids, timeout=PREVIEW_TIMEOUT)
        self.occurrences = occurrences
        self.heading = heading
        self.page = 0
        self.pages = embeds.page_count(len(occurrences))
        self._sync_buttons()

    def current_embed(self) -> discord.Embed:
        return embeds.list_embed(self.occurrences, heading=self.heading, page=self.page)

    def _sync_buttons(self) -> None:
        self.prev_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= self.pages - 1

    async def on_timeout(self) -> None:
        if self.message is None:
            return
        try:
            await self.message.edit(view=None)
        except discord.HTTPException as exc:
            log.warning("Could not update expired list (%s)", type(exc).__name__)

    async def _go(self, interaction: discord.Interaction, delta: int) -> None:
        self.message = interaction.message or self.message
        self.page = min(max(self.page + delta, 0), self.pages - 1)
        self._sync_buttons()
        await interaction.response.edit_message(
            embed=self.current_embed(), view=self, allowed_mentions=NO_MENTIONS
        )

    @discord.ui.button(label="前へ", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._go(interaction, -1)

    @discord.ui.button(label="次へ", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._go(interaction, 1)


async def setup(bot: NomusyoBot) -> None:
    db = bot.db
    if db is None:
        raise RuntimeError("bot.db is not open (it is opened in NomusyoBot.setup_hook)")
    service = ScheduleService(db, bot.config.owner_user_id, default_tz=bot.config.tz_default)
    await bot.add_cog(ScheduleCog(bot, service, bot.config.allowed_user_ids))
