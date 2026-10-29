"""Domain models and time formatting helpers.

Rules (schedule-data-model.md section 1):
* All datetimes held in models are **aware**; naive values raise ``ValidationError``.
* DB UTC format: ``YYYY-MM-DDTHH:MM:SSZ`` (``*_utc``); wall clock: ``YYYY-MM-DDTHH:MM:SS``
  (``*_local``, interpreted in the row's ``tz``).
* An all-day item is stored as local midnight with ``all_day=True``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from nomusyo.schedule.errors import ValidationError

UTC_FMT = "%Y-%m-%dT%H:%M:%SZ"
LOCAL_FMT = "%Y-%m-%dT%H:%M:%S"
DEFAULT_TZ = "Asia/Tokyo"
ALL_DAY_MINUTES = 24 * 60
MAX_REMINDERS = 10  # rules per event/TODO (single definition; parsing/service import it)
MAX_DAY_OFFSET = 31  # upper bound of ``Nd@HH:MM``


def get_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ValidationError("タイムゾーンが不正です") from None


def require_aware(value: datetime, what: str = "日時") -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{what}はタイムゾーン付きである必要があります")
    return value


def to_utc_str(dt: datetime) -> str:
    return require_aware(dt).astimezone(UTC).strftime(UTC_FMT)


def from_utc_str(s: str) -> datetime:
    return datetime.strptime(s, UTC_FMT).replace(tzinfo=UTC)


def to_local_str(dt: datetime) -> str:
    """Wall-clock string in the datetime's own zone (the caller stores that zone name in ``tz``)."""
    return require_aware(dt).strftime(LOCAL_FMT)


def from_local_str(s: str, tz: str) -> datetime:
    return datetime.strptime(s, LOCAL_FMT).replace(tzinfo=get_zone(tz))


def local_midnight(d: date, zone: ZoneInfo) -> datetime:
    return datetime.combine(d, time(0, 0), tzinfo=zone)


def local_day_bounds(d: date, zone: ZoneInfo) -> tuple[datetime, datetime]:
    """[start, end) of local calendar day ``d`` (DST-safe: next midnight, not +24h)."""
    return local_midnight(d, zone), local_midnight(d + timedelta(days=1), zone)


@dataclass(frozen=True)
class Window:
    """Half-open time window ``[start, end)``. Both ends aware."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        require_aware(self.start, "窓の開始")
        require_aware(self.end, "窓の終了")
        if self.end <= self.start:
            raise ValidationError("窓の終了は開始より後である必要があります")

    @classmethod
    def for_day(cls, d: date, zone: ZoneInfo) -> Window:
        return cls(*local_day_bounds(d, zone))


@dataclass(frozen=True)
class ReminderSpec:
    """A reminder rule as entered. Either ``offset_min`` (N minutes before) or
    ``at_local`` ('HH:MM') with ``day_offset`` (N days before, at that time)."""

    offset_min: int | None = None
    at_local: str | None = None
    day_offset: int = 0

    def __post_init__(self) -> None:
        if (self.offset_min is None) == (self.at_local is None):
            raise ValidationError("リマインドは「N分前」か「N日前@時刻」のどちらかです")
        if self.offset_min is not None and (self.offset_min < 0 or self.day_offset != 0):
            raise ValidationError("リマインドの指定が不正です")
        if not 0 <= self.day_offset <= MAX_DAY_OFFSET:
            raise ValidationError(f"リマインドの日数は{MAX_DAY_OFFSET}日前までです")


def validate_todo_reminders(
    due: datetime | None, due_all_day: bool, specs: Iterable[ReminderSpec]
) -> None:
    """Single check of the final (due, due_all_day, rules) state of a TODO (design 3.2):
    rules need a due, and a date-only due allows only ``at_local`` rules (no ``offset_min``)."""
    specs = list(specs)
    if due is None:
        if specs:
            raise ValidationError("期限が必要です")
        return
    if due_all_day and any(s.offset_min is not None for s in specs):
        raise ValidationError("日付のみの期限には「N分前」のリマインドは使えません(例 1d@20:00)")


@dataclass(frozen=True)
class ReminderRule:
    id: int
    spec: ReminderSpec
    event_id: int | None = None
    todo_id: int | None = None


@dataclass(frozen=True)
class Event:
    id: int
    owner_id: int
    title: str
    start: datetime  # aware, in ``tz``; first occurrence (DTSTART)
    tz: str
    all_day: bool = False
    duration_min: int | None = None
    note: str | None = None
    place: str | None = None
    kind: str = "event"
    rrule: str | None = None  # RFC5545 RRULE body for recurring events (None = single event)
    skip_holidays: bool = False
    series_id: int | None = None
    source: str = "manual"
    created_at: datetime | None = None
    updated_at: datetime | None = None
    deleted_at: datetime | None = None
    reminders: tuple[ReminderRule, ...] = field(default=(), compare=False)

    def __post_init__(self) -> None:
        require_aware(self.start, "開始日時")

    @property
    def end(self) -> datetime:
        """Exclusive end. All-day: next local midnight (or +duration days). Instant events
        (no duration) end where they start."""
        if self.all_day:
            minutes = self.duration_min or ALL_DAY_MINUTES
            days = max(1, -(-minutes // ALL_DAY_MINUTES))
            return local_midnight(self.start.date() + timedelta(days=days), self.start.tzinfo)  # type: ignore[arg-type]
        if self.duration_min:
            return (self.start.astimezone(UTC) + timedelta(minutes=self.duration_min)).astimezone(
                self.start.tzinfo
            )
        return self.start

    @property
    def deleted(self) -> bool:
        return self.deleted_at is not None


@dataclass(frozen=True)
class Occurrence:
    """One concrete instance of an event inside a window (virtual; no table).
    Key = ``(event_id, occurrence_local)`` where ``occurrence_local`` is the original wall-clock
    start from the rule (equal to the event's own start for non-recurring events)."""

    event_id: int
    occurrence_local: str
    start: datetime
    end: datetime
    title: str
    all_day: bool
    place: str | None = None
    note: str | None = None
    kind: str = "event"

    def __post_init__(self) -> None:
        require_aware(self.start)
        require_aware(self.end)


@dataclass(frozen=True)
class Todo:
    id: int
    owner_id: int
    title: str
    tz: str
    list_name: str = "todo"
    note: str | None = None
    priority: int = 0
    due: datetime | None = None  # aware; date-only due = local midnight of that day
    due_all_day: bool = False
    done_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    deleted_at: datetime | None = None
    reminders: tuple[ReminderRule, ...] = field(default=(), compare=False)

    def __post_init__(self) -> None:
        if self.due is not None:
            require_aware(self.due, "期限")

    @property
    def done(self) -> bool:
        return self.done_at is not None

    @property
    def overdue_boundary(self) -> datetime | None:
        """Moment after which an open TODO counts as overdue (date-only: next local midnight)."""
        if self.due is None:
            return None
        if self.due_all_day:
            return local_midnight(self.due.date() + timedelta(days=1), self.due.tzinfo)  # type: ignore[arg-type]
        return self.due

    def is_overdue(self, now: datetime) -> bool:
        boundary = self.overdue_boundary
        return not self.done and boundary is not None and now >= boundary
