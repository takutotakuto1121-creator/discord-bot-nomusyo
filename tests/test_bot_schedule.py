import asyncio
import dataclasses
import logging
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import discord
import pytest

from nomusyo.bot.checks import DENIED_MESSAGE, AllowedCommandTree
from nomusyo.bot.cogs import schedule as sched
from nomusyo.bot.cogs.schedule import (
    MSG_BAD_REF,
    MSG_CANCELLED,
    MSG_EXPIRED,
    AddConfirmView,
    DeleteConfirmView,
    DraftModal,
    EditModal,
    ListView,
    ScheduleCog,
)
from nomusyo.schedule.clock import FixedClock
from nomusyo.schedule.errors import NotFoundError, ScheduleError, ValidationError
from nomusyo.schedule.models import ReminderSpec, Window
from nomusyo.schedule.service import ScheduleService
from nomusyo.storage import Database

JST = ZoneInfo("Asia/Tokyo")
NOW = datetime(2026, 10, 29, 12, 0, tzinfo=JST)  # Thursday
ALLOWED_ID = 100
DENIED_ID = 999


def jst(*a):
    return datetime(*a, tzinfo=JST)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db(tmp_path):
    d = Database.open(tmp_path / "s.db")
    yield d
    d.close()


@pytest.fixture
def svc(db):
    return ScheduleService(db, 1, clock=FixedClock(NOW))


@pytest.fixture
def cog(svc):
    return ScheduleCog(MagicMock(), svc, {ALLOWED_ID})


def count_events(db, *, include_deleted=False):
    sql = "SELECT COUNT(*) AS c FROM events"
    if not include_deleted:
        sql += " WHERE deleted_at_utc IS NULL"
    return db.query_one(sql)["c"]


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
    inter.response.send_modal = AsyncMock()
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
    assert calls, "expected at least one response"
    for c in inter.response.send_message.await_args_list + inter.followup.send.await_args_list:
        assert c.kwargs.get("ephemeral") is True
    for c in calls:
        am = c.kwargs.get("allowed_mentions")
        assert isinstance(am, discord.AllowedMentions)
        assert am.everyone is False and am.users is False and am.roles is False
        assert am.replied_user is False


def reply_text(inter):
    c = (all_calls(inter))[-1]
    return c.args[0] if c.args else c.kwargs.get("content")


async def press(view, item, inter):
    """Mimic the discord.py dispatch: interaction_check first, then the callback."""
    if await view.interaction_check(inter):
        await item.callback(inter)


async def submit(modal, inter):
    if await modal.interaction_check(inter):
        await modal.on_submit(inter)


def set_values(modal, **values):
    for name, value in values.items():
        getattr(modal, f"f_{name}")._value = value


async def start_add(cog, **kw):
    kw.setdefault("when", "11/3 15:00")
    kw.setdefault("title", "会議")
    inter = make_inter()
    await cog.add.callback(cog, inter, **kw)
    return inter


def preview_view(inter) -> AddConfirmView:
    return inter.response.send_message.await_args.kwargs["view"]


# ===== /add ====================================================================================
def test_add_shows_preview_without_writing_to_db(cog, db):
    async def go():
        inter = await start_add(cog, duration="1h", place="会議室", remind="10m")
        kw = inter.response.send_message.await_args.kwargs
        assert kw["ephemeral"] is True
        embed = kw["embed"]
        assert embed.title == "プレビュー: 会議"
        fields = {f.name: f.value for f in embed.fields}
        assert fields["日時"] == "2026-11-03(火) 15:00〜16:00"
        assert fields["場所"] == "会議室" and fields["リマインド"] == "10分前"
        assert isinstance(kw["view"], AddConfirmView)
        assert kw["view"].message is inter.original_response.return_value
        assert_private_and_no_mentions(inter)

    run(go())
    assert count_events(db) == 0


def test_add_save_stores_exactly_one_event(cog, svc, db):
    async def go():
        inter = await start_add(
            cog, duration="90m", place="会議室", note="メモ", remind="10m,1d@20:00"
        )
        view = preview_view(inter)
        click = make_inter()
        await press(view, view.save, click)
        assert click.response.edit_message.await_args.kwargs["content"] == "保存しました。"
        assert click.response.edit_message.await_args.kwargs["view"] is None
        assert_private_and_no_mentions(click)

    run(go())
    assert count_events(db) == 1
    ev = svc.get_event(1)
    assert ev.title == "会議" and ev.start == jst(2026, 11, 3, 15)
    assert ev.duration_min == 90 and ev.place == "会議室" and ev.note == "メモ"
    assert {r.spec for r in ev.reminders} == {
        ReminderSpec(offset_min=10),
        ReminderSpec(day_offset=1, at_local="20:00"),
    }


def test_add_relative_and_all_day_inputs(cog, svc):
    async def go():
        inter = await start_add(cog, when="明日 9:00", title="朝")
        v1 = preview_view(inter)
        assert v1.draft.start == jst(2026, 10, 30, 9) and not v1.draft.all_day
        inter2 = await start_add(cog, when="11/3", title="終日")
        v2 = preview_view(inter2)
        assert v2.draft.all_day and v2.draft.start == jst(2026, 11, 3)

    run(go())


