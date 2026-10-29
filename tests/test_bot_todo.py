import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import discord
import pytest

from nomusyo.bot.checks import DENIED_MESSAGE, AllowedCommandTree
from nomusyo.bot.cogs import todo as todo_mod
from nomusyo.bot.cogs.todo import (
    MSG_BAD_REF,
    MSG_CANCELLED,
    TodoCog,
    TodoListView,
)
from nomusyo.schedule.clock import FixedClock
from nomusyo.schedule.errors import NotFoundError
from nomusyo.schedule.models import Todo
from nomusyo.schedule.service import ScheduleService
from nomusyo.storage import Database

JST = ZoneInfo("Asia/Tokyo")
NOW = datetime(2026, 10, 29, 12, 0, tzinfo=JST)
ALLOWED_ID = 100
DENIED_ID = 999


def jst(*a):
    return datetime(*a, tzinfo=JST)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db(tmp_path):
    d = Database.open(tmp_path / "t.db")
    yield d
    d.close()


@pytest.fixture
def svc(db):
    return ScheduleService(db, 1, clock=FixedClock(NOW))


@pytest.fixture
def cog(svc):
    return TodoCog(MagicMock(), svc, {ALLOWED_ID})


def make_inter(user_id=ALLOWED_ID, *, done=False, autocomplete=False):
    inter = MagicMock()
    inter.user.id = user_id
    inter.type = (
        discord.InteractionType.autocomplete
        if autocomplete
        else discord.InteractionType.application_command
    )
    inter.response.is_done.return_value = done
    inter.response.send_message = AsyncMock()
    inter.response.edit_message = AsyncMock()
    inter.followup.send = AsyncMock()
    message = MagicMock()
    message.edit = AsyncMock()
    inter.original_response = AsyncMock(return_value=message)
    return inter


def all_calls(inter):
    return [
        *inter.response.send_message.await_args_list,
        *inter.response.edit_message.await_args_list,
        *inter.followup.send.await_args_list,
    ]


def assert_private_and_no_mentions(inter):
    calls = all_calls(inter)
    assert calls
    for c in inter.response.send_message.await_args_list + inter.followup.send.await_args_list:
        assert c.kwargs.get("ephemeral") is True
    for c in calls:
        am = c.kwargs.get("allowed_mentions")
        assert isinstance(am, discord.AllowedMentions)
        assert am.everyone is False and am.users is False and am.roles is False


def reply_text(inter):
    c = all_calls(inter)[-1]
    return c.args[0] if c.args else c.kwargs.get("content")


async def press(view, item, inter):
    """Mimic discord.py dispatch: interaction_check first, then the callback."""
    if await view.interaction_check(inter):
        await item.callback(inter)


def find(view, label):
    return next(c for c in view.children if c.label == label)


def labels(view):
    return [c.label for c in view.children]


async def add(cog, **kw):
    kw.setdefault("title", "牛乳")
    inter = make_inter()
    await cog.add.callback(cog, inter, **kw)
    return inter


async def open_list(cog, status="open"):
    inter = make_inter()
    await cog.list_todos.callback(cog, inter, status)
    return inter


def view_of(inter):
    return inter.response.send_message.await_args.kwargs["view"]


def add_n(svc, n, **kw):
    return [svc.add_todo(f"t{i}", **kw) for i in range(n)]


def count_todos(db, *, include_deleted=False):
    sql = "SELECT COUNT(*) AS c FROM todos"
    if not include_deleted:
        sql += " WHERE deleted_at_utc IS NULL"
    return db.query_one(sql)["c"]


def field(inter, name):
    e = inter.response.send_message.await_args.kwargs["embed"]
    return {f.name: f.value for f in e.fields}[name]


