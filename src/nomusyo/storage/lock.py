"""Single-instance protection via the ``instance_lock`` row (resident-bot.md 5.3 (b)).

The holder refreshes ``heartbeat_utc`` periodically (suggest every 30 s). Another process may take
over only if the heartbeat is older than ``stale_after`` (default 120 s) or the lock was released
(clean shutdown deletes the row). After a crash a restart may therefore have to wait up to
``stale_after`` -- the process should exit non-zero and let Docker restart it.
This only guards instances that share one DB file; it cannot stop two bots with separate DBs.
"""

from __future__ import annotations

import os
import socket
import uuid
from datetime import UTC, datetime, timedelta

from nomusyo.storage.db import Database
from nomusyo.storage.errors import InstanceLockError

DEFAULT_STALE_AFTER = timedelta(seconds=120)
CLOCK_SKEW_TOLERANCE = timedelta(seconds=5)
_FMT = "%Y-%m-%dT%H:%M:%SZ"


def new_holder_id() -> str:
    """Host name + per-start random ID (no secrets)."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"


def _fmt(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime(_FMT)


def acquire_instance_lock(
    db: Database,
    holder: str,
    *,
    now: datetime | None = None,
    stale_after: timedelta = DEFAULT_STALE_AFTER,
) -> None:
    """Take the lock or raise ``InstanceLockError`` if another live holder exists."""
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT holder, heartbeat_utc FROM instance_lock WHERE id = 1"
        ).fetchone()
        if row is not None and row["holder"] != holder:
            beat = datetime.strptime(row["heartbeat_utc"], _FMT).replace(tzinfo=UTC)
            age = now - beat
            # A heartbeat far in the future means the clock was rewound: treat it as stale
            # instead of blocking until the clock catches up.
            if -CLOCK_SKEW_TOLERANCE < age < stale_after:
                raise InstanceLockError("another instance appears to be running")
        conn.execute(
            "INSERT INTO instance_lock (id, holder, started_at_utc, heartbeat_utc)"
            " VALUES (1, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET holder = excluded.holder,"
            " started_at_utc = excluded.started_at_utc, heartbeat_utc = excluded.heartbeat_utc",
            (holder, _fmt(now), _fmt(now)),
        )


def heartbeat_instance_lock(db: Database, holder: str, *, now: datetime | None = None) -> bool:
    """Refresh the heartbeat. Returns False if the lock was lost (taken by someone else)."""
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    cur = db.execute(
        "UPDATE instance_lock SET heartbeat_utc = ? WHERE id = 1 AND holder = ?",
        (_fmt(now), holder),
    )
    return cur.rowcount == 1


def release_instance_lock(db: Database, holder: str) -> None:
    db.execute("DELETE FROM instance_lock WHERE id = 1 AND holder = ?", (holder,))


def has_lock_table(db: Database) -> bool:
    row = db.query_one(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'instance_lock'"
    )
    return row is not None


def open_database_with_lock(path: str, holder: str) -> Database:
    """Open the DB without migrating, take the instance lock, then apply migrations.

    Schema is never advanced before the lock is held. The ``instance_lock`` table itself is
    created by migration 0001, so on a DB that does not have it yet (fresh install) migrations run
    first (they are serialized by ``BEGIN IMMEDIATE`` and re-checked per migration, so concurrent
    first starts are safe) and the lock is taken right after. Raises ``InstanceLockError`` if
    another live instance holds the lock; the DB is closed on any failure.
    """
    from nomusyo.storage.migrations import apply_migrations

    db = Database.open(path, migrate=False)
    try:
        if has_lock_table(db):
            acquire_instance_lock(db, holder)
            apply_migrations(db)
        else:
            apply_migrations(db)
            acquire_instance_lock(db, holder)
    except BaseException:
        db.close()
        raise
    return db
