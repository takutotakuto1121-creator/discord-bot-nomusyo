"""Schedule service (Discord independent): events, TODOs and reminder rules on SQLite.

Phase 2 scope: non-recurring events only. Listing goes through :meth:`ScheduleService.expand`
so that phase 3 can plug RRULE expansion in without changing callers. ``remind`` rules are only
stored (notifications are generated and sent in phase 3). All SQL is parameterized.
Derived UTC columns are computed in :func:`_utc_of` / :meth:`_event_columns` (single place).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from nomusyo.schedule import planner, recurrence
from nomusyo.schedule.clock import Clock, SystemClock
from nomusyo.schedule.errors import NotFoundError, ValidationError
from nomusyo.schedule.models import (
    DEFAULT_TZ,
    MAX_REMINDERS,
    Event,
    Occurrence,
    ReminderRule,
    ReminderSpec,
    Todo,
    Window,
    from_local_str,
    from_utc_str,
    get_zone,
    require_aware,
    to_local_str,
    to_utc_str,
    validate_todo_reminders,
)
from nomusyo.storage.db import Database

MAX_TITLE = 200
MAX_NOTE = 2000
MAX_PLACE = 200


__all__ = ["MAX_REMINDERS", "UNSET", "ScheduleService"]  # MAX_REMINDERS: re-exported from models


class _Unset:
    def __repr__(self) -> str:
        return "UNSET"


UNSET: Any = _Unset()  # "leave unchanged" marker for update_* (None means "clear")


def _check_text(value: str | None, what: str, maximum: int, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise ValidationError(f"{what}を入力してください")
        return None
    v = value.strip()
    if not v:
        if required:
            raise ValidationError(f"{what}を入力してください")
        return None
    if len(v) > maximum:
        raise ValidationError(f"{what}は{maximum}文字以内にしてください")
    return v


def _check_specs(specs: Sequence[ReminderSpec]) -> list[ReminderSpec]:
    uniq: list[ReminderSpec] = []
    for s in specs:
        if not isinstance(s, ReminderSpec):
            raise ValidationError("リマインドの指定が不正です")
        if s not in uniq:
            uniq.append(s)
    if len(uniq) > MAX_REMINDERS:
        raise ValidationError(f"リマインドは{MAX_REMINDERS}件までです")
    return uniq


def _in_tz(dt: datetime, tz: str) -> datetime:
    return require_aware(dt).astimezone(get_zone(tz))


class ScheduleService:
    def __init__(
        self,
        db: Database,
        owner_id: int,
        *,
        clock: Clock | None = None,
        default_tz: str = DEFAULT_TZ,
    ) -> None:
        get_zone(default_tz)
        self._db = db
        self.owner_id = owner_id
        self.clock: Clock = clock or SystemClock()
        self.default_tz = default_tz

    # ---- helpers -------------------------------------------------------------------------
    def _now_str(self) -> str:
        return to_utc_str(self.clock.now())

    def _now(self) -> datetime:
        return self.clock.now()

    def _rules(self, col: str, ids: Sequence[int]) -> dict[int, list[ReminderRule]]:
        out: dict[int, list[ReminderRule]] = {i: [] for i in ids}
        if not ids:
            return out
        marks = ",".join("?" * len(ids))
        # ``col`` is one of two literals chosen by this module, never user input.
        rows = self._db.query(
            f"SELECT * FROM reminder_rules WHERE {col} IN ({marks}) ORDER BY id",  # noqa: S608
            list(ids),
        )
        for r in rows:
            spec = ReminderSpec(
                offset_min=r["offset_min"], at_local=r["at_local"], day_offset=r["day_offset"]
            )
            out[r[col]].append(
                ReminderRule(id=r["id"], spec=spec, event_id=r["event_id"], todo_id=r["todo_id"])
            )
        return out

    @staticmethod
    def _save_rules(
        conn: sqlite3.Connection, col: str, item_id: int, specs: Sequence[ReminderSpec], now: str
    ) -> None:
        conn.execute(f"DELETE FROM reminder_rules WHERE {col} = ?", (item_id,))  # noqa: S608
        for s in specs:
            conn.execute(
                f"INSERT INTO reminder_rules ({col}, offset_min, at_local, day_offset,"  # noqa: S608
                " created_at_utc) VALUES (?, ?, ?, ?, ?)",
                (item_id, s.offset_min, s.at_local, s.day_offset, now),
            )

    def _fetch(
        self,
        conn: sqlite3.Connection,
        table: str,
        item_id: int,
        *,
        include_deleted: bool,
        missing: str,
    ) -> sqlite3.Row:
        """Owner-scoped row read on ``conn`` (so it can share a transaction with the write)."""
        # ``table`` is one of two literals chosen by this module, never user input.
        row = conn.execute(
            f"SELECT * FROM {table} WHERE id = ? AND owner_id = ?",  # noqa: S608
            (item_id, self.owner_id),
        ).fetchone()
        if row is None or (row["deleted_at_utc"] and not include_deleted):
            raise NotFoundError(missing)
        return row

    # ---- events --------------------------------------------------------------------------
    def _event_from_row(self, r: sqlite3.Row, rules: list[ReminderRule]) -> Event:
        def opt(v: str | None) -> datetime | None:
            return from_utc_str(v) if v else None

        return Event(
            id=r["id"],
            owner_id=r["owner_id"],
            title=r["title"],
            start=from_local_str(r["dtstart_local"], r["tz"]),
            tz=r["tz"],
            all_day=bool(r["all_day"]),
            duration_min=r["duration_min"],
            note=r["note"],
            place=r["place"],
            kind=r["kind"],
            rrule=r["rrule"],
            skip_holidays=bool(r["skip_holidays"]),
            series_id=r["series_id"],
            source=r["source"],
            created_at=opt(r["created_at_utc"]),
            updated_at=opt(r["updated_at_utc"]),
            deleted_at=opt(r["deleted_at_utc"]),
            reminders=tuple(rules),
        )

    def _event_columns(
        self, start: datetime, tz: str, all_day: bool, duration_min: int | None
    ) -> dict[str, Any]:
        """Normalize and derive columns (wall clock in ``tz``, start_utc, until_utc)."""
        local = _in_tz(start, tz)
        if all_day:
            local = local.replace(hour=0, minute=0, second=0, microsecond=0)
        if duration_min is not None and duration_min <= 0:
            raise ValidationError("所要時間が不正です")
        probe = Event(
            id=0, owner_id=0, title="x", start=local, tz=tz, all_day=all_day,
            duration_min=duration_min,
        )  # fmt: skip
        return {
            "dtstart_local": to_local_str(local),
            "start_utc": to_utc_str(local),
            "until_utc": to_utc_str(probe.end),
            "all_day": int(all_day),
            "duration_min": duration_min,
            "tz": tz,
        }

    def add_event(
        self,
        title: str,
        start: datetime,
        *,
        all_day: bool = False,
        duration_min: int | None = None,
        note: str | None = None,
        place: str | None = None,
        tz: str | None = None,
        reminders: Sequence[ReminderSpec] = (),
        kind: str = "event",
    ) -> Event:
        """Create a non-recurring event.

        TODO(V2-60): when ``rrule`` is stored, ``events.until_utc`` must be set to
        ``recurrence.series_until_utc(rrule, start, span)`` (``None`` if unbounded) instead of
        the single-occurrence end computed by :meth:`_event_columns`; ``expand`` pre-filters on it.
        """
        tz = tz or self.default_tz
        get_zone(tz)
        if kind not in ("event", "reminder"):
            raise ValidationError("種別が不正です")
        cols = self._event_columns(start, tz, all_day, duration_min)
        specs = _check_specs(reminders)
        now = self._now_str()
        with self._db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO events (owner_id, kind, title, note, place, all_day, dtstart_local,"
                " duration_min, tz, start_utc, until_utc, created_at_utc, updated_at_utc)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.owner_id,
                    kind,
                    _check_text(title, "タイトル", MAX_TITLE, required=True),
                    _check_text(note, "メモ", MAX_NOTE, required=False),
                    _check_text(place, "場所", MAX_PLACE, required=False),
                    cols["all_day"],
                    cols["dtstart_local"],
                    cols["duration_min"],
                    tz,
                    cols["start_utc"],
                    cols["until_utc"],
                    now,
                    now,
                ),
            )
            event_id = int(cur.lastrowid or 0)
            self._save_rules(conn, "event_id", event_id, specs, now)
            planner.reconcile_event(conn, event_id, self._now())
        return self.get_event(event_id)

    def get_event(self, event_id: int, *, include_deleted: bool = False) -> Event:
        row = self._db.query_one(
            "SELECT * FROM events WHERE id = ? AND owner_id = ?", (event_id, self.owner_id)
        )
        if row is None or (row["deleted_at_utc"] and not include_deleted):
            raise NotFoundError("予定が見つかりません")
        return self._event_from_row(row, self._rules("event_id", [event_id])[event_id])

    def update_event(
        self,
        event_id: int,
        *,
        title: str = UNSET,
        start: datetime = UNSET,
        all_day: bool = UNSET,
        duration_min: int | None = UNSET,
        note: str | None = UNSET,
        place: str | None = UNSET,
        reminders: Sequence[ReminderSpec] = UNSET,
    ) -> Event:
        """Edit given fields only (``None`` clears note/place/duration). Deleted events are
        not editable. Passing ``reminders`` replaces the whole rule set. Read and write share one
        transaction.

        TODO(V2-60): if ``rrule`` becomes editable, recompute ``until_utc`` with
        ``recurrence.series_until_utc(rrule, start, span)`` whenever rrule/start/duration change.
        """
        specs = None if reminders is UNSET else _check_specs(reminders)
        now = self._now_str()
        with self._db.transaction() as conn:
            row = self._fetch(
                conn, "events", event_id, include_deleted=False, missing="予定が見つかりません"
            )
            cur = self._event_from_row(row, [])
            sets: dict[str, Any] = {}
            if title is not UNSET:
                sets["title"] = _check_text(title, "タイトル", MAX_TITLE, required=True)
            if note is not UNSET:
                sets["note"] = _check_text(note, "メモ", MAX_NOTE, required=False)
            if place is not UNSET:
                sets["place"] = _check_text(place, "場所", MAX_PLACE, required=False)
            if any(v is not UNSET for v in (start, all_day, duration_min)):
                sets.update(
                    self._event_columns(
                        cur.start if start is UNSET else start,
                        cur.tz,
                        cur.all_day if all_day is UNSET else all_day,
                        cur.duration_min if duration_min is UNSET else duration_min,
                    )
                )
            sets["updated_at_utc"] = now
            names = ", ".join(f"{k} = ?" for k in sets)  # keys are literals from this method
            conn.execute(
                f"UPDATE events SET {names} WHERE id = ? AND owner_id = ?",  # noqa: S608
                [*sets.values(), event_id, self.owner_id],
            )
            if specs is not None:
                self._save_rules(conn, "event_id", event_id, specs, now)
            planner.reconcile_event(conn, event_id, self._now())
        return self.get_event(event_id)

    def delete_event(self, event_id: int) -> Event:
        """Soft delete (idempotent is NOT assumed: deleting a deleted event is NotFound)."""
        now = self._now_str()
        with self._db.transaction() as conn:
            self._fetch(
                conn, "events", event_id, include_deleted=False, missing="予定が見つかりません"
            )
            conn.execute(
                "UPDATE events SET deleted_at_utc = ?, updated_at_utc = ?"
                " WHERE id = ? AND owner_id = ?",
                (now, now, event_id, self.owner_id),
            )
            planner.reconcile_event(conn, event_id, self._now())
        return self.get_event(event_id, include_deleted=True)

    def restore_event(self, event_id: int) -> Event:
        """Undo a soft delete (no write, and ``updated_at`` untouched, if not deleted)."""
        with self._db.transaction() as conn:
            row = self._fetch(
                conn, "events", event_id, include_deleted=True, missing="予定が見つかりません"
            )
            if row["deleted_at_utc"]:
                conn.execute(
                    "UPDATE events SET deleted_at_utc = NULL, updated_at_utc = ?"
                    " WHERE id = ? AND owner_id = ?",
                    (self._now_str(), event_id, self.owner_id),
                )
                planner.reconcile_event(conn, event_id, self._now())
        return self.get_event(event_id)

    def set_event_reminders(self, event_id: int, specs: Sequence[ReminderSpec]) -> Event:
        """Store reminder rules only (no notification is generated in phase 2)."""
        return self.update_event(event_id, reminders=specs)

    def plan_all(self, *, mode: planner.Mode = "steady") -> planner.PlanStats:
        """Startup / daily top-up: reconcile every live event and TODO of this owner."""
        return planner.plan_all(self._db, self.owner_id, self._now(), mode=mode)

    def expand(self, window: Window, *, include_reminders_kind: bool = True) -> list[Occurrence]:
        """The single entry point for listing: occurrences overlapping ``window``, sorted by
        start (all-day first within a day). Recurring events (``rrule``) are expanded via
        ``recurrence``. Per-occurrence exceptions are not merged yet (planned for V2-60)."""
        rows = self._db.query(
            "SELECT * FROM events WHERE owner_id = ? AND deleted_at_utc IS NULL"
            " AND start_utc < ? AND (until_utc IS NULL OR until_utc >= ?)",
            (self.owner_id, to_utc_str(window.end), to_utc_str(window.start)),
        )
        out: list[Occurrence] = []
        for r in rows:
            if r["kind"] == "reminder" and not include_reminders_kind:
                continue
            ev = self._event_from_row(r, [])
            if ev.rrule:
                try:
                    probe = ev.end - ev.start
                    starts = recurrence.occurrence_starts(ev.rrule, ev.start, window, span=probe)
                except ValidationError:
                    continue  # corrupt rule: hide this event rather than break the listing
                pairs = [(loc, replace(ev, start=s)) for loc, s in starts]
            else:
                pairs = [(r["dtstart_local"], ev)]
            for occurrence_local, item in pairs:
                start, end = item.start, item.end
                if start >= window.end:
                    continue
                if end > start:
                    if end <= window.start:
                        continue
                elif start < window.start:  # instant event
                    continue
                out.append(
                    Occurrence(
                        event_id=ev.id,
                        occurrence_local=occurrence_local,
                        start=start,
                        end=end,
                        title=ev.title,
                        all_day=ev.all_day,
                        place=ev.place,
                        note=ev.note,
                        kind=ev.kind,
                    )
                )
        out.sort(key=lambda o: (o.start.astimezone(UTC), not o.all_day, o.event_id))
        return out

    # ---- todos ---------------------------------------------------------------------------
    def _todo_from_row(self, r: sqlite3.Row, rules: list[ReminderRule]) -> Todo:
        def opt(v: str | None) -> datetime | None:
            return from_utc_str(v) if v else None

        return Todo(
            id=r["id"],
            owner_id=r["owner_id"],
            title=r["title"],
            tz=r["tz"],
            list_name=r["list_name"],
            note=r["note"],
            priority=r["priority"],
            due=from_local_str(r["due_local"], r["tz"]) if r["due_local"] else None,
            due_all_day=bool(r["due_all_day"]),
            done_at=opt(r["done_at_utc"]),
            created_at=opt(r["created_at_utc"]),
            updated_at=opt(r["updated_at_utc"]),
            deleted_at=opt(r["deleted_at_utc"]),
            reminders=tuple(rules),
        )

    def _due_columns(self, due: datetime | None, due_all_day: bool, tz: str) -> dict[str, Any]:
        if due is None:
            if due_all_day:
                raise ValidationError("期限が必要です")
            return {"due_local": None, "due_utc": None, "due_all_day": 0}
        local = _in_tz(due, tz)
        if due_all_day:
            local = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return {
            "due_local": to_local_str(local),
            "due_utc": to_utc_str(local),
            "due_all_day": int(due_all_day),
        }

    def add_todo(
        self,
        title: str,
        *,
        due: datetime | None = None,
        due_all_day: bool = False,
        note: str | None = None,
        priority: int = 0,
        tz: str | None = None,
        reminders: Sequence[ReminderSpec] = (),
        list_name: str = "todo",
    ) -> Todo:
        tz = tz or self.default_tz
        get_zone(tz)
        if not 0 <= priority <= 3:
            raise ValidationError("優先度は0〜3です")
        specs = _check_specs(reminders)
        cols = self._due_columns(due, due_all_day, tz)
        validate_todo_reminders(due, due_all_day, specs)  # N3 + design 3.2
        now = self._now_str()
        with self._db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO todos (owner_id, list_name, title, note, priority, due_local,"
                " due_all_day, tz, due_utc, created_at_utc, updated_at_utc)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.owner_id,
                    _check_text(list_name, "リスト名", 50, required=True),
                    _check_text(title, "タイトル", MAX_TITLE, required=True),
                    _check_text(note, "メモ", MAX_NOTE, required=False),
                    priority,
                    cols["due_local"],
                    cols["due_all_day"],
                    tz,
                    cols["due_utc"],
                    now,
                    now,
                ),
            )
            todo_id = int(cur.lastrowid or 0)
            self._save_rules(conn, "todo_id", todo_id, specs, now)
            planner.reconcile_todo(conn, todo_id, self._now())
        return self.get_todo(todo_id)

    def get_todo(self, todo_id: int, *, include_deleted: bool = False) -> Todo:
        row = self._db.query_one(
            "SELECT * FROM todos WHERE id = ? AND owner_id = ?", (todo_id, self.owner_id)
        )
        if row is None or (row["deleted_at_utc"] and not include_deleted):
            raise NotFoundError("TODOが見つかりません")
        return self._todo_from_row(row, self._rules("todo_id", [todo_id])[todo_id])

    def update_todo(
        self,
        todo_id: int,
        *,
        title: str = UNSET,
        due: datetime | None = UNSET,
        due_all_day: bool = UNSET,
        note: str | None = UNSET,
        priority: int = UNSET,
        reminders: Sequence[ReminderSpec] = UNSET,
    ) -> Todo:
        """Edit given fields only. The final (due, due_all_day, rules) state is validated once
        (:func:`validate_todo_reminders`): clearing the due also clears the rules; a date-only
        due with existing/new ``offset_min`` rules is rejected; ``due_all_day=True`` without a
        due is rejected. Read and write share one transaction."""
        new_specs = None if reminders is UNSET else _check_specs(reminders)
        now = self._now_str()
        with self._db.transaction() as conn:
            row = self._fetch(
                conn, "todos", todo_id, include_deleted=False, missing="TODOが見つかりません"
            )
            cur = self._todo_from_row(row, self._rules("todo_id", [todo_id])[todo_id])
            sets: dict[str, Any] = {}
            if title is not UNSET:
                sets["title"] = _check_text(title, "タイトル", MAX_TITLE, required=True)
            if note is not UNSET:
                sets["note"] = _check_text(note, "メモ", MAX_NOTE, required=False)
            if priority is not UNSET:
                if not 0 <= priority <= 3:
                    raise ValidationError("優先度は0〜3です")
                sets["priority"] = priority
            new_due = cur.due if due is UNSET else due
            if new_due is None and due_all_day is not UNSET and due_all_day:
                raise ValidationError("期限が必要です")
            new_all_day = (
                False
                if new_due is None
                else (cur.due_all_day if due_all_day is UNSET else due_all_day)
            )
            if due is not UNSET or due_all_day is not UNSET:
                sets.update(self._due_columns(new_due, new_all_day, cur.tz))
            final = new_specs if new_specs is not None else [r.spec for r in cur.reminders]
            if new_due is None and new_specs is None and due is not UNSET:
                new_specs = final = []  # due cleared: rules are meaningless (N3)
            validate_todo_reminders(new_due, new_all_day, final)
            sets["updated_at_utc"] = now
            names = ", ".join(f"{k} = ?" for k in sets)
            conn.execute(
                f"UPDATE todos SET {names} WHERE id = ? AND owner_id = ?",  # noqa: S608
                [*sets.values(), todo_id, self.owner_id],
            )
            if new_specs is not None:
                self._save_rules(conn, "todo_id", todo_id, new_specs, now)
            planner.reconcile_todo(conn, todo_id, self._now())
        return self.get_todo(todo_id)

    def complete_todo(self, todo_id: int) -> Todo:
        """Mark done (idempotent: keeps the first completion time)."""
        now = self._now_str()
        with self._db.transaction() as conn:
            row = self._fetch(
                conn, "todos", todo_id, include_deleted=False, missing="TODOが見つかりません"
            )
            if not row["done_at_utc"]:
                conn.execute(
                    "UPDATE todos SET done_at_utc = ?, updated_at_utc = ?"
                    " WHERE id = ? AND owner_id = ?",
                    (now, now, todo_id, self.owner_id),
                )
            planner.reconcile_todo(conn, todo_id, self._now())
        return self.get_todo(todo_id)

    def reopen_todo(self, todo_id: int) -> Todo:
        """Mark open again (no write, and ``updated_at`` untouched, if already open)."""
        with self._db.transaction() as conn:
            row = self._fetch(
                conn, "todos", todo_id, include_deleted=False, missing="TODOが見つかりません"
            )
            if row["done_at_utc"]:
                conn.execute(
                    "UPDATE todos SET done_at_utc = NULL, updated_at_utc = ?"
                    " WHERE id = ? AND owner_id = ?",
                    (self._now_str(), todo_id, self.owner_id),
                )
            planner.reconcile_todo(conn, todo_id, self._now())
        return self.get_todo(todo_id)

    def delete_todo(self, todo_id: int) -> Todo:
        now = self._now_str()
        with self._db.transaction() as conn:
            self._fetch(
                conn, "todos", todo_id, include_deleted=False, missing="TODOが見つかりません"
            )
            conn.execute(
                "UPDATE todos SET deleted_at_utc = ?, updated_at_utc = ?"
                " WHERE id = ? AND owner_id = ?",
                (now, now, todo_id, self.owner_id),
            )
            planner.reconcile_todo(conn, todo_id, self._now())
        return self.get_todo(todo_id, include_deleted=True)

    def restore_todo(self, todo_id: int) -> Todo:
        """Undo a soft delete (no write, and ``updated_at`` untouched, if not deleted)."""
        with self._db.transaction() as conn:
            row = self._fetch(
                conn, "todos", todo_id, include_deleted=True, missing="TODOが見つかりません"
            )
            if row["deleted_at_utc"]:
                conn.execute(
                    "UPDATE todos SET deleted_at_utc = NULL, updated_at_utc = ?"
                    " WHERE id = ? AND owner_id = ?",
                    (self._now_str(), todo_id, self.owner_id),
                )
            planner.reconcile_todo(conn, todo_id, self._now())
        return self.get_todo(todo_id)

    def list_todos(self, status: str = "open", *, list_name: str | None = None) -> list[Todo]:
        """``status``: open | done | overdue | no_due (open without due) | all (not deleted).
        Open order: due ascending (no-due last), then priority desc, then id."""
        if status not in ("open", "done", "overdue", "no_due", "all"):
            raise ValidationError("状態の指定が不正です")
        sql = "SELECT * FROM todos WHERE owner_id = ? AND deleted_at_utc IS NULL"
        params: list[Any] = [self.owner_id]
        if list_name is not None:
            sql += " AND list_name = ?"
            params.append(list_name)
        rows = self._db.query(sql + " ORDER BY id", params)
        ids = [r["id"] for r in rows]
        rules = self._rules("todo_id", ids)
        todos = [self._todo_from_row(r, rules[r["id"]]) for r in rows]
        now = self._now()
        if status == "open":
            todos = [t for t in todos if not t.done]
        elif status == "done":
            todos = [t for t in todos if t.done]
            todos.sort(key=lambda t: t.done_at or now, reverse=True)
            return todos
        elif status == "overdue":
            todos = [t for t in todos if t.is_overdue(now)]
        elif status == "no_due":
            todos = [t for t in todos if not t.done and t.due is None]
        far = now + timedelta(days=365 * 100)
        todos.sort(key=lambda t: (t.due.astimezone(UTC) if t.due else far, -t.priority, t.id))
        return todos
