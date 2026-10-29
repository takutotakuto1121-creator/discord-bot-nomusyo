from datetime import datetime
from zoneinfo import ZoneInfo

import discord
import pytest

from nomusyo.bot import embeds
from nomusyo.bot.embeds import (
    LIST_PER_PAGE,
    MAX_DESCRIPTION,
    MAX_EMBED_TOTAL,
    MAX_FIELD_VALUE,
    MAX_FOOTER,
    MAX_TITLE,
    escape,
    event_embed,
    format_duration,
    format_reminder,
    format_when,
    list_embed,
    page_count,
    plain,
    safe,
    sanitize,
    truncate,
)
from nomusyo.schedule.models import Event, Occurrence, ReminderRule, ReminderSpec

JST = ZoneInfo("Asia/Tokyo")
ZWSP = "​"


def jst(*a):
    return datetime(*a, tzinfo=JST)


def occ(i, start, *, title="予定", all_day=False, place=None):
    return Occurrence(
        event_id=i,
        occurrence_local=f"{start:%Y-%m-%dT%H:%M}",
        start=start,
        end=start,
        title=title,
        all_day=all_day,
        place=place,
    )


def field_map(embed):
    return {f.name: f.value for f in embed.fields}


# ---- truncate / sanitize ---------------------------------------------------------------------
def test_truncate_keeps_short_text_and_strips():
    assert truncate("  abc  ", 10) == "abc"


def test_truncate_exact_limit_untouched():
    assert truncate("a" * 10, 10) == "a" * 10


def test_truncate_over_limit_ends_with_ellipsis_within_limit():
    out = truncate("a" * 11, 10)
    assert len(out) == 10 and out.endswith("…")


@pytest.mark.parametrize(
    "text",
    ["@everyone", "@here", "<@123456789>", "<@!123456789>", "<@&987654321>", "<#555555>"],
)
def test_sanitize_breaks_mention_syntax(text):
    out = sanitize(text)
    assert "@everyone" not in out and "@here" not in out
    assert "<@" not in out and "<#" not in out
    assert out.replace(ZWSP, "") == text  # visible text is unchanged


# ---- event_embed limits ----------------------------------------------------------------------
def test_event_embed_title_truncated_to_250():
    e = event_embed(title="あ" * 400, start=jst(2026, 11, 3, 15))
    assert len(e.title) <= MAX_TITLE


def test_event_embed_title_includes_heading():
    e = event_embed(title="会議", start=jst(2026, 11, 3, 15), heading="プレビュー")
    assert e.title == "プレビュー: 会議"


def test_event_embed_field_value_truncated_to_1000():
    e = event_embed(title="t", start=jst(2026, 11, 3, 15), place="場" * 3000, note="メ" * 3000)
    values = field_map(e)
    assert len(values["場所"]) <= MAX_FIELD_VALUE
    assert len(values["メモ"]) <= MAX_FIELD_VALUE


def test_event_embed_footer_truncated_to_200():
    e = event_embed(title="t", start=jst(2026, 11, 3, 15), event_id=1, tz="Z" * 500)
    assert len(e.footer.text) <= MAX_FOOTER


def test_event_embed_footer_has_id_and_tz():
    e = event_embed(title="t", start=jst(2026, 11, 3, 15), event_id=7, tz="Asia/Tokyo")
    assert e.footer.text == "ID: 7 / Asia/Tokyo"


def test_event_embed_without_optional_parts_has_only_when_field():
    e = event_embed(title="t", start=jst(2026, 11, 3, 15))
    assert list(field_map(e)) == ["日時"]
    assert e.footer.text is None


def test_event_embed_within_total_limit_with_max_inputs():
    e = event_embed(
        title="t" * 1000,
        start=jst(2026, 11, 3, 15),
        place="p" * 5000,
        note="n" * 5000,
        reminders=[ReminderSpec(offset_min=m) for m in range(1, 11)],
        event_id=1,
        tz="Asia/Tokyo",
    )
    assert len(e) <= MAX_EMBED_TOTAL