# ===== /todo add ===============================================================================
def test_add_stores_and_shows_done_button(cog, svc):
    async def go():
        inter = await add(cog, due="2026-11-03 18:00", note="m", priority=2, remind="1h")
        kw = inter.response.send_message.await_args.kwargs
        assert kw["embed"].title == "牛乳"
        assert labels(kw["view"]) == ["完了"]
        assert_private_and_no_mentions(inter)

    run(go())
    [t] = svc.list_todos("all")
    assert t.title == "牛乳" and t.priority == 2 and t.note == "m"
    assert t.due == jst(2026, 11, 3, 18) and len(t.reminders) == 1


def test_add_without_due(cog, svc):
    run(add(cog))
    [t] = svc.list_todos("all")
    assert t.due is None


@pytest.mark.parametrize(
    "kw,needle",
    [
        ({"remind": "1h"}, "期限が必要"),
        ({"due": "2026-10-28"}, "過去"),
        ({"due": "いつか"}, "形式"),
        ({"title": "あ" * 201}, "200"),
        ({"note": "x" * 2001}, "2000"),
        ({"priority": 4}, "優先度"),
        ({"title": "  "}, "タイトル"),
    ],
)
def test_add_invalid_input_replies_error_and_writes_nothing(cog, db, kw, needle):
    async def go():
        inter = await add(cog, **kw)
        assert needle in reply_text(inter)
        assert "view" not in inter.response.send_message.await_args.kwargs
        assert_private_and_no_mentions(inter)

    run(go())
    assert count_todos(db, include_deleted=True) == 0


def test_add_error_does_not_echo_input(cog):
    async def go():
        inter = await add(cog, title="SECRET" * 50, note="x" * 3000)
        assert "SECRET" not in reply_text(inter)

    run(go())


def test_add_mention_title_is_neutralized(cog):
    async def go():
        inter = await add(cog, title="@everyone <@1>")
        e = inter.response.send_message.await_args.kwargs["embed"]
        assert "@everyone" not in e.title and "<@1>" not in e.title
        assert_private_and_no_mentions(inter)

    run(go())


def test_add_error_uses_followup_when_response_done(cog):
    async def go():
        inter = make_inter(done=True)
        await cog.add.callback(cog, inter, title="  ")
        inter.followup.send.assert_awaited_once()
        inter.response.send_message.assert_not_called()

    run(go())


def test_add_done_button_completes(cog, svc):
    async def go():
        view = view_of(await add(cog))
        click = make_inter()
        await press(view, view.children[0], click)
        kw = click.response.edit_message.await_args.kwargs
        assert kw["content"] == "完了にしました。" and kw["view"] is None
        assert_private_and_no_mentions(click)

    run(go())
    assert svc.list_todos("open") == []
    assert len(svc.list_todos("done")) == 1


def test_add_done_button_double_press(cog):
    async def go():
        view = view_of(await add(cog))
        await press(view, view.children[0], make_inter())
        second = make_inter()
        await press(view, view.children[0], second)
        assert "完了" in reply_text(second)
        second.response.edit_message.assert_not_called()

    run(go())


def test_add_done_button_timeout_removes_button_keeps_embed(cog):
    async def go():
        view = view_of(await add(cog))
        await view.on_timeout()
        view.message.edit.assert_awaited_once_with(view=None)

    run(go())


# ===== done / delete flow ======================================================================
def test_add_done_delete_flow(cog, svc, db):
    async def go():
        await add(cog)
        [t] = svc.list_todos("open")
        d = make_inter()
        await cog.done.callback(cog, d, str(t.id))
        assert reply_text(d) == "完了にしました。"
        assert_private_and_no_mentions(d)
        assert field(d, "状態") == "完了"

        dl = make_inter()
        await cog.delete.callback(cog, dl, f"#{t.id}")
        view = view_of(dl)
        assert dl.response.send_message.await_args.kwargs["embed"].title == "削除確認: 牛乳"
        assert svc.get_todo(t.id).title == "牛乳"  # unchanged before confirming
        click = make_inter()
        await press(view, view.confirm, click)
        assert (
            click.response.edit_message.await_args.kwargs["content"] == f"削除しました(#{t.id})。"
        )
        assert_private_and_no_mentions(click)
        return t.id

    tid = run(go())
    assert svc.list_todos("all") == []
    assert count_todos(db) == 0 and count_todos(db, include_deleted=True) == 1  # soft delete
    with pytest.raises(NotFoundError):
        svc.get_todo(tid)