def test_add_double_press_save_saves_once(cog, db):
    async def go():
        view = preview_view(await start_add(cog))
        first, second = make_inter(), make_inter()
        await press(view, view.save, first)
        await press(view, view.save, second)
        assert "完了" in reply_text(second)
        second.response.edit_message.assert_not_called()
        assert_private_and_no_mentions(second)

    run(go())
    assert count_events(db) == 1


def test_add_concurrent_save_saves_once(cog, db):
    async def go():
        view = preview_view(await start_add(cog))
        await asyncio.gather(
            press(view, view.save, make_inter()), press(view, view.save, make_inter())
        )

    run(go())
    assert count_events(db) == 1


def test_add_save_service_error_allows_retry(cog, svc, db, monkeypatch):
    async def go():
        view = preview_view(await start_add(cog))
        real = svc.add_event
        monkeypatch.setattr(svc, "add_event", MagicMock(side_effect=ValidationError("NG")))
        failed = make_inter()
        await press(view, view.save, failed)
        assert reply_text(failed) == "NG"
        assert view.finished is False
        assert_private_and_no_mentions(failed)
        monkeypatch.setattr(svc, "add_event", real)
        await press(view, view.save, make_inter())

    run(go())
    assert count_events(db) == 1


def test_add_cancel_does_not_save(cog, db):
    async def go():
        view = preview_view(await start_add(cog))
        click = make_inter()
        await press(view, view.cancel, click)
        assert click.response.edit_message.await_args.kwargs["content"] == MSG_CANCELLED
        late = make_inter()
        await press(view, view.save, late)  # save after cancel is refused
        assert "完了" in reply_text(late)

    run(go())
    assert count_events(db) == 0


def test_add_timeout_discards_and_edits_message(cog, db):
    async def go():
        inter = await start_add(cog)
        view = preview_view(inter)
        await view.on_timeout()
        view.message.edit.assert_awaited_once_with(content=MSG_EXPIRED, embed=None, view=None)
        late = make_inter()
        await press(view, view.save, late)
        assert "完了" in reply_text(late)

    run(go())
    assert count_events(db) == 0


def test_add_timeout_after_save_does_not_touch_message(cog):
    async def go():
        view = preview_view(await start_add(cog))
        await press(view, view.save, make_inter())
        await view.on_timeout()
        view.message.edit.assert_not_called()

    run(go())


def test_add_timeout_message_edit_failure_is_swallowed(cog, caplog):
    async def go():
        view = preview_view(await start_add(cog))
        view.message.edit.side_effect = discord.HTTPException(MagicMock(status=404), "gone")
        with caplog.at_level(logging.DEBUG):
            await view.on_timeout()

    run(go())
    assert "HTTPException" in caplog.text


def test_add_revise_opens_modal_and_updates_preview_without_saving(cog, db):
    async def go():
        view = preview_view(await start_add(cog, note="元メモ", place="A"))
        click = make_inter()
        await press(view, view.revise, click)
        modal = click.response.send_modal.await_args.args[0]
        assert isinstance(modal, DraftModal)
        assert modal.f_title.value == "会議" and modal.f_when.value == "2026-11-03 15:00"

        set_values(modal, title="新題", when="11/4 10:00", duration="2h", place="B", remind="1h")
        sub = make_inter()
        await submit(modal, sub)
        kw = sub.response.edit_message.await_args.kwargs
        assert kw["embed"].title == "プレビュー: 新題" and kw["view"] is view
        assert view.draft.start == jst(2026, 11, 4, 10)
        assert view.draft.duration_min == 120 and view.draft.place == "B"
        assert view.draft.note == "元メモ"  # the note is kept by the revise modal
        assert_private_and_no_mentions(sub)
        assert count_events(db) == 0
        await press(view, view.save, make_inter())

    run(go())
    assert count_events(db) == 1


def test_add_revise_invalid_keeps_previous_preview(cog, db):
    async def go():
        view = preview_view(await start_add(cog))
        before = view.draft
        click = make_inter()
        await press(view, view.revise, click)
        modal = click.response.send_modal.await_args.args[0]
        set_values(modal, when="でたらめ")
        sub = make_inter()
        await submit(modal, sub)
        assert "プレビューは変更されていません" in reply_text(sub)
        sub.response.edit_message.assert_not_called()
        assert view.draft is before
        assert_private_and_no_mentions(sub)

    run(go())
    assert count_events(db) == 0


def test_add_revise_modal_after_finish_is_refused(cog, db):
    async def go():
        view = preview_view(await start_add(cog))
        click = make_inter()
        await press(view, view.revise, click)
        modal = click.response.send_modal.await_args.args[0]
        await press(view, view.cancel, make_inter())
        sub = make_inter()
        await submit(modal, sub)
        assert "完了" in reply_text(sub)
        sub.response.edit_message.assert_not_called()
        late = make_inter()
        await press(view, view.revise, late)  # revise button after finish
        late.response.send_modal.assert_not_called()

    run(go())
    assert count_events(db) == 0