def test_fit_total_drops_trailing_fields_when_over_5500():
    e = discord.Embed(title="t")
    for i in range(8):
        e.add_field(name=f"f{i}", value="x" * 1000, inline=False)
    assert len(e) > MAX_EMBED_TOTAL
    out = embeds._fit_total(e)
    assert len(out) <= MAX_EMBED_TOTAL
    names = [f.name for f in out.fields]
    assert names == [f"f{i}" for i in range(len(names))]  # leading fields kept, tail dropped
    assert len(names) < 8


def test_fit_total_noop_when_within_limit():
    e = discord.Embed(title="t")
    e.add_field(name="a", value="b")
    assert len(embeds._fit_total(e).fields) == 1


def test_fit_total_truncates_description_when_no_fields_left():
    e = discord.Embed(title="t", description="d" * 6000)
    out = embeds._fit_total(e)
    assert len(out) <= MAX_EMBED_TOTAL


# ---- mentions neutralized --------------------------------------------------------------------
def test_event_embed_neutralizes_mentions_everywhere():
    bad = "@everyone <@123> <#456> @here"
    e = event_embed(
        title=bad, start=jst(2026, 11, 3, 15), place=bad, note=bad, heading=bad, tz=bad,
        event_id=1,
    )  # fmt: skip
    texts = [e.title, e.footer.text, *[f.value for f in e.fields]]
    for t in texts:
        assert "@everyone" not in t and "@here" not in t
        assert "<@" not in t and "<#" not in t


def test_list_embed_neutralizes_mentions():
    o = occ(1, jst(2026, 11, 3, 15), title="@everyone <@1> <#2>", place="<@&3>")
    e = list_embed([o], heading="@everyone")
    assert "@everyone" not in e.description and "<@" not in e.description
    assert "<#" not in e.description
    assert "@everyone" not in e.title


def test_mention_heavy_text_still_within_discord_limits():
    e = event_embed(title="@" * 400, start=jst(2026, 11, 3, 15), note="@" * 3000)
    assert len(e.title) <= 256
    assert len(field_map(e)["メモ"]) <= 1024


# ---- format_when -----------------------------------------------------------------------------
def test_format_when_timed_same_day():
    assert (
        format_when(jst(2026, 11, 3, 15), jst(2026, 11, 3, 16, 30), all_day=False)
        == "2026-11-03(火) 15:00〜16:30"
    )


def test_format_when_timed_no_end_shows_start_only():
    assert format_when(jst(2026, 11, 3, 15), None, all_day=False) == "2026-11-03(火) 15:00"
    s = jst(2026, 11, 3, 15)
    assert format_when(s, s, all_day=False) == "2026-11-03(火) 15:00"


def test_format_when_crossing_midnight():
    assert (
        format_when(jst(2026, 11, 3, 23), jst(2026, 11, 4, 1), all_day=False)
        == "2026-11-03(火) 23:00 〜 2026-11-04(水) 01:00"
    )


def test_format_when_all_day_single():
    assert format_when(jst(2026, 11, 3), jst(2026, 11, 4), all_day=True) == "2026-11-03(火) 終日"
    assert format_when(jst(2026, 11, 3), None, all_day=True) == "2026-11-03(火) 終日"


def test_format_when_all_day_multi_day_uses_inclusive_last_day():
    assert (
        format_when(jst(2026, 11, 3), jst(2026, 11, 6), all_day=True)
        == "2026-11-03(火) 〜 2026-11-05(木) 終日"
    )


def test_format_when_year_boundary_weekday():
    assert format_when(jst(2026, 12, 31), jst(2027, 1, 2), all_day=True) == (
        "2026-12-31(木) 〜 2027-01-01(金) 終日"
    )


# ---- duration / reminder display ------------------------------------------------------------
@pytest.mark.parametrize(
    ("minutes", "text"),
    [(90, "1h30m"), (60, "1h"), (5, "5m"), (1440, "1d"), (1500, "1d1h"), (0, "0m")],
)
def test_format_duration(minutes, text):
    assert format_duration(minutes) == text


@pytest.mark.parametrize(
    ("spec", "text"),
    [
        (ReminderSpec(offset_min=10), "10分前"),
        (ReminderSpec(offset_min=60), "1時間前"),
        (ReminderSpec(offset_min=120), "2時間前"),
        (ReminderSpec(offset_min=1440), "1日前"),
        (ReminderSpec(offset_min=90), "90分前"),
        (ReminderSpec(day_offset=0, at_local="09:00"), "当日 09:00"),
        (ReminderSpec(day_offset=1, at_local="20:00"), "1日前 20:00"),
    ],
)
def test_format_reminder(spec, text):
    assert format_reminder(spec) == text