@pytest.mark.parametrize("ref", ["abc", "", "  ", "1; DROP", "１", "-1", "1.5", "title"])
def test_done_and_delete_malformed_reference(cog, svc, ref):
    svc.add_todo("a")

    async def go():
        for fn in (cog.done, cog.delete):
            inter = make_inter()
            await fn.callback(cog, inter, ref)
            assert reply_text(inter) == MSG_BAD_REF
            assert "view" not in inter.response.send_message.await_args.kwargs
            assert_private_and_no_mentions(inter)

    run(go())
    assert len(svc.list_todos("open")) == 1


@pytest.mark.parametrize("ref", ["99999", "#99999", "0"])
def test_done_and_delete_nonexistent_id(cog, ref):
    async def go():
        for fn in (cog.done, cog.delete):
            inter = make_inter()
            await fn.callback(cog, inter, ref)
            assert "見つかりません" in reply_text(inter)
            assert_private_and_no_mentions(inter)

    run(go())


def test_done_is_idempotent(cog, svc):
    t = svc.add_todo("a")

    async def go():
        for _ in range(2):
            inter = make_inter()
            await cog.done.callback(cog, inter, str(t.id))
            assert reply_text(inter) == "完了にしました。"

    run(go())


def test_done_deleted_todo_not_found(cog, svc):
    t = svc.add_todo("a")
    svc.delete_todo(t.id)

    async def go():
        inter = make_inter()
        await cog.done.callback(cog, inter, str(t.id))
        assert "見つかりません" in reply_text(inter)

    run(go())


def test_delete_cancel_and_timeout_keep_todo(cog, svc):
    t = svc.add_todo("a")

    async def open_delete():
        inter = make_inter()
        await cog.delete.callback(cog, inter, str(t.id))
        return view_of(inter)

    async def go():
        view = await open_delete()
        click = make_inter()
        await press(view, view.cancel, click)
        assert click.response.edit_message.await_args.kwargs["content"] == MSG_CANCELLED
        late = make_inter()
        await press(view, view.confirm, late)  # already finished
        assert "完了" in reply_text(late)
        assert svc.get_todo(t.id).id == t.id

        view2 = await open_delete()
        await view2.on_timeout()
        assert "削除しませんでした" in view2.message.edit.await_args.kwargs["content"]
        late2 = make_inter()
        await press(view2, view2.confirm, late2)  # press after timeout is refused
        late2.response.edit_message.assert_not_called()
        assert svc.get_todo(t.id).id == t.id

    run(go())


def test_delete_double_confirm(cog, svc):
    t = svc.add_todo("a")

    async def go():
        inter = make_inter()
        await cog.delete.callback(cog, inter, str(t.id))
        view = view_of(inter)
        await press(view, view.confirm, make_inter())
        second = make_inter()
        await press(view, view.confirm, second)
        second.response.edit_message.assert_not_called()
        assert "完了" in reply_text(second)

    run(go())


def test_delete_confirm_when_deleted_elsewhere(cog, svc):
    t = svc.add_todo("a")

    async def go():
        inter = make_inter()
        await cog.delete.callback(cog, inter, str(t.id))
        view = view_of(inter)
        svc.delete_todo(t.id)
        click = make_inter()
        await press(view, view.confirm, click)
        assert "見つかりません" in reply_text(click)
        assert view.finished is False

    run(go())