@pytest.mark.parametrize(
    ("kwargs", "needle"),
    [
        ({"when": "2026-10-28 09:00"}, "過去"),
        ({"when": "2026-10-29 11:59"}, "過去"),
        ({"when": "来週の火曜"}, "形式"),
        ({"when": "13/45 10:00"}, ""),
        ({"when": "2026-02-30"}, "存在しない"),
        ({"when": ""}, "指定"),
        ({"when": "11/3 25:00"}, ""),
        ({"title": "   "}, "タイトル"),
        ({"title": "あ" * 201}, "200文字以内"),
        ({"place": "あ" * 201}, "場所"),
        ({"note": "あ" * 2001}, "メモ"),
        ({"duration": "abc"}, ""),
        ({"duration": "99999d"}, ""),
        ({"remind": "xyz"}, ""),
        ({"remind": ",".join(f"{i}m" for i in range(1, 12))}, ""),
    ],
)
def test_add_invalid_input_replies_ephemeral_error_and_writes_nothing(cog, db, kwargs, needle):
    async def go():
        inter = await start_add(cog, **kwargs)
        assert inter.response.send_message.await_count == 1
        kw = inter.response.send_message.await_args.kwargs
        assert "view" not in kw and "embed" not in kw  # no preview, no buttons
        text = reply_text(inter)
        assert text and needle in text
        assert_private_and_no_mentions(inter)

    run(go())
    assert count_events(db) == 0


def test_add_error_message_does_not_echo_user_input(cog):
    async def go():
        inter = await start_add(cog, when="@everyone <@123>")
        assert "@everyone" not in reply_text(inter)

    run(go())


def test_add_preview_neutralizes_mentions_in_embed(cog):
    async def go():
        inter = await start_add(cog, title="@everyone <@1>", place="<#2>", note="@here")
        embed = inter.response.send_message.await_args.kwargs["embed"]
        texts = [embed.title, *[f.value for f in embed.fields]]
        assert all("@everyone" not in t and "<@" not in t and "<#" not in t for t in texts)
        assert all("@here" not in t for t in texts)

    run(go())


def test_add_uses_followup_when_response_already_done(cog):
    async def go():
        inter = make_inter(done=True)
        await cog.add.callback(cog, inter, when="2020-01-01", title="x")
        inter.followup.send.assert_awaited_once()
        inter.response.send_message.assert_not_called()
        assert_private_and_no_mentions(inter)

    run(go())


# ===== /edit ===================================================================================
def add_past_event(svc, **kw):
    kw.setdefault("duration_min", 60)
    kw.setdefault("place", "旧場所")
    kw.setdefault("note", "旧メモ")
    return svc.add_event("過去の会議", jst(2026, 10, 1, 10), **kw)


async def open_edit(cog, event_id):
    inter = make_inter()
    await cog.edit.callback(cog, inter, event=str(event_id))
    return inter


def test_edit_opens_modal_prefilled(cog, svc):
    ev = add_past_event(svc)

    async def go():
        inter = await open_edit(cog, ev.id)
        modal = inter.response.send_modal.await_args.args[0]
        assert isinstance(modal, EditModal)
        assert modal.f_title.value == "過去の会議"
        assert modal.f_when.value == "2026-10-01 10:00"
        assert modal.f_duration.value == "1h"
        assert modal.f_place.value == "旧場所" and modal.f_note.value == "旧メモ"

    run(go())


def test_edit_unchanged_when_is_not_revalidated_for_past_event(cog, svc):
    ev = add_past_event(svc)

    async def go():
        modal = (await open_edit(cog, ev.id)).response.send_modal.await_args.args[0]
        set_values(modal, title="題名だけ修正")
        sub = make_inter()
        await submit(modal, sub)
        kw = sub.response.send_message.await_args.kwargs
        assert kw["embed"].title == "題名だけ修正"
        assert sub.response.send_message.await_args.args[0] == "更新しました。"
        assert_private_and_no_mentions(sub)

    run(go())
    got = svc.get_event(ev.id)
    assert got.title == "題名だけ修正" and got.start == jst(2026, 10, 1, 10)
    assert got.duration_min == 60 and got.place == "旧場所"


def test_edit_changed_when_to_past_is_rejected(cog, svc):
    ev = add_past_event(svc)

    async def go():
        modal = (await open_edit(cog, ev.id)).response.send_modal.await_args.args[0]
        set_values(modal, title="変更", when="2026-10-02 10:00")
        sub = make_inter()
        await submit(modal, sub)
        assert "過去" in reply_text(sub)
        assert_private_and_no_mentions(sub)

    run(go())
    got = svc.get_event(ev.id)
    assert got.title == "過去の会議" and got.start == jst(2026, 10, 1, 10)


