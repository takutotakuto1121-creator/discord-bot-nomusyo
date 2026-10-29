"""``/todo add|list|done|delete``: TODO commands backed by ``ScheduleService``.

Business rules live in ``nomusyo.schedule``; this module only translates between Discord
interactions and the service. Commands are checked by ``AllowedCommandTree``, buttons by
``AllowedView`` (autocomplete checks explicitly). Responses are ephemeral and never allow
mentions. User input is never echoed in log messages.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Collection
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

from nomusyo.bot import embeds
from nomusyo.bot.app import NomusyoBot
from nomusyo.bot.checks import AllowedView, ensure_allowed, run_db
from nomusyo.schedule.errors import ScheduleError, ValidationError
from nomusyo.schedule.models import Todo
from nomusyo.schedule.service import ScheduleService
from nomusyo.schedule.todo import STATUS_LABELS, build_todo_input

log = logging.getLogger(__name__)

VIEW_TIMEOUT = 180.0
AUTOCOMPLETE_LIMIT = 25
MSG_EXPIRED = "時間切れのため操作を終了しました。"
MSG_CANCELLED = "キャンセルしました。"
MSG_BAD_REF = "TODOは候補から選択してください。"
MSG_FINISHED = "この操作はすでに完了しています。"
NO_MENTIONS = discord.AllowedMentions.none()


async def _reply(interaction: discord.Interaction, text: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_MENTIONS)
    else:
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS)


def todo_choice_name(t: Todo) -> str:
    due = f" ({embeds.todo_due_text(t)})" if t.due else ""
    return embeds.truncate(f"#{t.id} {t.title}{due}", 100)


def _matches(t: Todo, query: str) -> bool:
    q = query.strip().lstrip("#").casefold()
    if not q:
        return True
    return q in " ".join([str(t.id), t.title, t.note or ""]).casefold()


@app_commands.guild_only()
class TodoCog(commands.GroupCog, group_name="todo", group_description="TODO を管理します"):
    def __init__(
        self,
        bot: NomusyoBot,
        service: ScheduleService,
        allowed_user_ids: Collection[int],
    ) -> None:
        super().__init__()
        self.bot = bot
        self.service = service
        self.allowed_user_ids = frozenset(allowed_user_ids)

    def now(self):
        return self.service.clock.now()

    def resolve_todo_id(self, raw: str) -> int:
        text = (raw or "").strip().lstrip("#").strip()
        if not text.isascii() or not text.isdigit():
            raise ValidationError(MSG_BAD_REF)
        return int(text)

    # ---- /todo add -------------------------------------------------------------------
    @app_commands.command(name="add", description="TODO を追加します")
    @app_commands.describe(
        title="タイトル",
        due="期限(例: 2026-11-03 18:00 / 11/3 / 明日 9:00)。省略可",
        note="メモ",
        priority="優先度(0〜3)",
        remind="リマインド(期限が必要。例: 1h,1d@20:00)",
    )
    async def add(
        self,
        interaction: discord.Interaction,
        title: str,
        due: str | None = None,
        note: str | None = None,
        priority: app_commands.Range[int, 0, 3] = 0,
        remind: str | None = None,
    ) -> None:
        try:
            data = build_todo_input(
                title=title,
                now=self.now(),
                tz=self.service.default_tz,
                due=due,
                note=note,
                priority=priority,
                remind=remind,
            )
            todo = await run_db(
                self.service.add_todo,
                data.title,
                due=data.due,
                due_all_day=data.due_all_day,
                note=data.note,
                priority=data.priority,
                tz=data.tz,
                reminders=data.reminders,
            )
        except ScheduleError as exc:
            await _reply(interaction, str(exc))
            return
        view = DoneButtonView(self, todo)
        await interaction.response.send_message(
            "追加しました。",
            embed=embeds.todo_embed(todo, now=self.now()),
            view=view,
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )
        view.message = await interaction.original_response()

    # ---- /todo list ------------------------------------------------------------------
    @app_commands.command(name="list", description="TODO の一覧を表示します")
    @app_commands.describe(status="表示する状態(既定: 未完了)")
    async def list_todos(
        self,
        interaction: discord.Interaction,
        status: Literal["open", "overdue", "done", "no_due", "all"] = "open",
    ) -> None:
        try:
            todos = await run_db(self.service.list_todos, status)
        except ScheduleError as exc:
            await _reply(interaction, str(exc))
            return
        view = TodoListView(self, todos, f"TODO 一覧({STATUS_LABELS[status]})", status=status)
        await interaction.response.send_message(
            embed=view.current_embed(),
            view=view if view.has_components else discord.utils.MISSING,
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )
        if view.has_components:
            view.message = await interaction.original_response()

    # ---- /todo done ------------------------------------------------------------------
    @app_commands.command(name="done", description="TODO を完了にします")
    @app_commands.describe(todo="完了する TODO(候補から選択)")
    async def done(self, interaction: discord.Interaction, todo: str) -> None:
        try:
            t = await run_db(self.service.complete_todo, self.resolve_todo_id(todo))
        except ScheduleError as exc:
            await _reply(interaction, str(exc))
            return
        await interaction.response.send_message(
            "完了にしました。",
            embed=embeds.todo_embed(t, now=self.now()),
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )

    # ---- /todo delete ----------------------------------------------------------------
    @app_commands.command(name="delete", description="TODO を削除します(確認あり)")
    @app_commands.describe(todo="削除する TODO(候補から選択)")
    async def delete(self, interaction: discord.Interaction, todo: str) -> None:
        try:
            t = await run_db(self.service.get_todo, self.resolve_todo_id(todo))
        except ScheduleError as exc:
            await _reply(interaction, str(exc))
            return
        view = DeleteConfirmView(self, t)
        await interaction.response.send_message(
            "この TODO を削除しますか?",
            embed=embeds.todo_embed(t, now=self.now(), heading="削除確認"),
            view=view,
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )
        view.message = await interaction.original_response()

    # ---- autocomplete ----------------------------------------------------------------
    async def _todo_autocomplete(
        self, interaction: discord.Interaction, current: str, status: str
    ) -> list[app_commands.Choice[str]]:
        if not await ensure_allowed(interaction, self.allowed_user_ids):
            return []
        try:
            todos = await run_db(self.service.list_todos, status)
        except Exception as exc:  # noqa: BLE001 - autocomplete must not raise
            log.warning("Autocomplete failed (%s)", type(exc).__name__)
            return []
        picked = [t for t in todos if _matches(t, current)][:AUTOCOMPLETE_LIMIT]
        return [app_commands.Choice(name=todo_choice_name(t), value=str(t.id)) for t in picked]

    @done.autocomplete("todo")
    async def done_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._todo_autocomplete(interaction, current, "open")

    @delete.autocomplete("todo")
    async def delete_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._todo_autocomplete(interaction, current, "all")


class _ExpiringView(AllowedView):
    message: discord.Message | None = None
    timeout_text: str | None = MSG_EXPIRED
    finished = False

    async def on_timeout(self) -> None:
        if self.finished or self.message is None:
            return
        self.finished = True
        try:
            if self.timeout_text is None:
                await self.message.edit(view=None)
            else:
                await self.message.edit(content=self.timeout_text, embed=None, view=None)
        except discord.HTTPException as exc:
            log.warning("Could not update expired message (%s)", type(exc).__name__)


class DoneButtonView(_ExpiringView):
    """Single 完了 button shown after ``/todo add``."""

    def __init__(self, cog: TodoCog, todo: Todo) -> None:
        super().__init__(cog.allowed_user_ids, timeout=VIEW_TIMEOUT)
        self.cog = cog
        self.todo = todo
        self.timeout_text = None  # keep the embed, only remove the button

    @discord.ui.button(label="完了", style=discord.ButtonStyle.success)
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.finished:
            await _reply(interaction, MSG_FINISHED)
            return
        self.message = interaction.message or self.message
        self.finished = True  # claim before the first await
        try:
            t = await run_db(self.cog.service.complete_todo, self.todo.id)
        except ScheduleError as exc:
            self.finished = False
            await _reply(interaction, str(exc))
            return
        except Exception:
            self.finished = False
            raise
        self.stop()
        await interaction.response.edit_message(
            content="完了にしました。",
            embed=embeds.todo_embed(t, now=self.cog.now()),
            view=None,
            allowed_mentions=NO_MENTIONS,
        )


class TodoListView(_ExpiringView):
    """Paged list; open TODOs on the page get a 完了 button each, completing removes them."""

    def __init__(
        self, cog: TodoCog, todos: list[Todo], heading: str, *, status: str = "open"
    ) -> None:
        super().__init__(cog.allowed_user_ids, timeout=VIEW_TIMEOUT)
        self.cog = cog
        self.todos = todos
        self.heading = heading
        self.status = status  # "all" keeps completed items in the list
        self.page = 0
        self.timeout_text = None
        self.lock = asyncio.Lock()  # serialize button presses (state + DB calls)
        self._rebuild()

    @property
    def pages(self) -> int:
        return embeds.page_count(len(self.todos), embeds.TODO_PER_PAGE)

    @property
    def has_components(self) -> bool:
        return len(self.children) > 0

    def current_embed(self) -> discord.Embed:
        return embeds.todo_list_embed(
            self.todos, heading=self.heading, now=self.cog.now(), page=self.page
        )

    def _page_items(self) -> list[Todo]:
        size = embeds.TODO_PER_PAGE
        return self.todos[self.page * size : (self.page + 1) * size]

    def _rebuild(self) -> None:
        self.page = min(max(self.page, 0), self.pages - 1)
        self.clear_items()
        for t in self._page_items():
            if not t.done:
                self.add_item(DoneItemButton(t.id))
        if self.pages > 1:
            prev_b = PageButton("前へ", -1, disabled=self.page <= 0)
            next_b = PageButton("次へ", 1, disabled=self.page >= self.pages - 1)
            prev_b.row = next_b.row = 1
            self.add_item(prev_b)
            self.add_item(next_b)

    async def refresh(self, interaction: discord.Interaction) -> None:
        self.message = interaction.message or self.message
        self._rebuild()
        if not self.has_components:
            self.finished = True
            self.stop()
        await interaction.response.edit_message(
            embed=self.current_embed(),
            view=self if self.has_components else None,
            allowed_mentions=NO_MENTIONS,
        )


class DoneItemButton(discord.ui.Button):
    view: TodoListView

    def __init__(self, todo_id: int) -> None:
        super().__init__(label=f"完了 #{todo_id}", style=discord.ButtonStyle.success, row=0)
        self.todo_id = todo_id

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        async with view.lock:
            try:
                done = await run_db(view.cog.service.complete_todo, self.todo_id)
            except ScheduleError as exc:
                await _reply(interaction, str(exc))
                return
            if view.status == "all":
                view.todos = [done if t.id == done.id else t for t in view.todos]
            else:
                view.todos = [t for t in view.todos if t.id != done.id]
            await view.refresh(interaction)


class PageButton(discord.ui.Button):
    view: TodoListView

    def __init__(self, label: str, delta: int, *, disabled: bool) -> None:
        super().__init__(label=label, style=discord.ButtonStyle.secondary, disabled=disabled)
        self.delta = delta

    async def callback(self, interaction: discord.Interaction) -> None:
        async with self.view.lock:
            self.view.page += self.delta
            await self.view.refresh(interaction)


class DeleteConfirmView(_ExpiringView):
    """Soft delete only after the confirm button."""

    def __init__(self, cog: TodoCog, todo: Todo) -> None:
        super().__init__(cog.allowed_user_ids, timeout=VIEW_TIMEOUT)
        self.cog = cog
        self.todo = todo
        self.timeout_text = "時間切れのため削除しませんでした。"

    @discord.ui.button(label="削除する", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.finished:
            await _reply(interaction, MSG_FINISHED)
            return
        self.message = interaction.message or self.message
        self.finished = True  # claim before the first await
        try:
            await run_db(self.cog.service.delete_todo, self.todo.id)
        except ScheduleError as exc:
            self.finished = False
            await _reply(interaction, str(exc))
            return
        except Exception:
            self.finished = False
            raise
        self.stop()
        await interaction.response.edit_message(
            content=f"削除しました(#{self.todo.id})。",
            embed=None,
            view=None,
            allowed_mentions=NO_MENTIONS,
        )

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.finished = True
        self.stop()
        await interaction.response.edit_message(
            content=MSG_CANCELLED, embed=None, view=None, allowed_mentions=NO_MENTIONS
        )


async def setup(bot: NomusyoBot) -> None:
    db = bot.db
    if db is None:
        raise RuntimeError("bot.db is not open (it is opened in NomusyoBot.setup_hook)")
    service = ScheduleService(db, bot.config.owner_user_id, default_tz=bot.config.tz_default)
    await bot.add_cog(TodoCog(bot, service, bot.config.allowed_user_ids))