# ===== permissions =============================================================================
def test_denied_user_cannot_use_buttons(cog, svc):
    t = svc.add_todo("a")

    async def go():
        view = view_of(await open_list(cog))
        click = make_inter(DENIED_ID)
        await press(view, view.children[0], click)
        assert reply_text(click) == DENIED_MESSAGE
        click.response.edit_message.assert_not_called()

        dinter = make_inter()
        await cog.delete.callback(cog, dinter, str(t.id))
        dview = view_of(dinter)
        c2 = make_inter(DENIED_ID)
        await press(dview, dview.confirm, c2)
        assert reply_text(c2) == DENIED_MESSAGE

    run(go())
    assert [x.id for x in svc.list_todos("open")] == [t.id]


def test_denied_user_rejected_by_command_tree():
    tree = AllowedCommandTree(
        discord.Client(intents=discord.Intents.none()), allowed_user_ids={ALLOWED_ID}
    )
    inter = make_inter(DENIED_ID)
    assert run(tree.interaction_check(inter)) is False
    assert reply_text(inter) == DENIED_MESSAGE
    assert run(tree.interaction_check(make_inter(ALLOWED_ID))) is True


def test_autocomplete_denied_user_gets_empty_and_no_response(cog, svc):
    add_n(svc, 3)

    async def go():
        for fn in (cog.done_autocomplete, cog.delete_autocomplete):
            inter = make_inter(DENIED_ID, autocomplete=True)
            assert await fn(inter, "") == []
            inter.response.send_message.assert_not_called()
            inter.followup.send.assert_not_called()

    run(go())


def test_autocomplete_done_lists_open_only_delete_lists_all(cog, svc):
    a, b = add_n(svc, 2)
    svc.complete_todo(a.id)

    async def go():
        out = await cog.done_autocomplete(make_inter(autocomplete=True), "")
        assert [c.value for c in out] == [str(b.id)]
        out = await cog.delete_autocomplete(make_inter(autocomplete=True), "")
        assert {c.value for c in out} == {str(a.id), str(b.id)}

    run(go())


def test_autocomplete_filter_limit_and_name_length(cog, svc):
    add_n(svc, 40, note="n")
    x = svc.add_todo("歯医者" * 50, due=jst(2026, 11, 3, 18))

    async def go():
        out = await cog.done_autocomplete(make_inter(autocomplete=True), "")
        assert len(out) == 25 and all(len(c.name) <= 100 for c in out)
        out = await cog.done_autocomplete(make_inter(autocomplete=True), "歯医")
        assert [c.value for c in out] == [str(x.id)]
        out = await cog.done_autocomplete(make_inter(autocomplete=True), f"#{x.id}")
        assert str(x.id) in [c.value for c in out]
        out = await cog.done_autocomplete(make_inter(autocomplete=True), "存在しない語")
        assert out == []

    run(go())


def test_autocomplete_service_error_returns_empty(cog, svc, monkeypatch):
    monkeypatch.setattr(svc, "list_todos", MagicMock(side_effect=RuntimeError("boom")))

    async def go():
        assert await cog.done_autocomplete(make_inter(autocomplete=True), "") == []

    run(go())


def test_choice_name_is_within_discord_limit():
    t = Todo(id=3, owner_id=1, title="x" * 300, tz="Asia/Tokyo", due=jst(2026, 11, 3, 18))
    name = todo_mod.todo_choice_name(t)
    assert name.startswith("#3 xxx") and len(name) <= 100


# ===== /todo list ==============================================================================
def test_list_empty_has_no_view(cog):
    async def go():
        inter = await open_list(cog)
        kw = inter.response.send_message.await_args.kwargs
        assert kw["embed"].description == "TODO はありません。"
        assert kw["view"] is discord.utils.MISSING
        assert_private_and_no_mentions(inter)

    run(go())


def test_list_headings_per_status(cog, svc):
    svc.add_todo("a")

    async def go():
        for status, label in todo_mod.STATUS_LABELS.items():
            inter = await open_list(cog, status)
            title = inter.response.send_message.await_args.kwargs["embed"].title
            assert title == f"TODO 一覧({label})"

    run(go())