def test_edit_changed_when_to_future_updates_start_and_all_day(cog, svc):
    ev = add_past_event(svc)

    async def go():
        modal = (await open_edit(cog, ev.id)).response.send_modal.await_args.args[0]
        set_values(modal, when="11/5")
        await submit(modal, make_inter())

    run(go())
    got = svc.get_event(ev.id)
    assert got.all_day is True and got.start == jst(2026, 11, 5)


def test_edit_blank_fields_clear_duration_place_note(cog, svc):
    ev = add_past_event(svc)

    async def go():
        modal = (await open_edit(cog, ev.id)).response.send_modal.await_args.args[0]
        set_values(modal, duration="", place="  ", note="")
        sub = make_inter()
        await submit(modal, sub)
        assert sub.response.send_message.await_args.args[0] == "更新しました。"

    run(go())
    got = svc.get_event(ev.id)
    assert got.duration_min is None and got.place is None and got.note is None
    assert got.title == "過去の会議"


@pytest.mark.parametrize(
    ("values", "needle"),
    [
        ({"title": ""}, "タイトル"),
        ({"title": "あ" * 201}, "200文字以内"),
        ({"place": "あ" * 201}, "場所"),
        ({"note": "あ" * 2001}, "メモ"),
        ({"duration": "xx"}, ""),
        ({"when": "でたらめ"}, "形式"),
    ],
)
def test_edit_invalid_values_rejected_and_unchanged(cog, svc, values, needle):
    ev = add_past_event(svc)

    async def go():
        modal = (await open_edit(cog, ev.id)).response.send_modal.await_args.args[0]
        set_values(modal, **values)
        sub = make_inter()
        await submit(modal, sub)
        assert needle in reply_text(sub)
        assert_private_and_no_mentions(sub)

    run(go())
    assert svc.get_event(ev.id).title == "過去の会議"
    assert svc.get_event(ev.id).note == "旧メモ"


@pytest.mark.parametrize("ref", ["999", "#999"])
def test_edit_nonexistent_id_replies_not_found(cog, ref):
    async def go():
        inter = make_inter()
        await cog.edit.callback(cog, inter, event=ref)
        inter.response.send_modal.assert_not_called()
        assert "見つかりません" in reply_text(inter)
        assert_private_and_no_mentions(inter)

    run(go())


@pytest.mark.parametrize("ref", ["", "abc", "1; DROP TABLE events", "１２", "-1", "1.5", "0x10"])
def test_edit_and_delete_malformed_reference_rejected(cog, svc, db, ref):
    add_past_event(svc)

    async def go():
        for command in (cog.edit, cog.delete):
            inter = make_inter()
            await command.callback(cog, inter, event=ref)
            assert reply_text(inter) == MSG_BAD_REF or "見つかりません" in reply_text(inter)
            inter.response.send_modal.assert_not_called()
            assert "view" not in inter.response.send_message.await_args.kwargs
            assert_private_and_no_mentions(inter)

    run(go())
    assert count_events(db) == 1


def test_edit_deleted_event_is_not_found(cog, svc):
    ev = add_past_event(svc)
    svc.delete_event(ev.id)

    async def go():
        inter = make_inter()
        await cog.edit.callback(cog, inter, event=str(ev.id))
        assert "見つかりません" in reply_text(inter)

    run(go())


def test_edit_modal_submit_for_event_deleted_meanwhile_replies_not_found(cog, svc):
    ev = add_past_event(svc)

    async def go():
        modal = (await open_edit(cog, ev.id)).response.send_modal.await_args.args[0]
        svc.delete_event(ev.id)
        sub = make_inter()
        await submit(modal, sub)
        assert "見つかりません" in reply_text(sub)

    run(go())


# ===== /delete =================================================================================
async def open_delete(cog, event_id):
    inter = make_inter()
    await cog.delete.callback(cog, inter, event=str(event_id))
    return inter


def window_all():
    return Window(jst(2026, 1, 1), jst(2027, 12, 31))


def test_delete_shows_confirmation_and_does_not_delete(cog, svc, db):
    ev = add_past_event(svc)

    async def go():
        inter = await open_delete(cog, ev.id)
        kw = inter.response.send_message.await_args.kwargs
        assert isinstance(kw["view"], DeleteConfirmView)
        assert kw["embed"].title == "削除確認: 過去の会議"
        assert kw["embed"].color.value == sched.embeds.COLOR_DANGER
        assert_private_and_no_mentions(inter)

    run(go())
    assert svc.get_event(ev.id).title == "過去の会議"
    assert [o.event_id for o in svc.expand(window_all())] == [ev.id]
    assert count_events(db) == 1


def test_delete_confirm_removes_from_get_and_expand(cog, svc, db):
    ev = add_past_event(svc)
    keep = svc.add_event("残す", jst(2026, 11, 3, 15))

    async def go():
        view = (await open_delete(cog, ev.id)).response.send_message.await_args.kwargs["view"]
        click = make_inter()
        await press(view, view.confirm, click)
        kw = click.response.edit_message.await_args.kwargs
        assert kw["content"] == f"削除しました(#{ev.id})。" and kw["view"] is None
        assert_private_and_no_mentions(click)

    run(go())
    with pytest.raises(NotFoundError):
        svc.get_event(ev.id)
    assert [o.event_id for o in svc.expand(window_all())] == [keep.id]
    assert count_events(db) == 1 and count_events(db, include_deleted=True) == 2  # soft delete


