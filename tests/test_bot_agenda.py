import asyncio
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import discord
import pytest

from nomusyo.bot import embeds
from nomusyo.bot.checks import DENIED_MESSAGE, AllowedCommandTree
from nomusyo.bot.cogs import agenda as agenda_mod
from nomusyo.bot.cogs import todo as todo_mod
from nomusyo.bot.cogs.agenda import MSG_FAILED, AgendaCog
from nomusyo.bot.cogs.todo import TodoCog, TodoListView
from nomusyo.schedule.agenda import Agenda, AgendaDay, Holiday, build_agenda
from nomusyo.schedule.clock import FixedClock
from nomusyo.schedule.errors import ScheduleError
from nomusyo.schedule.models import Occurrence
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
    return AgendaCog(MagicMock(), svc, {ALLOWED_ID})


def make_inter(user_id=ALLOWED_ID):
    inter = MagicMock()
    inter.user.id = user_id
    inter.type = discord.InteractionType.application_command
    inter.response.is_done.return_value = False
    inter.response.send_message = AsyncMock()
    inter.response.edit_message = AsyncMock()
    inter.followup.send = AsyncMock()
    return inter


def sent(inter):
    c = inter.response.send_message.await_args
    assert c.kwargs["ephemeral"] is True
    am = c.kwargs["allowed_mentions"]
    assert isinstance(am, discord.AllowedMentions)
    assert am.everyone is False and am.users is False and am.roles is False
    return c


def fields(embed):
    return {f.name: f.value for f in embed.fields}


def occ(i, title, h=9, *, all_day=False, place=None):
    s = jst(2026, 10, 29, h, 0)
    return Occurrence(i, f"k{i}", s, s, title, all_day, place)


def mk_agenda(days=1, *, occs=(), holidays=(), overdue=(), upcoming=()):
    ds = []
    for i in range(days):
        d = date(2026, 10, 29) + timedelta(days=i)
        ds.append(
            AgendaDay(
                d,
                tuple(h for h in holidays if h.date == d),
                tuple(o for o in occs if i == 0),
            )
        )
    return Agenda(date(2026, 10, 29), tuple(ds), tuple(overdue), tuple(upcoming), "Asia/Tokyo")


# ===== embed ===================================================================================
def test_embed_empty_today_says_no_plans():
    e = embeds.agenda_embed(mk_agenda(1), heading="h", now=NOW)
    assert e.description == "予定・TODO はありません。"
    assert len(e.fields) == 0


def test_embed_empty_week_omits_empty_days():
    e = embeds.agenda_embed(mk_agenda(7), heading="h", now=NOW)
    assert len(e.fields) == 0
    assert e.description


def test_embed_day_with_nothing_but_todo_today_shows_no_plans(svc):
    svc.add_todo("t", due=jst(2026, 10, 29, 20, 0))
    e = embeds.agenda_embed(build_agenda(svc), heading="h", now=NOW)
    f = fields(e)
    assert f["2026-10-29(木)"] == "予定はありません。"
    assert "期限が近い TODO" in f


def test_embed_week_only_nonempty_days_and_todo_sections(svc):
    svc.add_event("mtg", jst(2026, 10, 31, 10, 0), place="会議室")
    svc.add_todo("old", due=jst(2026, 10, 28, 9, 0))
    svc.add_todo("soon", due=jst(2026, 11, 1, 9, 0))
    e = embeds.agenda_embed(build_agenda(svc, days=7), heading="h", now=NOW)
    f = fields(e)
    assert list(f) == ["2026-10-31(土)", "期限切れの TODO", "期限が近い TODO"]
    assert "10:00 mtg @\u200b会議室" in f["2026-10-31(土)"]
    assert "old" in f["期限切れの TODO"] and "soon" in f["期限が近い TODO"]
    assert e.footer.text == "Asia/Tokyo"


def test_embed_holiday_line_and_order():
    h = Holiday(date(2026, 10, 29), "祝日X")
    e = embeds.agenda_embed(
        mk_agenda(7, occs=[occ(1, "終日", all_day=True)], holidays=[h]), heading="h", now=NOW
    )
    lines = fields(e)["2026-10-29(木)"].split("\n")
    assert lines[0] == "祝日: 祝日X"
    assert lines[1].startswith("終日 終日")


def test_embed_holiday_only_day_shown_in_week():
    h = Holiday(date(2026, 10, 31), "祝")
    e = embeds.agenda_embed(mk_agenda(7, holidays=[h]), heading="h", now=NOW)
    assert fields(e) == {"2026-10-31(土)": "祝日: 祝"}


def test_embed_field_limit_and_omitted_count():
    occs = [occ(i, "x" * 100) for i in range(1, 40)]
    e = embeds.agenda_embed(mk_agenda(1, occs=occs), heading="h", now=NOW)
    value = e.fields[0].value
    assert len(value) <= embeds.MAX_FIELD_VALUE
    last = value.split("\n")[-1]
    assert last.startswith("…ほか ") and last.endswith(" 件")
    shown = len(value.split("\n")) - 1
    assert last == f"…ほか {39 - shown} 件"