def test_event_embed_shows_reminders_joined():
    e = event_embed(
        title="t",
        start=jst(2026, 11, 3, 15),
        reminders=[ReminderSpec(offset_min=10), ReminderSpec(day_offset=1, at_local="20:00")],
    )
    assert field_map(e)["リマインド"] == "10分前、1日前 20:00"


def test_event_embed_kind_reminder_has_reminder_color():
    e = event_embed(title="t", start=jst(2026, 11, 3, 15), kind="reminder")
    assert e.color.value == embeds.COLOR_REMINDER
    plain = event_embed(title="t", start=jst(2026, 11, 3, 15))
    assert plain.color.value == embeds.COLOR_EVENT
    custom = event_embed(title="t", start=jst(2026, 11, 3, 15), color=embeds.COLOR_DANGER)
    assert custom.color.value == embeds.COLOR_DANGER


def test_embed_from_event_uses_rules_and_hides_end_for_instant_event():
    ev = Event(
        id=5, owner_id=1, title="会議", start=jst(2026, 11, 3, 15), tz="Asia/Tokyo",
        reminders=(ReminderRule(id=1, spec=ReminderSpec(offset_min=10), event_id=5),),
    )  # fmt: skip
    e = embeds.embed_from_event(ev, heading="削除確認")
    assert e.title == "削除確認: 会議"
    assert field_map(e)["日時"] == "2026-11-03(火) 15:00"
    assert field_map(e)["リマインド"] == "10分前"
    assert e.footer.text == "ID: 5 / Asia/Tokyo"


def test_embed_from_event_all_day_and_duration():
    all_day = Event(id=1, owner_id=1, title="x", start=jst(2026, 11, 3), tz="Asia/Tokyo",
                    all_day=True)  # fmt: skip
    assert field_map(embeds.embed_from_event(all_day))["日時"] == "2026-11-03(火) 終日"
    timed = Event(id=2, owner_id=1, title="x", start=jst(2026, 11, 3, 15), tz="Asia/Tokyo",
                  duration_min=90)  # fmt: skip
    assert field_map(embeds.embed_from_event(timed))["日時"] == "2026-11-03(火) 15:00〜16:30"