def test_delete_double_confirm_is_refused_second_time(cog, svc):
    ev = add_past_event(svc)

    async def go():
        view = (await open_delete(cog, ev.id)).response.send_message.await_args.kwargs["view"]
        await press(view, view.confirm, make_inter())
        second = make_inter()
        await press(view, view.confirm, second)
        assert "完了" in reply_text(second)
        second.response.edit_message.assert_not_called()

    run(go())


def test_delete_cancel_and_timeout_keep_event(cog, svc):
    ev = add_past_event(svc)

    async def go():
        view = (await open_delete(cog, ev.id)).response.send_message.await_args.kwargs["view"]
        click = make_inter()
        await press(view, view.cancel, click)
        assert click.response.edit_message.await_args.kwargs["content"] == MSG_CANCELLED
        late = make_inter()
        await press(view, view.confirm, late)
        assert "完了" in reply_text(late)

        view2 = (await open_delete(cog, ev.id)).response.send_message.await_args.kwargs["view"]
        await view2.on_timeout()
        view2.message.edit.assert_awaited_once()
        assert "削除しませんでした" in view2.message.edit.await_args.kwargs["content"]

    run(go())
    assert svc.get_event(ev.id).title == "過去の会議"


def test_delete_confirm_when_already_deleted_elsewhere_replies_error(cog, svc):
    ev = add_past_event(svc)

    async def go():
        view = (await open_delete(cog, ev.id)).response.send_message.await_args.kwargs["view"]
        svc.delete_event(ev.id)
        click = make_inter()
        await press(view, view.confirm, click)
        assert "見つかりません" in reply_text(click)
        assert view.finished is False
        assert_private_and_no_mentions(click)

    run(go())


def test_delete_nonexistent_id(cog):
    async def go():
        inter = await open_delete(cog, 12345)
        assert "見つかりません" in reply_text(inter)
        assert "view" not in inter.response.send_message.await_args.kwargs

    run(go())


# ===== /list ===================================================================================
def add_n_future(svc, n, title="予定"):
    out = []
    for i in range(n):
        out.append(svc.add_event(f"{title}{i}", jst(2026, 10, 30, 8, i)))
    return out


def test_list_empty(cog):
    async def go():
        inter = make_inter()
        await cog.list_events.callback(cog, inter)
        kw = inter.response.send_message.await_args.kwargs
        assert kw["embed"].description == "予定はありません。"
        assert kw["view"] is discord.utils.MISSING
        assert_private_and_no_mentions(inter)

    run(go())


def test_list_single_page_has_no_view(cog, svc):
    add_n_future(svc, 3)

    async def go():
        inter = make_inter()
        await cog.list_events.callback(cog, inter)
        kw = inter.response.send_message.await_args.kwargs
        assert kw["view"] is discord.utils.MISSING
        assert "予定0" in kw["embed"].description

    run(go())


def test_list_paging_buttons(cog, svc):
    add_n_future(svc, 11)

    async def go():
        inter = make_inter()
        await cog.list_events.callback(cog, inter)
        view = inter.response.send_message.await_args.kwargs["view"]
        assert isinstance(view, ListView) and view.pages == 2
        assert view.message is inter.original_response.return_value
        assert view.prev_button.disabled and not view.next_button.disabled
        click = make_inter()
        await press(view, view.next_button, click)
        embed = click.response.edit_message.await_args.kwargs["embed"]
        assert embed.footer.text == "2/2 ページ・全 11 件"
        assert view.next_button.disabled and not view.prev_button.disabled
        assert_private_and_no_mentions(click)
        await press(view, view.next_button, make_inter())  # clamped at the last page
        assert view.page == 1
        await press(view, view.prev_button, make_inter())
        assert view.page == 0

    run(go())


def test_list_query_filters_by_title_place_note_and_id(cog, svc):
    a = svc.add_event("歯医者", jst(2026, 10, 30, 8))
    svc.add_event("会議", jst(2026, 10, 30, 9), place="渋谷")
    svc.add_event("買い物", jst(2026, 10, 30, 10), note="ミルク")

    async def go():
        for query, expected in (("歯", ["歯医者"]), ("渋谷", ["会議"]), ("ミルク", ["買い物"])):
            inter = make_inter()
            await cog.list_events.callback(cog, inter, query=query)
            desc = inter.response.send_message.await_args.kwargs["embed"].description
            assert [t for t in ("歯医者", "会議", "買い物") if t in desc] == expected
        inter = make_inter()
        await cog.list_events.callback(cog, inter, query=f"#{a.id}")
        assert "歯医者" in inter.response.send_message.await_args.kwargs["embed"].description

    run(go())