def test_embed_total_limit_with_many_todos(svc):
    for i in range(60):
        svc.add_todo(f"{i}" + "あ" * 150, due=jst(2026, 10, 28, 9, 0))
    for i in range(60):
        svc.add_todo(f"u{i}" + "い" * 150, due=jst(2026, 10, 30, 9, 0))
    for i in range(30):
        svc.add_event("e" * 150, jst(2026, 10, 29, 9, 0) + timedelta(days=i % 7, minutes=i))
    e = embeds.agenda_embed(build_agenda(svc, days=7), heading="h" * 300, now=NOW)
    assert len(e) <= embeds.MAX_EMBED_TOTAL
    assert all(len(f.value) <= embeds.MAX_FIELD_VALUE for f in e.fields)
    assert len(e.title) <= embeds.MAX_TITLE


def test_embed_neutralizes_mentions():
    h = Holiday(date(2026, 10, 29), "@everyone")
    e = embeds.agenda_embed(
        mk_agenda(
            1,
            occs=[occ(1, "@everyone <@123> <@&5>", place="@here")],
            holidays=[h],
        ),
        heading="@everyone",
        now=NOW,
    )
    text = e.title + "".join(f.value for f in e.fields)
    for bad in ("@everyone", "@here", "<@", "<@&"):
        assert bad not in text


def test_embed_neutralizes_todo_title(svc):
    svc.add_todo("@everyone <@1>", due=jst(2026, 10, 28, 9, 0))
    e = embeds.agenda_embed(build_agenda(svc), heading="h", now=NOW)
    text = "".join(f.value for f in e.fields)
    assert "@everyone" not in text and "<@" not in text


# ===== cog =====================================================================================
def test_today_and_week_ephemeral_no_mentions(cog, svc):
    svc.add_event("@everyone mtg", jst(2026, 10, 29, 15, 0))

    async def go():
        t = make_inter()
        await cog.today.callback(cog, t)
        w = make_inter()
        await cog.week.callback(cog, w)
        return t, w

    t, w = run(go())
    et = sent(t).kwargs["embed"]
    ew = sent(w).kwargs["embed"]
    assert "@everyone" not in "".join(f.value for f in et.fields)
    assert et.title == "今日の予定とTODO"
    assert ew.title == "今後7日間の予定とTODO"
    assert not ew.fields or ew.fields[0].name == "2026-10-29(木)"


def test_today_empty_message(cog):
    inter = make_inter()
    run(cog.today.callback(cog, inter))
    assert sent(inter).kwargs["embed"].description == "予定・TODO はありません。"


def test_week_days_covered(cog, svc):
    svc.add_event("last", jst(2026, 11, 4, 9, 0))
    svc.add_event("outside", jst(2026, 11, 5, 9, 0))
    inter = make_inter()
    run(cog.week.callback(cog, inter))
    f = fields(sent(inter).kwargs["embed"])
    assert list(f) == ["2026-11-04(水)"]


def test_denied_user_rejected_by_command_tree():
    tree = AllowedCommandTree(
        discord.Client(intents=discord.Intents.none()), allowed_user_ids={ALLOWED_ID}
    )
    inter = make_inter(DENIED_ID)
    assert run(tree.interaction_check(inter)) is False
    assert inter.response.send_message.await_args.args[0] == DENIED_MESSAGE
    assert run(tree.interaction_check(make_inter(ALLOWED_ID))) is True


def test_schedule_error_message_shown(cog, monkeypatch):
    def boom(*a, **k):
        raise ScheduleError("日数の指定が不正です")

    monkeypatch.setattr(agenda_mod, "build_agenda", boom)
    inter = make_inter()
    run(cog.today.callback(cog, inter))
    c = sent(inter)
    assert c.args[0] == "日数の指定が不正です"
    assert "embed" not in c.kwargs


def test_unexpected_error_generic_message_no_leak(cog, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret-detail sqlite path")

    monkeypatch.setattr(agenda_mod, "build_agenda", boom)
    inter = make_inter()
    run(cog.week.callback(cog, inter))
    c = sent(inter)
    assert c.args[0] == MSG_FAILED
    assert "secret" not in c.args[0]


def test_cog_passes_holidays_to_embed(cog, svc, monkeypatch):
    monkeypatch.setattr(cog, "holidays", lambda: [Holiday(date(2026, 10, 29), "祝日テスト")])
    inter = make_inter()
    run(cog.today.callback(cog, inter))
    f = fields(sent(inter).kwargs["embed"])
    assert f["2026-10-29(木)"] == "祝日: 祝日テスト"


# ===== regression: TodoListView status =========================================================
def todo_cog(svc):
    return TodoCog(MagicMock(), svc, {ALLOWED_ID})


def test_todo_view_default_status_is_open(svc):
    view = TodoListView(todo_cog(svc), [], "h")
    assert view.status == "open"


@pytest.mark.parametrize(
    ("status", "remains"), [("all", True), ("open", False), ("overdue", False)]
)
def test_todo_view_done_button_keeps_or_removes(svc, status, remains):
    t = svc.add_todo("牛乳", due=jst(2026, 10, 28, 9, 0))
    view = TodoListView(todo_cog(svc), svc.list_todos(status), "h", status=status)
    button = view.children[0]
    click = make_inter()

    async def go():
        await button.callback(click)

    run(go())
    ids = [x.id for x in view.todos]
    assert (t.id in ids) is remains
    if remains:
        assert view.todos[0].done
    assert todo_mod  # module imported for parity with other bot tests
