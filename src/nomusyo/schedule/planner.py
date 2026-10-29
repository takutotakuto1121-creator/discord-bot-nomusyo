"""Notification planner (Discord independent): materialize ``notifications`` rows from
``reminder_rules`` for events (single + recurring) and TODOs. Design: schedule-data-model.md 4.

* Rolling window ``[now - STALE_DAYS, now + MATERIALIZE_DAYS]`` on ``scheduled_utc``.
* Identity is the ``dedupe_key`` string (UNIQUE); a re-run only applies a diff (insert, update
  times of ``pending`` rows, skip, revive). ``sending`` / ``sent`` / ``failed`` and
  non-revivable ``skipped`` rows are never touched.
* ``reconcile_event`` / ``reconcile_todo`` take an open connection so that ``ScheduleService``
  can call them inside its own transaction. ``plan_all`` opens one transaction per target.
* All datetimes are aware; stored as UTC strings. Wall-clock rules (``at_local``, all-day
  notification time) are resolved in the target's own ``tz`` (DST safe).
* Nothing here sends anything, and no title/note text is ever copied into ``notifications``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Literal

from nomusyo.schedule import recurrence
from nomusyo.schedule.digest import parse_bool, parse_hhmm
from nomusyo.schedule.errors import ValidationError
from nomusyo.schedule.models import (
    MAX_DAY_OFFSET,
    Window,
    from_local_str,
    get_zone,
    to_utc_str,
)
from nomusyo.storage.db import Database

MATERIALIZE_DAYS = 35
STALE_DAYS = 3

KEY_NOTIFY_AT_START = "notify_at_start"
KEY_NOTIFY_AT_DUE = "notify_at_due"
KEY_ALL_DAY_TIME = "all_day_notify_time"
DEFAULT_ALL_DAY_TIME = time(8, 0)

Mode = Literal["edit", "initial", "steady"]
# skip reasons that may be revived when the target is wanted again (design 4.3)
REVIVABLE = ("superseded", "exception_skip", "target_deleted", "todo_done")


@dataclass(frozen=True)
class PlanStats:
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    revived: int = 0

    def __add__(self, other: PlanStats) -> PlanStats:
        return PlanStats(
            self.inserted + other.inserted,
            self.updated + other.updated,
            self.skipped + other.skipped,
            self.revived + other.revived,
        )


@dataclass(frozen=True)
class _Want:
    key: str
    kind: str
    occurrence_local: str
    rule_id: int | None
    target_utc: str
    scheduled_utc: str


def _setting(conn: sqlite3.Connection, owner_id: int, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM settings WHERE owner_id = ? AND key = ?", (owner_id, key)
    ).fetchone()
    return None if row is None else row["value"]


@dataclass(frozen=True)
class _Opts:
    at_start: bool
    at_due: bool
    all_day_time: time

    @classmethod
    def load(cls, conn: sqlite3.Connection, owner_id: int) -> _Opts:
        return cls(
            at_start=parse_bool(_setting(conn, owner_id, KEY_NOTIFY_AT_START), True),
            at_due=parse_bool(_setting(conn, owner_id, KEY_NOTIFY_AT_DUE), True),
            all_day_time=parse_hhmm(_setting(conn, owner_id, KEY_ALL_DAY_TIME))
            or DEFAULT_ALL_DAY_TIME,
        )


def window_for(now: datetime) -> Window:
    return Window(now - timedelta(days=STALE_DAYS), now + timedelta(days=MATERIALIZE_DAYS))


def _wall(day, t: time, zone) -> datetime:
    """Wall clock ``day`` + ``t`` in ``zone`` as UTC (a gap time shifts forward by zone rules)."""
    naive = datetime.combine(day, t)
    return naive.replace(tzinfo=zone).astimezone(UTC)


def _hhmm(s: str) -> time:
    return time(int(s[:2]), int(s[3:5]))


def _rule_key(prefix: str, rule: sqlite3.Row) -> str:
    if rule["offset_min"] is not None:
        return f"{prefix}:m{rule['offset_min']}"
    return f"{prefix}:a{rule['day_offset']}@{rule['at_local'].replace(':', '')}"


def _scheduled_for_rule(
    rule: sqlite3.Row, target: datetime, all_day: bool, zone
) -> datetime | None:
    """Fire time (UTC) of a rule for a target (aware, in ``zone``); ``None`` if not applicable
    (``offset_min`` is meaningless for date-only targets)."""
    if rule["offset_min"] is not None:
        if all_day:
            return None
        return target.astimezone(UTC) - timedelta(minutes=rule["offset_min"])
    day = target.astimezone(zone).date() - timedelta(days=rule["day_offset"])
    return _wall(day, _hhmm(rule["at_local"]), zone)


def _wants_for_target(
    rules: list[sqlite3.Row],
    *,
    prefix: str,
    occurrence_local: str,
    target: datetime,
    all_day: bool,
    zone,
    base_kind: str | None,
    opts: _Opts,
    win: Window,
) -> list[_Want]:
    """Wanted rows of one occurrence (event) or one due (TODO), restricted to the window."""
    out: list[_Want] = []
    target_s = to_utc_str(target)

    def add(key: str, kind: str, rule_id: int | None, when: datetime) -> None:
        if win.start <= when <= win.end:
            out.append(_Want(key, kind, occurrence_local, rule_id, target_s, to_utc_str(when)))

    for r in rules:
        when = _scheduled_for_rule(r, target, all_day, zone)
        if when is not None:
            add(_rule_key(prefix, r), "remind", r["id"], when)
    if base_kind is not None and (opts.at_start if base_kind == "start" else opts.at_due):
        when = (
            _wall(target.astimezone(zone).date(), opts.all_day_time, zone)
            if all_day
            else target.astimezone(UTC)
        )
        add(f"{prefix}:{base_kind}", base_kind, None, when)
    return out


def _apply(
    conn: sqlite3.Connection,
    *,
    owner_id: int,
    target_col: str,
    target_id: int,
    wants: list[_Want],
    gone_reason: str | None,
    now: datetime,
    mode: Mode,
) -> PlanStats:
    """Diff ``wants`` against existing rows of one target (design 4.4 step 2/3)."""
    now_s = to_utc_str(now)
    existing = {
        r["dedupe_key"]: r
        for r in conn.execute(
            f"SELECT * FROM notifications WHERE {target_col} = ?",  # noqa: S608 (literal column)
            (target_id,),
        )
    }
    ins = upd = skp = rev = 0
    wanted_keys = {w.key for w in wants}
    target_type = "event" if target_col == "event_id" else "todo"

    for w in wants:
        row = existing.get(w.key)
        past = w.scheduled_utc < now_s
        if row is None:
            status, reason = "pending", None
            if past and mode != "steady":
                status, reason = "skipped", "past_at_creation"
            conn.execute(
                "INSERT INTO notifications (owner_id, target_type, event_id, todo_id, rule_id,"
                " kind, occurrence_local, dedupe_key, target_utc, scheduled_utc, fire_at_utc,"
                " status, skip_reason, created_at_utc, updated_at_utc)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(dedupe_key) DO NOTHING",
                (
                    owner_id,
                    target_type,
                    target_id if target_type == "event" else None,
                    target_id if target_type == "todo" else None,
                    w.rule_id,
                    w.kind,
                    w.occurrence_local,
                    w.key,
                    w.target_utc,
                    w.scheduled_utc,
                    w.scheduled_utc,
                    status,
                    reason,
                    now_s,
                    now_s,
                ),
            )
            ins += 1
            continue
        status = row["status"]
        changed = (
            row["scheduled_utc"] != w.scheduled_utc
            or row["target_utc"] != w.target_utc
            or row["rule_id"] != w.rule_id
        )
        if status == "pending":
            if not changed:
                continue
            if row["scheduled_utc"] != w.scheduled_utc and past and mode != "steady":
                conn.execute(
                    "UPDATE notifications SET status='skipped', skip_reason='past_at_creation',"
                    " rule_id=?, target_utc=?, scheduled_utc=?, fire_at_utc=?, updated_at_utc=?"
                    " WHERE id=? AND status='pending'",
                    (w.rule_id, w.target_utc, w.scheduled_utc, w.scheduled_utc, now_s, row["id"]),
                )
                skp += 1
                continue
            # keep a backed-off fire_at when only the target/rule changed
            fire = (
                w.scheduled_utc if row["scheduled_utc"] != w.scheduled_utc else row["fire_at_utc"]
            )
            conn.execute(
                "UPDATE notifications SET rule_id=?, target_utc=?, scheduled_utc=?, fire_at_utc=?,"
                " updated_at_utc=? WHERE id=? AND status='pending'",
                (w.rule_id, w.target_utc, w.scheduled_utc, fire, now_s, row["id"]),
            )
            upd += 1
        elif status == "skipped" and row["skip_reason"] in REVIVABLE and w.scheduled_utc > now_s:
            conn.execute(
                "UPDATE notifications SET status='pending', skip_reason=NULL, rule_id=?,"
                " target_utc=?, scheduled_utc=?, fire_at_utc=?, updated_at_utc=?"
                " WHERE id=? AND status='skipped'",
                (w.rule_id, w.target_utc, w.scheduled_utc, w.scheduled_utc, now_s, row["id"]),
            )
            rev += 1
        # sending / sent / failed / non-revivable skipped: never touched

    lower = to_utc_str(window_for(now).start)
    for key, row in existing.items():
        if key in wanted_keys or row["status"] != "pending":
            continue
        # When the target is gone every pending row goes; otherwise only rows inside the
        # rolling window (older pending rows belong to the dispatcher's stale handling).
        if gone_reason is None and row["scheduled_utc"] < lower:
            continue
        conn.execute(
            "UPDATE notifications SET status='skipped', skip_reason=?, updated_at_utc=?"
            " WHERE id=? AND status='pending'",
            (gone_reason or "superseded", now_s, row["id"]),
        )
        skp += 1
    return PlanStats(ins, upd, skp, rev)


def reconcile_event(
    conn: sqlite3.Connection, event_id: int, now: datetime, *, mode: Mode = "edit"
) -> PlanStats:
    """Bring the notifications of one event in line with its current state. Call inside the
    transaction that changed the event/rules. Unknown ids are a no-op."""
    ev = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if ev is None:
        return PlanStats()
    owner_id = ev["owner_id"]
    gone = "target_deleted" if ev["deleted_at_utc"] else None
    wants: list[_Want] = []
    if gone is None:
        planned = _event_wants(conn, ev, now)
        if planned is None:  # corrupt rrule: leave the existing rows alone
            return PlanStats()
        wants = planned
    return _apply(
        conn,
        owner_id=owner_id,
        target_col="event_id",
        target_id=event_id,
        wants=wants,
        gone_reason=gone,
        now=now,
        mode=mode,
    )


def _event_wants(conn: sqlite3.Connection, ev: sqlite3.Row, now: datetime) -> list[_Want] | None:
    zone = get_zone(ev["tz"])
    all_day = bool(ev["all_day"])
    opts = _Opts.load(conn, ev["owner_id"])
    win = window_for(now)
    rules = conn.execute(
        "SELECT * FROM reminder_rules WHERE event_id = ? ORDER BY id", (ev["id"],)
    ).fetchall()
    lead = timedelta(days=MAX_DAY_OFFSET + 1)
    for r in rules:
        if r["offset_min"] is not None:
            lead = max(lead, timedelta(minutes=r["offset_min"]))
    start = from_local_str(ev["dtstart_local"], ev["tz"])
    if ev["rrule"]:
        try:
            occs = recurrence.occurrence_starts(
                ev["rrule"], start, Window(win.start, win.end + lead)
            )
        except ValidationError:
            return None
    else:
        occs = [(ev["dtstart_local"], start)]
    wants: list[_Want] = []
    for occurrence_local, s in occs:
        wants += _wants_for_target(
            rules,
            prefix=f"e:{ev['id']}:{occurrence_local}",
            occurrence_local=occurrence_local,
            target=s,
            all_day=all_day,
            zone=zone,
            base_kind="start",
            opts=opts,
            win=win,
        )
    return wants


def reconcile_todo(
    conn: sqlite3.Connection, todo_id: int, now: datetime, *, mode: Mode = "edit"
) -> PlanStats:
    """Same as :func:`reconcile_event` for a TODO. Done/deleted/no-due TODOs want nothing, so
    their pending rows become skipped (``todo_done`` / ``target_deleted`` / ``superseded``);
    reopening or restoring revives rows whose time is still in the future."""
    t = conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()
    if t is None:
        return PlanStats()
    gone: str | None = None
    if t["deleted_at_utc"]:
        gone = "target_deleted"
    elif t["done_at_utc"]:
        gone = "todo_done"
    elif t["due_local"] is None:
        gone = "superseded"
    wants: list[_Want] = []
    if gone is None:
        wants = _todo_wants(conn, t, now)
    return _apply(
        conn,
        owner_id=t["owner_id"],
        target_col="todo_id",
        target_id=todo_id,
        wants=wants,
        gone_reason=gone,
        now=now,
        mode=mode,
    )


def _todo_wants(conn: sqlite3.Connection, t: sqlite3.Row, now: datetime) -> list[_Want]:
    zone = get_zone(t["tz"])
    opts = _Opts.load(conn, t["owner_id"])
    rules = conn.execute(
        "SELECT * FROM reminder_rules WHERE todo_id = ? ORDER BY id", (t["id"],)
    ).fetchall()
    due = from_local_str(t["due_local"], t["tz"])
    return _wants_for_target(
        rules,
        prefix=f"t:{t['id']}:{t['due_local']}",
        occurrence_local=t["due_local"],
        target=due,
        all_day=bool(t["due_all_day"]),
        zone=zone,
        base_kind="due",
        opts=opts,
        win=window_for(now),
    )


def plan_all(db: Database, owner_id: int, now: datetime, *, mode: Mode = "steady") -> PlanStats:
    """Reconcile every live event/TODO of ``owner_id`` (startup and daily top-up), one
    transaction per target. Also cleans up pending rows of deleted/done targets.
    ``mode='steady'`` re-inserts missed past rows inside the stale window as ``pending``;
    use ``'initial'`` for the first backfill so history is not replayed."""
    win = window_for(now)
    horizon = to_utc_str(win.end + timedelta(days=MAX_DAY_OFFSET + 1))
    event_ids = [
        r["id"]
        for r in db.query(
            "SELECT id FROM events WHERE owner_id = ? AND ((deleted_at_utc IS NULL"
            " AND start_utc < ? AND (until_utc IS NULL OR until_utc >= ?))"
            " OR id IN (SELECT event_id FROM notifications WHERE status = 'pending'"
            " AND event_id IS NOT NULL))",
            (owner_id, horizon, to_utc_str(win.start)),
        )
    ]
    todo_ids = [
        r["id"]
        for r in db.query(
            "SELECT id FROM todos WHERE owner_id = ? AND ((deleted_at_utc IS NULL"
            " AND done_at_utc IS NULL AND due_local IS NOT NULL)"
            " OR id IN (SELECT todo_id FROM notifications WHERE status = 'pending'"
            " AND todo_id IS NOT NULL))",
            (owner_id,),
        )
    ]
    total = PlanStats()
    for eid in event_ids:
        with db.transaction() as conn:
            total += reconcile_event(conn, eid, now, mode=mode)
    for tid in todo_ids:
        with db.transaction() as conn:
            total += reconcile_todo(conn, tid, now, mode=mode)
    return total


__all__ = [
    "MATERIALIZE_DAYS",
    "STALE_DAYS",
    "PlanStats",
    "plan_all",
    "reconcile_event",
    "reconcile_todo",
    "window_for",
]
