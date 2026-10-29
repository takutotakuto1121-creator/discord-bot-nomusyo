"""Embed builders for schedule items (pure functions; Discord limits enforced here).

Discord limits respected (with margin): title 256, description 4096, field name 256,
field value 1024, footer 2048, 25 fields, 6000 characters in total per embed.
User text is neutralized against mentions (``@everyone``, ``<@id>``, ``<#id>``) in addition to
the ``AllowedMentions.none()`` that callers pass when sending.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

import discord

from nomusyo.schedule.agenda import Agenda, AgendaDay
from nomusyo.schedule.models import Event, Occurrence, ReminderSpec, Todo

MAX_TITLE = 250
MAX_DESCRIPTION = 4000
MAX_FIELD_NAME = 250
MAX_FIELD_VALUE = 1000
MAX_FOOTER = 200
MAX_EMBED_TOTAL = 5500
LIST_PER_PAGE = 10
LIST_LINE_MAX = 300

COLOR_EVENT = 0x3498DB  # events: blue (schedule.md 2.1)
COLOR_REMINDER = 0x9B59B6
COLOR_DANGER = 0xE74C3C
COLOR_NEUTRAL = 0x95A5A6

_ZWSP = "​"
_WEEKDAYS = "月火水木金土日"


def truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def sanitize(text: str) -> str:
    """Neutralize mention syntax by breaking ``@`` and ``<`` with a zero-width space."""
    return text.replace("@", "@" + _ZWSP).replace("<", "<" + _ZWSP)


def escape(text: str) -> str:
    """Escape markdown / masked-link syntax, then neutralize mentions (for body text)."""
    escaped = discord.utils.escape_markdown(text.strip(), ignore_links=False)
    return sanitize(escaped.replace("[", "\\[").replace("]", "\\]"))


def safe(text: str, limit: int) -> str:
    """Escape markdown + mentions first, then truncate to ``limit`` (after escaping)."""
    return truncate(escape(text), limit)


def plain(text: str, limit: int) -> str:
    """Mention-neutralized text for title / footer / field names (no markdown escaping)."""
    return truncate(sanitize(text.strip()), limit)


def format_date(dt: datetime) -> str:
    return f"{dt:%Y-%m-%d}({_WEEKDAYS[dt.weekday()]})"


def format_when(start: datetime, end: datetime | None, *, all_day: bool) -> str:
    """``2026-11-03(火) 15:00〜16:30`` / ``2026-11-03(火) 終日`` (end is exclusive)."""
    if all_day:
        if end is not None and end - start > timedelta(days=1):
            last = end - timedelta(days=1)
            return f"{format_date(start)} 〜 {format_date(last)} 終日"
        return f"{format_date(start)} 終日"
    text = f"{format_date(start)} {start:%H:%M}"
    if end is not None and end > start:
        if end.date() == start.date():
            return f"{text}〜{end:%H:%M}"
        return f"{text} 〜 {format_date(end)} {end:%H:%M}"
    return text


def format_duration(minutes: int) -> str:
    """Inverse of ``parse_duration`` (``1d``, ``2h``, ``1h30m``, ``90m`` -> ``1h30m``)."""
    d, rest = divmod(minutes, 1440)
    h, m = divmod(rest, 60)
    return "".join(f"{n}{u}" for n, u in ((d, "d"), (h, "h"), (m, "m")) if n) or "0m"


def format_reminder(spec: ReminderSpec) -> str:
    if spec.at_local is not None:
        day = "当日" if spec.day_offset == 0 else f"{spec.day_offset}日前"
        return f"{day} {spec.at_local}"
    minutes = spec.offset_min or 0
    if minutes and minutes % 1440 == 0:
        return f"{minutes // 1440}日前"
    if minutes and minutes % 60 == 0:
        return f"{minutes // 60}時間前"
    return f"{minutes}分前"


def _fit_total(embed: discord.Embed) -> discord.Embed:
    """Final guard: drop trailing fields until the total length is within the limit."""
    while len(embed) > MAX_EMBED_TOTAL and embed.fields:
        embed.remove_field(len(embed.fields) - 1)
    if len(embed) > MAX_EMBED_TOTAL and embed.description:
        over = len(embed) - MAX_EMBED_TOTAL
        embed.description = truncate(embed.description, max(0, len(embed.description) - over - 1))
    return embed


def event_embed(
    *,
    title: str,
    start: datetime,
    end: datetime | None = None,
    all_day: bool = False,
    place: str | None = None,
    note: str | None = None,
    reminders: Sequence[ReminderSpec] = (),
    event_id: int | None = None,
    heading: str | None = None,
    kind: str = "event",
    color: int | None = None,
    tz: str | None = None,
) -> discord.Embed:
    """One event = one embed (title, when, place, reminders, note; ID in the footer)."""
    heading_text = f"{heading}: " if heading else ""
    embed = discord.Embed(
        title=plain(f"{heading_text}{title}", MAX_TITLE),
        color=color if color is not None else COLOR_REMINDER if kind == "reminder" else COLOR_EVENT,
    )
    embed.add_field(
        name="日時",
        value=safe(format_when(start, end, all_day=all_day), MAX_FIELD_VALUE),
        inline=False,
    )
    if place:
        embed.add_field(name="場所", value=safe(place, MAX_FIELD_VALUE), inline=False)
    if reminders:
        text = "、".join(format_reminder(r) for r in reminders)
        embed.add_field(name="リマインド", value=safe(text, MAX_FIELD_VALUE), inline=False)
    if note:
        embed.add_field(name="メモ", value=safe(note, MAX_FIELD_VALUE), inline=False)
    footer = []
    if event_id is not None:
        footer.append(f"ID: {event_id}")
    if tz:
        footer.append(tz)
    if footer:
        embed.set_footer(text=plain(" / ".join(footer), MAX_FOOTER))
    return _fit_total(embed)


def embed_from_event(event: Event, *, heading: str | None = None, color: int | None = None):
    return event_embed(
        title=event.title,
        start=event.start,
        end=event.end if (event.duration_min or event.all_day) else None,
        all_day=event.all_day,
        place=event.place,
        note=event.note,
        reminders=[r.spec for r in event.reminders],
        event_id=event.id,
        heading=heading,
        kind=event.kind,
        color=color,
        tz=event.tz,
    )


def _occurrence_line(o: Occurrence) -> str:
    when = f"{o.start:%m/%d}({_WEEKDAYS[o.start.weekday()]}) "
    when += "終日" if o.all_day else f"{o.start:%H:%M}"
    place = f" @{_ZWSP}{escape(o.place)}" if o.place else ""
    return truncate(f"`#{o.event_id}` {when} {escape(o.title)}{place}", LIST_LINE_MAX)


def page_count(total: int, per_page: int = LIST_PER_PAGE) -> int:
    return max(1, -(-total // per_page))


def list_embed(
    occurrences: Sequence[Occurrence],
    *,
    heading: str,
    page: int = 0,
    per_page: int = LIST_PER_PAGE,
) -> discord.Embed:
    """A page of occurrences grouped under date headings; page is clamped to a valid range."""
    pages = page_count(len(occurrences), per_page)
    page = min(max(page, 0), pages - 1)
    chunk = occurrences[page * per_page : (page + 1) * per_page]
    embed = discord.Embed(title=plain(heading, MAX_TITLE), color=COLOR_EVENT)
    if not chunk:
        embed.description = "予定はありません。"
    else:
        lines: list[str] = []
        current: str | None = None
        for o in chunk:
            day = format_date(o.start)
            if day != current:
                lines.append(f"**{day}**")
                current = day
            lines.append(_occurrence_line(o))
        embed.description = truncate("\n".join(lines), MAX_DESCRIPTION)
    embed.set_footer(text=f"{page + 1}/{pages} ページ・全 {len(occurrences)} 件")
    return _fit_total(embed)


# ---- TODO ------------------------------------------------------------------------------------
COLOR_TODO = 0x2ECC71
TODO_PER_PAGE = 5
PRIORITY_LABELS = {0: "なし", 1: "低", 2: "中", 3: "高"}


def todo_due_text(todo: Todo) -> str:
    if todo.due is None:
        return "期限なし"
    return format_when(todo.due, None, all_day=todo.due_all_day)


def _todo_status(todo: Todo, now: datetime) -> str:
    if todo.done:
        return "完了"
    if todo.is_overdue(now):
        return "期限切れ"
    return "未完了"


def todo_embed(todo: Todo, *, now: datetime, heading: str | None = None) -> discord.Embed:
    """One TODO = one embed (due, status, priority, reminders, note; ID in the footer)."""
    heading_text = f"{heading}: " if heading else ""
    status = _todo_status(todo, now)
    color = COLOR_DANGER if status == "期限切れ" else COLOR_TODO if not todo.done else COLOR_NEUTRAL
    embed = discord.Embed(title=plain(f"{heading_text}{todo.title}", MAX_TITLE), color=color)
    embed.add_field(name="期限", value=safe(todo_due_text(todo), MAX_FIELD_VALUE), inline=True)
    embed.add_field(name="状態", value=status, inline=True)
    if todo.priority:
        embed.add_field(name="優先度", value=PRIORITY_LABELS.get(todo.priority, "-"), inline=True)
    if todo.reminders:
        text = "、".join(format_reminder(r.spec) for r in todo.reminders)
        embed.add_field(name="リマインド", value=safe(text, MAX_FIELD_VALUE), inline=False)
    if todo.note:
        embed.add_field(name="メモ", value=safe(todo.note, MAX_FIELD_VALUE), inline=False)
    embed.set_footer(text=plain(f"ID: {todo.id} / {todo.tz}", MAX_FOOTER))
    return _fit_total(embed)


def _todo_line(todo: Todo, now: datetime) -> str:
    mark = "[済] " if todo.done else "【期限切れ】" if todo.is_overdue(now) else ""
    due = f" ({todo_due_text(todo)})" if todo.due else ""
    return truncate(f"`#{todo.id}` {mark}{escape(todo.title)}{due}", LIST_LINE_MAX)


def todo_list_embed(
    todos: Sequence[Todo],
    *,
    heading: str,
    now: datetime,
    page: int = 0,
    per_page: int = TODO_PER_PAGE,
) -> discord.Embed:
    pages = page_count(len(todos), per_page)
    page = min(max(page, 0), pages - 1)
    chunk = todos[page * per_page : (page + 1) * per_page]
    embed = discord.Embed(title=plain(heading, MAX_TITLE), color=COLOR_TODO)
    if chunk:
        embed.description = truncate("\n".join(_todo_line(t, now) for t in chunk), MAX_DESCRIPTION)
    else:
        embed.description = "TODO はありません。"
    embed.set_footer(text=f"{page + 1}/{pages} ページ・全 {len(todos)} 件")
    return _fit_total(embed)


# ---- Agenda (/today /week) -------------------------------------------------------------------
MAX_AGENDA_FIELDS = 10


def _agenda_event_line(o: Occurrence) -> str:
    when = "終日" if o.all_day else f"{o.start:%H:%M}"
    place = f" @{_ZWSP}{escape(o.place)}" if o.place else ""
    return truncate(f"{when} {escape(o.title)}{place} `#{o.event_id}`", LIST_LINE_MAX)


def _fit_lines(lines: Sequence[str], limit: int = MAX_FIELD_VALUE) -> str:
    """Join lines within ``limit``; the omitted count is appended when lines are dropped."""
    if len("\n".join(lines)) <= limit:
        return "\n".join(lines)
    kept: list[str] = []
    size = 0
    for i, line in enumerate(lines):
        note = f"…ほか {len(lines) - i} 件"
        if size + len(line) + 1 + len(note) > limit:
            kept.append(note)
            break
        kept.append(line)
        size += len(line) + 1
    return "\n".join(kept)


def _agenda_day_name(day: AgendaDay) -> str:
    return format_date(datetime.combine(day.date, datetime.min.time()))


def agenda_embed(agenda: Agenda, *, heading: str, now: datetime) -> discord.Embed:
    """Days (holidays, events), overdue TODOs and TODOs due in range as fields."""
    embed = discord.Embed(title=plain(heading, MAX_TITLE), color=COLOR_EVENT)
    single = len(agenda.days) == 1
    if agenda.empty:
        embed.description = "予定・TODO はありません。"
    for day in agenda.days:
        if day.empty and not single:
            continue
        lines = [safe(f"祝日: {h.name}", LIST_LINE_MAX) for h in day.holidays]
        lines += [_agenda_event_line(o) for o in day.occurrences]
        value = _fit_lines(lines) if lines else "予定はありません。"
        if agenda.empty and single:
            break
        embed.add_field(
            name=plain(_agenda_day_name(day), MAX_FIELD_NAME), value=value, inline=False
        )
    if agenda.overdue:
        lines = [_todo_line(t, now) for t in agenda.overdue]
        embed.add_field(name="期限切れの TODO", value=_fit_lines(lines), inline=False)
    if agenda.due_in_range:
        lines = [_todo_line(t, now) for t in agenda.due_in_range]
        embed.add_field(name="期限が近い TODO", value=_fit_lines(lines), inline=False)
    embed.set_footer(text=plain(agenda.tz, MAX_FOOTER))
    return _fit_total(embed)