def test_list_range_today_excludes_tomorrow(cog, svc):
    svc.add_event("今日の予定", jst(2026, 10, 29, 18))
    svc.add_event("明日の予定", jst(2026, 10, 30, 9))

    async def go():
        inter = make_inter()
        await cog.list_events.callback(cog, inter, range="today")
        embed = inter.response.send_message.await_args.kwargs["embed"]
        assert "今日の予定" in embed.description and "明日の予定" not in embed.description
        assert "今日" in embed.title

    run(go())


def test_list_view_timeout_removes_buttons(cog, svc):
    add_n_future(svc, 11)

    async def go():
        inter = make_inter()
        await cog.list_events.callback(cog, inter)
        view = inter.response.send_message.await_args.kwargs["view"]
        await view.on_timeout()
        view.message.edit.assert_awaited_once_with(view=None)

    run(go())


# ===== autocomplete ============================================================================
async def complete(cog, current="", user_id=ALLOWED_ID):
    inter = make_inter(user_id, autocomplete=True)
    out = await cog.edit_autocomplete(inter, current)
    return inter, out


def test_autocomplete_denied_user_gets_empty_and_no_response(cog, svc):
    add_n_future(svc, 3)

    async def go():
        for fn in (cog.edit_autocomplete, cog.delete_autocomplete):
            inter = make_inter(DENIED_ID, autocomplete=True)
            assert await fn(inter, "") == []
            inter.response.send_message.assert_not_called()
            inter.followup.send.assert_not_called()

    run(go())


def test_autocomplete_limited_to_25(cog, svc):
    add_n_future(svc, 40)

    async def go():
        _, out = await complete(cog)
        assert len(out) == 25
        assert all(len(c.name) <= 100 for c in out)
        out2 = await cog.delete_autocomplete(make_inter(autocomplete=True), "")
        assert len(out2) == 25

    run(go())


def test_autocomplete_filters_by_query_and_returns_id_values(cog, svc):
    a = svc.add_event("歯医者", jst(2026, 10, 30, 8), place="駅前")
    svc.add_event("会議", jst(2026, 10, 30, 9))

    async def go():
        _, out = await complete(cog, "歯")
        assert [c.value for c in out] == [str(a.id)]
        assert out[0].name.startswith(f"#{a.id} 10/30 08:00 歯医者")
        _, out = await complete(cog, "駅前")
        assert [c.value for c in out] == [str(a.id)]
        _, out = await complete(cog, f"#{a.id}")
        assert [c.value for c in out] == [str(a.id)]
        _, out = await complete(cog, "存在しない語")
        assert out == []

    run(go())


def test_autocomplete_is_case_insensitive(cog, svc):
    svc.add_event("Dentist", jst(2026, 10, 30, 8))

    async def go():
        _, out = await complete(cog, "dENT")
        assert len(out) == 1

    run(go())


def test_autocomplete_orders_upcoming_before_recent_past_and_skips_old(cog, svc):
    past = svc.add_event("過去", jst(2026, 10, 20, 9))
    future = svc.add_event("未来", jst(2026, 11, 20, 9))
    svc.add_event("古すぎる", jst(2026, 8, 1, 9))  # more than 30 days back
    svc.add_event("遠すぎる", jst(2028, 1, 1, 9))  # more than 365 days ahead

    async def go():
        _, out = await complete(cog)
        assert [c.value for c in out] == [str(future.id), str(past.id)]

    run(go())


def test_autocomplete_excludes_deleted(cog, svc):
    ev = svc.add_event("消す", jst(2026, 10, 30, 8))
    svc.delete_event(ev.id)

    async def go():
        _, out = await complete(cog)
        assert out == []

    run(go())


def test_autocomplete_service_failure_returns_empty_and_logs_type_only(
    cog, svc, monkeypatch, caplog
):
    monkeypatch.setattr(svc, "expand", MagicMock(side_effect=RuntimeError("secret-detail")))

    async def go():
        with caplog.at_level(logging.DEBUG):
            _, out = await complete(cog)
        assert out == []

    run(go())
    assert "RuntimeError" in caplog.text and "secret-detail" not in caplog.text


# ===== allowed-user enforcement ================================================================
def assert_denied(inter):
    inter.response.send_message.assert_awaited_once()
    c = inter.response.send_message.await_args
    assert c.args[0] == DENIED_MESSAGE
    assert c.kwargs["ephemeral"] is True


def test_denied_user_cannot_use_add_buttons_and_modal(cog, db):
    async def go():
        view = preview_view(await start_add(cog))
        for item in (view.save, view.revise, view.cancel):
            bad = make_inter(DENIED_ID)
            await press(view, item, bad)
            assert_denied(bad)
            bad.response.edit_message.assert_not_called()
            bad.response.send_modal.assert_not_called()
        assert view.finished is False  # the denied cancel did not close the preview

        ok = make_inter()
        await press(view, view.revise, ok)
        modal = ok.response.send_modal.await_args.args[0]
        set_values(modal, title="乗っ取り")
        bad = make_inter(DENIED_ID)
        await submit(modal, bad)
        assert_denied(bad)
        assert view.draft.title == "会議"

    run(go())
    assert count_events(db) == 0