def test_list_status_filters(cog, svc):
    open_ = svc.add_todo("o", due=jst(2026, 11, 3, 18))
    nodue = svc.add_todo("n")
    over = svc.add_todo("v", due=jst(2026, 10, 29, 9))
    done = svc.add_todo("d")
    svc.complete_todo(done.id)
    everything = {open_.id, nodue.id, over.id, done.id}
    expected = {
        "open": {open_.id, nodue.id, over.id},
        "overdue": {over.id},
        "done": {done.id},
        "no_due": {nodue.id},
        "all": everything,
    }

    async def go():
        for status, ids in expected.items():
            inter = await open_list(cog, status)
            desc = inter.response.send_message.await_args.kwargs["embed"].description
            assert {i for i in everything if f"`#{i}`" in desc} == ids, status

    run(go())


def test_list_invalid_status_replies_error(cog):
    async def go():
        inter = make_inter()
        await cog.list_todos.callback(cog, inter, "bogus")
        assert "状態" in reply_text(inter)

    run(go())


def test_list_view_button_count_single_page(cog, svc):
    add_n(svc, 3)

    async def go():
        view = view_of(await open_list(cog))
        assert labels(view) == [f"完了 #{t.id}" for t in svc.list_todos("open")]

    run(go())


def test_list_done_only_has_no_buttons(cog, svc):
    t = svc.add_todo("a")
    svc.complete_todo(t.id)

    async def go():
        inter = await open_list(cog, "done")
        assert inter.response.send_message.await_args.kwargs["view"] is discord.utils.MISSING

    run(go())


def test_list_view_button_count_multi_page(cog, svc):
    n = todo_mod.embeds.TODO_PER_PAGE
    add_n(svc, n + 2)

    async def go():
        view = view_of(await open_list(cog))
        assert len(view.children) == n + 2  # n done buttons + prev + next
        assert labels(view)[-2:] == ["前へ", "次へ"]
        assert find(view, "前へ").disabled is True
        assert find(view, "次へ").disabled is False

    run(go())


def test_list_done_button_removes_item(cog, svc):
    a, b = add_n(svc, 2)

    async def go():
        view = view_of(await open_list(cog))
        click = make_inter()
        await press(view, find(view, f"完了 #{a.id}"), click)
        kw = click.response.edit_message.await_args.kwargs
        assert f"`#{a.id}`" not in kw["embed"].description
        assert f"`#{b.id}`" in kw["embed"].description
        assert labels(view) == [f"完了 #{b.id}"]
        assert_private_and_no_mentions(click)
        click2 = make_inter()
        await press(view, find(view, f"完了 #{b.id}"), click2)
        kw2 = click2.response.edit_message.await_args.kwargs
        assert kw2["view"] is None
        assert kw2["embed"].description == "TODO はありません。"

    run(go())
    assert svc.list_todos("open") == []


def test_list_done_button_in_all_status_marks_done_instead(cog, svc):
    a, b = add_n(svc, 2)

    async def go():
        view = view_of(await open_list(cog, "all"))
        click = make_inter()
        await press(view, find(view, f"完了 #{a.id}"), click)
        desc = click.response.edit_message.await_args.kwargs["embed"].description
        assert f"`#{a.id}` [済] t0" in desc
        assert f"`#{b.id}` t1" in desc
        assert labels(view) == [f"完了 #{b.id}"]

    run(go())


def test_list_done_button_for_todo_completed_elsewhere_is_harmless(cog, svc):
    a, b = add_n(svc, 2)

    async def go():
        view = view_of(await open_list(cog))
        svc.complete_todo(a.id)  # e.g. via /todo done in the meantime
        click = make_inter()
        await press(view, find(view, f"完了 #{a.id}"), click)
        assert_private_and_no_mentions(click)
        assert labels(view) == [f"完了 #{b.id}"]

    run(go())
    assert [t.id for t in svc.list_todos("open")] == [b.id]


def test_list_done_button_for_todo_deleted_meanwhile(cog, svc):
    a, _ = add_n(svc, 2)

    async def go():
        view = view_of(await open_list(cog))
        svc.delete_todo(a.id)
        click = make_inter()
        await press(view, find(view, f"完了 #{a.id}"), click)
        assert "見つかりません" in reply_text(click)
        click.response.edit_message.assert_not_called()

    run(go())