# ---- list_embed ------------------------------------------------------------------------------
def many(n):
    return [occ(i, jst(2026, 11, 3 + i // 3, 9 + i % 3)) for i in range(1, n + 1)]


@pytest.mark.parametrize(
    ("total", "pages"),
    [(0, 1), (1, 1), (10, 1), (11, 2), (20, 2), (21, 3)],
)
def test_page_count_boundaries(total, pages):
    assert page_count(total) == pages


def test_list_embed_empty():
    e = list_embed([], heading="予定一覧")
    assert e.description == "予定はありません。"
    assert e.footer.text == "1/1 ページ・全 0 件"


def test_list_embed_exactly_one_page_has_all_items():
    items = many(LIST_PER_PAGE)
    e = list_embed(items, heading="h")
    assert e.footer.text.startswith("1/1")
    assert all(f"`#{o.event_id}`" in e.description for o in items)


def test_list_embed_page_boundaries_split_items():
    items = many(LIST_PER_PAGE + 1)
    first = list_embed(items, heading="h", page=0)
    second = list_embed(items, heading="h", page=1)
    assert "`#10`" in first.description and "`#11`" not in first.description
    assert "`#11`" in second.description and "`#10`" not in second.description
    assert second.footer.text == "2/2 ページ・全 11 件"


def test_list_embed_page_is_clamped():
    items = many(11)
    assert list_embed(items, heading="h", page=99).footer.text.startswith("2/2")
    assert list_embed(items, heading="h", page=-5).footer.text.startswith("1/2")


def test_list_embed_groups_by_date_heading_and_marks_all_day():
    items = [
        occ(1, jst(2026, 11, 3), title="祝", all_day=True),
        occ(2, jst(2026, 11, 3, 15), title="会議", place="会議室"),
        occ(3, jst(2026, 11, 4, 9), title="朝"),
    ]
    lines = list_embed(items, heading="h").description.split("\n")
    assert lines[0] == "**2026-11-03(火)**"
    assert "終日" in lines[1] and "祝" in lines[1]
    assert "15:00" in lines[2] and "@会議室" in lines[2].replace(ZWSP, "")
    assert lines[3] == "**2026-11-04(水)**"
    assert sum(1 for line in lines if line.startswith("**")) == 2


def test_list_embed_long_titles_stay_within_description_and_total_limits():
    items = [occ(i, jst(2026, 11, 3, 9), title="あ" * 5000) for i in range(1, 11)]
    e = list_embed(items, heading="x" * 1000)
    assert len(e.description) <= MAX_DESCRIPTION
    assert len(e.title) <= MAX_TITLE
    assert len(e) <= MAX_EMBED_TOTAL


def test_safe_combines_truncate_and_sanitize():
    assert safe("  @everyone  ", 100) == "@" + ZWSP + "everyone"


# ---- TODO embeds -----------------------------------------------------------------------------
from nomusyo.bot.embeds import (  # noqa: E402
    TODO_PER_PAGE,
    todo_due_text,
    todo_embed,
    todo_list_embed,
)
from nomusyo.schedule.models import Todo  # noqa: E402

TODO_NOW = jst(2026, 10, 29, 12)


def mk_todo(i=1, title="牛乳", **kw):
    kw.setdefault("owner_id", 1)
    kw.setdefault("tz", "Asia/Tokyo")
    return Todo(id=i, title=title, **kw)


def test_todo_due_text_none_timed_and_all_day():
    assert todo_due_text(mk_todo()) == "期限なし"
    assert todo_due_text(mk_todo(due=jst(2026, 11, 3, 18))) == "2026-11-03(火) 18:00"
    assert todo_due_text(mk_todo(due=jst(2026, 11, 3), due_all_day=True)) == "2026-11-03(火) 終日"


def test_todo_embed_open_no_due():
    e = todo_embed(mk_todo(7), now=TODO_NOW)
    f = field_map(e)
    assert f["期限"] == "期限なし" and f["状態"] == "未完了"
    assert "優先度" not in f and "メモ" not in f and "リマインド" not in f
    assert e.footer.text == "ID: 7 / Asia/Tokyo"
    assert e.color.value == embeds.COLOR_TODO


def test_todo_embed_overdue_and_done():
    over = todo_embed(mk_todo(due=jst(2026, 10, 29, 9)), now=TODO_NOW)
    assert field_map(over)["状態"] == "期限切れ"
    assert over.color.value == embeds.COLOR_DANGER
    done = todo_embed(
        mk_todo(due=jst(2026, 10, 29, 9), done_at=jst(2026, 10, 29, 10)), now=TODO_NOW
    )
    assert field_map(done)["状態"] == "完了"
    assert done.color.value == embeds.COLOR_NEUTRAL


def test_todo_embed_all_day_due_today_is_not_overdue():
    t = mk_todo(due=jst(2026, 10, 29), due_all_day=True)
    assert field_map(todo_embed(t, now=TODO_NOW))["状態"] == "未完了"
    assert field_map(todo_embed(t, now=jst(2026, 10, 30, 0, 0)))["状態"] == "期限切れ"


def test_todo_embed_priority_note_heading():
    e = todo_embed(mk_todo(priority=3, note="メモ"), now=TODO_NOW, heading="削除確認")
    f = field_map(e)
    assert f["優先度"] == "高" and f["メモ"] == "メモ"
    assert e.title == "削除確認: 牛乳"


def test_todo_embed_mention_heavy_within_limits():
    t = mk_todo(title="@" * 300, note="<@1>" * 1000)
    e = todo_embed(t, now=TODO_NOW, heading="@everyone")
    assert len(e.title) <= 256
    assert len(field_map(e)["メモ"]) <= 1024
    assert len(e) <= MAX_EMBED_TOTAL
    assert "@everyone" not in e.title and "<@1>" not in field_map(e)["メモ"]


def test_todo_list_embed_empty():
    e = todo_list_embed([], heading="TODO 一覧", now=TODO_NOW)
    assert e.description == "TODO はありません。"
    assert e.footer.text == "1/1 ページ・全 0 件"


def test_todo_list_embed_lines_marks():
    todos = [
        mk_todo(1, "通常"),
        mk_todo(2, "遅れ", due=jst(2026, 10, 28, 9)),
        mk_todo(3, "済", done_at=jst(2026, 10, 29, 10)),
        mk_todo(4, "期限", due=jst(2026, 11, 3, 18)),
    ]
    lines = todo_list_embed(todos, heading="h", now=TODO_NOW).description.split("\n")
    assert lines[0] == "`#1` 通常"
    assert lines[1].startswith("`#2` 【期限切れ】遅れ (2026-10-28(水) 09:00)")
    assert lines[2] == "`#3` [済] 済"
    assert lines[3] == "`#4` 期限 (2026-11-03(火) 18:00)"


def test_todo_list_embed_page_boundaries():
    n = TODO_PER_PAGE * 2
    todos = [mk_todo(i, f"t{i}") for i in range(1, n + 1)]
    p0 = todo_list_embed(todos, heading="h", now=TODO_NOW, page=0)
    p1 = todo_list_embed(todos, heading="h", now=TODO_NOW, page=1)
    assert len(p0.description.split("\n")) == TODO_PER_PAGE
    assert p0.description.split("\n")[0] == "`#1` t1"
    assert p1.description.split("\n")[0] == f"`#{TODO_PER_PAGE + 1}` t{TODO_PER_PAGE + 1}"
    assert p1.footer.text == f"2/2 ページ・全 {n} 件"
    # one more item creates a third page with a single line
    more = todos + [mk_todo(n + 1, "last")]
    p2 = todo_list_embed(more, heading="h", now=TODO_NOW, page=2)
    assert p2.description == f"`#{n + 1}` last"
    assert p2.footer.text.startswith("3/3")


def test_todo_list_embed_page_is_clamped():
    todos = [mk_todo(1)]
    assert todo_list_embed(todos, heading="h", now=TODO_NOW, page=9).footer.text.startswith("1/1")
    assert todo_list_embed(todos, heading="h", now=TODO_NOW, page=-3).footer.text.startswith("1/1")


def test_todo_list_embed_mention_heavy_within_limits():
    todos = [mk_todo(i, "@everyone <@1>" * 100) for i in range(1, TODO_PER_PAGE + 1)]
    e = todo_list_embed(todos, heading="@" * 400, now=TODO_NOW)
    assert len(e.description) <= MAX_DESCRIPTION
    assert len(e.title) <= 256
    assert len(e) <= MAX_EMBED_TOTAL
    assert "@everyone" not in e.description and "<@" not in e.description


# ---- escape / plain / backtick id ------------------------------------------------------------
@pytest.mark.parametrize("text", ["*bold*", "_it_", "~~s~~", "`code`", "||spoiler||", "# h", "> q"])
def test_escape_neutralizes_markdown(text):
    out = escape(text)
    assert out != text
    assert "\\" in out


def test_escape_neutralizes_masked_link():
    out = escape("[click](https://evil.example)")
    assert "\\[" in out and "\\]" in out
    assert "[click](" not in out


def test_escape_neutralizes_mentions():
    out = escape("@everyone <@123>")
    assert "@everyone" not in out and "<@123>" not in out


def test_plain_only_neutralizes_mentions_not_markdown():
    out = plain("**t** @everyone <@1> [a](b)", 100)
    assert "**t**" in out and "[a](b)" in out
    assert "@everyone" not in out and "<@1>" not in out


def test_plain_truncates():
    assert len(plain("x" * 500, 50)) <= 50


def test_list_line_keeps_backtick_id_but_escapes_title():
    e = list_embed([occ(7, jst(2026, 11, 3, 15), title="*x* [a](b)")], heading="h")
    assert "`#7`" in e.description
    assert "*x*" not in e.description.replace("\\*", "")
    assert "[a](b)" not in e.description


def test_list_heading_uses_plain_not_escaped():
    e = list_embed([], heading="**今週** @everyone")
    assert "**今週**" in e.title
    assert "@everyone" not in e.title