def test_denied_user_cannot_confirm_delete(cog, svc, db):
    ev = add_past_event(svc)

    async def go():
        view = (await open_delete(cog, ev.id)).response.send_message.await_args.kwargs["view"]
        for item in (view.confirm, view.cancel):
            bad = make_inter(DENIED_ID)
            await press(view, item, bad)
            assert_denied(bad)
        assert view.finished is False

    run(go())
    assert svc.get_event(ev.id).title == "過去の会議"


def test_denied_user_cannot_submit_edit_modal(cog, svc):
    ev = add_past_event(svc)

    async def go():
        modal = (await open_edit(cog, ev.id)).response.send_modal.await_args.args[0]
        set_values(modal, title="乗っ取り")
        bad = make_inter(DENIED_ID)
        await submit(modal, bad)
        assert_denied(bad)

    run(go())
    assert svc.get_event(ev.id).title == "過去の会議"


def test_denied_user_cannot_page_list(cog, svc):
    add_n_future(svc, 11)

    async def go():
        inter = make_inter()
        await cog.list_events.callback(cog, inter)
        view = inter.response.send_message.await_args.kwargs["view"]
        bad = make_inter(DENIED_ID)
        await press(view, view.next_button, bad)
        assert_denied(bad)
        assert view.page == 0

    run(go())


def test_denied_response_uses_followup_when_already_responded(cog):
    async def go():
        view = preview_view(await start_add(cog))
        bad = make_inter(DENIED_ID, done=True)
        await press(view, view.save, bad)
        bad.followup.send.assert_awaited_once()
        assert bad.followup.send.await_args.kwargs["ephemeral"] is True

    run(go())


def test_all_schedule_views_and_modals_use_allowed_bases(cog, svc):
    ev = add_past_event(svc)
    draft = cog.build_draft(when="11/3 15:00", title="x")

    async def go():
        views = [
            AddConfirmView(cog, draft),
            DeleteConfirmView(cog, ev),
            ListView(cog.allowed_user_ids, [], "h"),
        ]
        modals = [DraftModal(cog, views[0]), EditModal(cog, ev)]
        for obj in (*views, *modals):
            assert obj.allowed_user_ids == frozenset({ALLOWED_ID})
            assert await obj.interaction_check(make_inter(ALLOWED_ID)) is True
            assert await obj.interaction_check(make_inter(DENIED_ID)) is False

    run(go())


def test_schedule_commands_are_gated_by_the_allowed_command_tree(cog):
    from nomusyo.bot.app import NomusyoBot
    from nomusyo.bot.config import BotConfig
    from nomusyo.config import Config

    cfg = BotConfig(
        discord_bot_token="t" * 20,
        guild_id=1,
        allowed_user_ids=frozenset({ALLOWED_ID}),
        notify_channel_id=2,
        admin_user_id=ALLOWED_ID,
        news=Config(anthropic_api_key="k"),
    )

    async def go():
        bot = NomusyoBot(cfg)
        await bot.add_cog(cog)
        assert isinstance(bot.tree, AllowedCommandTree)
        names = {c.name for c in bot.tree.get_commands()}
        assert {"add", "list", "edit", "delete"} <= names
        for command in bot.tree.get_commands():
            if command.name in {"add", "list", "edit", "delete"}:
                assert command.guild_only is True
        bad = make_inter(DENIED_ID)
        assert await bot.tree.interaction_check(bad) is False
        assert_denied(bad)
        assert await bot.tree.interaction_check(make_inter(ALLOWED_ID)) is True

    run(go())


def test_denied_command_callbacks_are_not_run_by_tree_but_autocomplete_is_gated_too(cog, svc):
    # Commands themselves rely on the tree check; autocomplete performs its own check.
    add_n_future(svc, 2)

    async def go():
        _, out = await complete(cog, "", user_id=DENIED_ID)
        assert out == []

    run(go())


# ===== misc ====================================================================================
def test_build_draft_trims_and_normalizes(cog):
    d = cog.build_draft(when=" 11/3 15:00 ", title="  会議  ", place="  ", note="", duration="")
    assert d.title == "会議" and d.place is None and d.note is None
    assert d.duration_min is None and d.reminders == () and d.tz == "Asia/Tokyo"


def test_window_for_ranges_use_local_midnight(cog):
    w, label = cog.window_for("today")
    assert (w.start, w.end) == (jst(2026, 10, 29), jst(2026, 10, 30)) and label == "今日"
    w, _ = cog.window_for("week")
    assert w.end == jst(2026, 11, 5)


def test_setup_requires_open_bot_db():
    bot = MagicMock()
    bot.db = None
    bot.add_cog = AsyncMock()
    with pytest.raises(RuntimeError):
        run(sched.setup(bot))
    bot.add_cog.assert_not_called()


def test_cog_has_no_db_ownership():
    c = ScheduleCog(MagicMock(), MagicMock(), {1})
    assert not hasattr(c, "_db_to_close")


