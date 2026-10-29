"""Discord independent TODO input handling (parsing and validation of raw user text).

Persistence and rules live in :class:`nomusyo.schedule.service.ScheduleService`; this module
turns raw strings into validated arguments for ``add_todo``. ``ScheduleError`` messages are safe
to show to users (they never echo the input).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from nomusyo.schedule import service as svc
from nomusyo.schedule.errors import ValidationError
from nomusyo.schedule.models import ReminderSpec, get_zone, validate_todo_reminders
from nomusyo.schedule.parsing import parse_reminders, parse_when

STATUSES = ("open", "overdue", "done", "no_due", "all")
STATUS_LABELS = {
    "open": "未完了",
    "overdue": "期限切れ",
    "done": "完了",
    "no_due": "期限なし",
    "all": "すべて",
}


@dataclass(frozen=True)
class TodoInput:
    title: str
    due: datetime | None
    due_all_day: bool
    note: str | None
    priority: int
    reminders: tuple[ReminderSpec, ...]
    tz: str


def _clean(value: str | None) -> str | None:
    v = (value or "").strip()
    return v or None


def build_todo_input(
    *,
    title: str,
    now: datetime,
    tz: str,
    due: str | None = None,
    note: str | None = None,
    priority: int = 0,
    remind: str | None = None,
) -> TodoInput:
    """Validate raw ``/todo add`` input. Raises ``ScheduleError``."""
    title_v = _clean(title)
    if title_v is None:
        raise ValidationError("タイトルを入力してください")
    if len(title_v) > svc.MAX_TITLE:
        raise ValidationError(f"タイトルは{svc.MAX_TITLE}文字以内にしてください")
    note_v = _clean(note)
    if note_v is not None and len(note_v) > svc.MAX_NOTE:
        raise ValidationError(f"メモは{svc.MAX_NOTE}文字以内にしてください")
    if not 0 <= priority <= 3:
        raise ValidationError("優先度は0〜3です")
    specs = parse_reminders(remind or "")
    due_v = _clean(due)
    if due_v is None:
        validate_todo_reminders(None, False, specs)
        return TodoInput(title_v, None, False, note_v, priority, (), tz)
    parsed = parse_when(due_v, now=now, tz=get_zone(tz))
    validate_todo_reminders(parsed.start, parsed.all_day, specs)
    return TodoInput(title_v, parsed.start, parsed.all_day, note_v, priority, specs, tz)
