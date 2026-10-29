"""Discord-independent aggregation of events and TODOs for a date range (``/today`` ``/week``).

``build_agenda`` returns an ``Agenda`` (plain data, no Embed) so the morning digest can reuse it.
Ordering is deterministic: per day, all-day first, then by start instant, then event id, then
occurrence key. Holidays are accepted as an argument (empty until V2-62).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from nomusyo.schedule.errors import ValidationError
from nomusyo.schedule.models import Occurrence, Todo, Window, get_zone, local_day_bounds
from nomusyo.schedule.service import ScheduleService


@dataclass(frozen=True)
class Holiday:
    date: date
    name: str


@dataclass(frozen=True)
class AgendaDay:
    date: date
    holidays: tuple[Holiday, ...]
    occurrences: tuple[Occurrence, ...]  # sorted, see ``sort_day``

    @property
    def empty(self) -> bool:
        return not self.holidays and not self.occurrences


@dataclass(frozen=True)
class Agenda:
    start_date: date
    days: tuple[AgendaDay, ...]  # one entry per calendar day, including empty ones
    overdue: tuple[Todo, ...]  # open and overdue at ``now``
    due_in_range: tuple[Todo, ...]  # open, not overdue, due inside the range
    tz: str

    @property
    def end_date(self) -> date:
        return self.days[-1].date

    @property
    def empty(self) -> bool:
        return not self.overdue and not self.due_in_range and all(d.empty for d in self.days)


def _day_key(o: Occurrence, day_start: datetime) -> tuple:
    start = max(o.start.astimezone(UTC), day_start.astimezone(UTC))
    return (not o.all_day, start, o.event_id, o.occurrence_local)


def sort_day(occurrences: Sequence[Occurrence], day_start: datetime) -> list[Occurrence]:
    """All-day first, then start time (events continuing from the previous day count as
    starting at midnight), then event id, then occurrence key."""
    return sorted(occurrences, key=lambda o: _day_key(o, day_start))


def _overlaps(o: Occurrence, lo: datetime, hi: datetime) -> bool:
    if o.end > o.start:
        return o.start < hi and o.end > lo
    return lo <= o.start < hi  # instant


def _todo_key(t: Todo) -> tuple:
    due = t.due.astimezone(UTC) if t.due else datetime.max.replace(tzinfo=UTC)
    return (due, -t.priority, t.id)


def assemble_agenda(
    occurrences: Sequence[Occurrence],
    todos: Sequence[Todo],
    *,
    start_date: date,
    days: int,
    zone: ZoneInfo,
    now: datetime,
    holidays: Sequence[Holiday] = (),
) -> Agenda:
    """Pure assembly. Multi-day events appear on every day they overlap. Done TODOs are ignored;
    a TODO that is overdue is listed only under ``overdue``."""
    if days < 1:
        raise ValidationError("日数の指定が不正です")
    out_days: list[AgendaDay] = []
    for i in range(days):
        d = start_date + timedelta(days=i)
        lo, hi = local_day_bounds(d, zone)
        todays = [o for o in occurrences if _overlaps(o, lo, hi)]
        hol = tuple(sorted((h for h in holidays if h.date == d), key=lambda h: h.name))
        out_days.append(AgendaDay(d, hol, tuple(sort_day(todays, lo))))
    range_lo = local_day_bounds(start_date, zone)[0]
    range_hi = local_day_bounds(start_date + timedelta(days=days - 1), zone)[1]
    open_todos = [t for t in todos if not t.done]
    overdue = sorted((t for t in open_todos if t.is_overdue(now)), key=_todo_key)
    upcoming = sorted(
        (
            t
            for t in open_todos
            if not t.is_overdue(now) and t.due is not None and range_lo <= t.due < range_hi
        ),
        key=_todo_key,
    )
    return Agenda(start_date, tuple(out_days), tuple(overdue), tuple(upcoming), str(zone))


def build_agenda(
    service: ScheduleService,
    *,
    days: int = 1,
    start_date: date | None = None,
    holidays: Sequence[Holiday] = (),
) -> Agenda:
    """Agenda for ``days`` days from ``start_date`` (default: today in the service tz)."""
    zone = get_zone(service.default_tz)
    now = service.clock.now()
    start = start_date or now.astimezone(zone).date()
    lo = local_day_bounds(start, zone)[0]
    hi = local_day_bounds(start + timedelta(days=days - 1), zone)[1]
    occurrences = service.expand(Window(lo, hi))
    todos = service.list_todos("open")
    return assemble_agenda(
        occurrences, todos, start_date=start, days=days, zone=zone, now=now, holidays=holidays
    )