def test_helpers_reminders_text_roundtrip():
    specs = [ReminderSpec(offset_min=10), ReminderSpec(day_offset=1, at_local="20:00")]
    assert sched.reminders_text(specs) == "10m,1d@20:00"
    assert sched.parse_reminders(sched.reminders_text(specs)) == tuple(specs)


# ===== unexpected errors / expired vs finished / modal limits ==================================
def fail_once(monkeypatch, obj, name):
    real = getattr(obj, name)
    calls = {"n": 0}

    def wrapper(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("unexpected db failure")
        return real(*a, **kw)

    monkeypatch.setattr(obj, name, wrapper)
    return calls


def test_save_unexpected_error_allows_retry_and_saves_once(cog, svc, db, monkeypatch):
    fail_once(monkeypatch, svc, "add_event")

    async def go():
        view = preview_view(await start_add(cog))
        with pytest.raises(RuntimeError):
            await press(view, view.save, make_inter())
        assert view.finished is False
        again = make_inter()
        await press(view, view.save, again)
        assert again.response.edit_message.await_args.kwargs["content"] == "保存しました。"
        third = make_inter()
        await press(view, view.save, third)
        third.response.edit_message.assert_not_called()

    run(go())
    assert count_events(db) == 1


def test_delete_unexpected_error_allows_retry(cog, svc, monkeypatch):
    ev = add_past_event(svc)
    fail_once(monkeypatch, svc, "delete_event")

    async def go():
        view = (await open_delete(cog, ev.id)).response.send_message.await_args.kwargs["view"]
        with pytest.raises(RuntimeError):
            await press(view, view.confirm, make_inter())
        assert view.finished is False
        click = make_inter()
        await press(view, view.confirm, click)
        assert "削除しました" in click.response.edit_message.await_args.kwargs["content"]

    run(go())
    with pytest.raises(NotFoundError):
        svc.get_event(ev.id)


def test_view_on_error_is_ephemeral_without_message(cog):
    async def go():
        view = preview_view(await start_add(cog))
        inter = make_inter()
        inter.client.notify_admin = AsyncMock()
        await view.on_error(inter, RuntimeError("SECRET-MSG"), view.save)
        c = inter.response.send_message.await_args
        assert c.kwargs["ephemeral"] is True and "SECRET-MSG" not in str(c)
        inter.client.notify_admin.assert_awaited_once()

    run(go())


def test_timeout_sets_expired_not_finished(cog):
    async def go():
        view = preview_view(await start_add(cog))
        await view.on_timeout()
        assert view.expired is True and view.finished is False
        view.message.edit.assert_awaited_once()

    run(go())


def test_timeout_after_finish_does_not_expire(cog):
    async def go():
        view = preview_view(await start_add(cog))
        await press(view, view.cancel, make_inter())
        await view.on_timeout()
        assert view.finished is True and view.expired is False
        view.message.edit.assert_not_called()

    run(go())


def test_pressing_after_expired_is_refused_and_does_not_save(cog, db):
    async def go():
        view = preview_view(await start_add(cog))
        await view.on_timeout()
        late = make_inter()
        await press(view, view.save, late)
        late.response.edit_message.assert_not_called()

    run(go())
    assert count_events(db) == 0


def test_press_updates_view_message_each_time(cog):
    async def go():
        view = preview_view(await start_add(cog))
        first = make_inter()
        first.message = MagicMock()
        await press(view, view.revise, first)
        assert view.message is first.message
        second = make_inter()
        second.message = MagicMock()
        await press(view, view.revise, second)
        assert view.message is second.message

    run(go())


def test_edit_modal_limits_and_timeout(cog, svc):
    ev = add_past_event(svc)
    m = EditModal(cog, ev)
    assert m.timeout == sched.PREVIEW_TIMEOUT
    assert m.f_title.max_length == sched.svc.MAX_TITLE
    assert m.f_note.max_length == sched.svc.MAX_NOTE
    assert m.f_place.max_length == sched.svc.MAX_PLACE


def test_draft_modal_timeout_is_preview_timeout(cog):
    async def go():
        view = preview_view(await start_add(cog))
        assert DraftModal(cog, view).timeout == sched.PREVIEW_TIMEOUT

    run(go())


@pytest.mark.parametrize(
    ("field", "limit"), [("title", "MAX_TITLE"), ("note", "MAX_NOTE"), ("place", "MAX_PLACE")]
)
def test_edit_modal_overlong_initial_value_is_error_not_truncated(cog, svc, field, limit):
    ev = add_past_event(svc)
    too_long = dataclasses.replace(ev, **{field: "あ" * (getattr(sched.svc, limit) + 1)})
    with pytest.raises(ScheduleError):
        EditModal(cog, too_long)


def test_edit_modal_value_at_limit_is_kept_intact(cog, svc):
    ev = add_past_event(svc, note="あ" * sched.svc.MAX_NOTE)
    assert EditModal(cog, ev).f_note.default == "あ" * sched.svc.MAX_NOTE