def test_list_paging_navigation(cog, svc):
    n = todo_mod.embeds.TODO_PER_PAGE
    todos = add_n(svc, n + 2)

    async def go():
        view = view_of(await open_list(cog))
        click = make_inter()
        await press(view, find(view, "次へ"), click)
        assert view.page == 1
        e = click.response.edit_message.await_args.kwargs["embed"]
        assert e.footer.text.startswith("2/2")
        assert f"`#{todos[n].id}`" in e.description
        assert find(view, "次へ").disabled is True and find(view, "前へ").disabled is False
        await press(view, find(view, "前へ"), make_inter())
        assert view.page == 0

    run(go())


def test_list_page_clamped_when_last_page_emptied(cog, svc):
    n = todo_mod.embeds.TODO_PER_PAGE
    todos = add_n(svc, n + 1)  # second page holds a single item

    async def go():
        view = view_of(await open_list(cog))
        await press(view, find(view, "次へ"), make_inter())
        assert view.page == 1
        click = make_inter()
        await press(view, find(view, f"完了 #{todos[n].id}"), click)
        assert view.page == 0 and view.pages == 1
        e = click.response.edit_message.await_args.kwargs["embed"]
        assert e.footer.text == f"1/1 ページ・全 {n} 件"
        assert "前へ" not in labels(view) and len(view.children) == n

    run(go())


def test_list_timeout_removes_buttons(cog, svc):
    add_n(svc, 2)

    async def go():
        view = view_of(await open_list(cog))
        await view.on_timeout()
        view.message.edit.assert_awaited_once_with(view=None)

    run(go())


def test_list_timeout_edit_failure_is_swallowed(cog, svc):
    add_n(svc, 2)

    async def go():
        view = view_of(await open_list(cog))
        view.message.edit.side_effect = discord.HTTPException(MagicMock(status=404), "gone")
        await view.on_timeout()

    run(go())


def test_list_view_empty_todos_clamps_page(cog):
    async def go():
        view = TodoListView(cog, [], "h")
        assert view.page == 0 and view.pages == 1 and not view.has_components

    run(go())


# ===== setup / unload ==========================================================================
def make_bot(db_path, db=None):
    bot = MagicMock()
    bot.config.db_path = db_path
    bot.config.owner_user_id = 1
    bot.config.tz_default = "Asia/Tokyo"
    bot.config.allowed_user_ids = frozenset({ALLOWED_ID})
    bot.add_cog = AsyncMock()
    if db is None:
        del bot.db  # getattr(bot, "db", None) -> None
    else:
        bot.db = db
    return bot


def test_setup_shares_existing_db_and_does_not_close_it(tmp_path):
    shared = Database.open(tmp_path / "bot.db")
    bot = make_bot(tmp_path / "bot.db", shared)

    async def go():
        await todo_mod.setup(bot)
        cog = bot.add_cog.await_args.args[0]
        assert isinstance(cog, TodoCog)
        assert cog.service._db is shared
        await cog.cog_unload()
        await cog.cog_unload()

    run(go())
    # still usable => not closed by the cog
    assert shared.query_one("SELECT COUNT(*) AS c FROM todos")["c"] == 0
    shared.close()


def test_setup_uses_bot_db(tmp_path):
    shared = MagicMock()
    bot = make_bot(tmp_path / "bot.db", db=shared)

    run(todo_mod.setup(bot))
    cog = bot.add_cog.await_args.args[0]
    assert not hasattr(cog, "_db_to_close")
    shared.close.assert_not_called()


def test_setup_requires_open_bot_db(tmp_path):
    bot = make_bot(tmp_path / "bot.db")
    bot.db = None
    with pytest.raises(RuntimeError):
        run(todo_mod.setup(bot))
    bot.add_cog.assert_not_called()


# ===== unexpected errors / lock / stop ==========================================================
def fail_once(monkeypatch, obj, name):
    real = getattr(obj, name)
    calls = {"n": 0}

    def wrapper(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("unexpected db failure")
        return real(*a, **kw)

    monkeypatch.setattr(obj, name, wrapper)


def test_done_button_unexpected_error_allows_retry(cog, svc, monkeypatch):
    fail_once(monkeypatch, svc, "complete_todo")

    async def go():
        view = view_of(await add(cog))
        with pytest.raises(RuntimeError):
            await press(view, view.children[0], make_inter())
        assert view.finished is False
        click = make_inter()
        await press(view, view.children[0], click)
        assert click.response.edit_message.await_args.kwargs["content"] == "完了にしました。"

    run(go())
    assert svc.list_todos("open") == []


def test_delete_confirm_unexpected_error_allows_retry(cog, svc, monkeypatch):
    t = svc.add_todo("a")
    fail_once(monkeypatch, svc, "delete_todo")

    async def go():
        inter = make_inter()
        await cog.delete.callback(cog, inter, str(t.id))
        view = view_of(inter)
        with pytest.raises(RuntimeError):
            await press(view, view.confirm, make_inter())
        assert view.finished is False
        click = make_inter()
        await press(view, view.confirm, click)
        assert "削除しました" in click.response.edit_message.await_args.kwargs["content"]

    run(go())
    assert svc.list_todos("all") == []


def test_view_on_error_ephemeral_without_message(cog):
    async def go():
        view = view_of(await add(cog))
        inter = make_inter()
        inter.client.notify_admin = AsyncMock()
        await view.on_error(inter, RuntimeError("SECRET-MSG"), view.children[0])
        c = inter.response.send_message.await_args
        assert c.kwargs["ephemeral"] is True and "SECRET-MSG" not in str(c)
        inter.client.notify_admin.assert_awaited_once()

    run(go())


def test_press_updates_view_message_each_time(cog):
    async def go():
        view = view_of(await add(cog))
        first = make_inter()
        first.message = MagicMock()
        view.finished = True
        await press(view, view.children[0], first)  # refused, message untouched
        view.finished = False
        await press(view, view.children[0], first)
        assert view.message is first.message

    run(go())


def test_timeout_after_finish_is_noop(cog):
    async def go():
        view = view_of(await add(cog))
        click = make_inter()
        click.message = MagicMock()
        click.message.edit = AsyncMock()
        await press(view, view.children[0], click)
        await view.on_timeout()
        click.message.edit.assert_not_called()

    run(go())


def test_list_view_serializes_concurrent_presses(cog, svc):
    a, b = add_n(svc, 2)

    async def go():
        view = view_of(await open_list(cog))
        assert isinstance(view.lock, asyncio.Lock)
        ia, ib = make_inter(), make_inter()
        ba, bb = find(view, f"完了 #{a.id}"), find(view, f"完了 #{b.id}")
        await asyncio.gather(press(view, ba, ia), press(view, bb, ib))
        assert view.todos == []
        assert view.is_finished()
        assert not view.lock.locked()
        return ia, ib

    ia, ib = run(go())
    assert svc.list_todos("open") == []
    kws = [i.response.edit_message.await_args.kwargs for i in (ia, ib)]
    assert any(k["view"] is None for k in kws)


def test_list_view_stops_when_no_items_left(cog, svc):
    (a,) = add_n(svc, 1)

    async def go():
        view = view_of(await open_list(cog))
        assert not view.is_finished()
        await press(view, find(view, f"完了 #{a.id}"), make_inter())
        assert view.finished is True and view.is_finished()
        assert not view.has_components

    run(go())


def test_list_view_keeps_running_with_remaining_items(cog, svc):
    a, b = add_n(svc, 2)

    async def go():
        view = view_of(await open_list(cog))
        await press(view, find(view, f"完了 #{a.id}"), make_inter())
        assert not view.is_finished() and view.has_components

    run(go())
