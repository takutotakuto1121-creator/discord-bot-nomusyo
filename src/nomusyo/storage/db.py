"""SQLite connection wrapper (single process, one connection guarded by a lock).

* ``isolation_level=None`` (autocommit): transactions are explicit via :meth:`Database.transaction`.
* ``foreign_keys=ON``, ``busy_timeout``, WAL (persistent), ``synchronous=NORMAL``.
* The connection is created with ``check_same_thread=False`` and every access takes an RLock, so
  it is safe to call from ``asyncio.to_thread`` workers.
* All SQL must be parameterized by callers (``?`` placeholders); never format values into SQL.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from nomusyo.storage.errors import StorageError

MIN_SQLITE_VERSION = (3, 35, 0)
BUSY_TIMEOUT_MS = 5000


def check_sqlite_version() -> None:
    if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
        need = ".".join(map(str, MIN_SQLITE_VERSION))
        raise StorageError(f"SQLite {need}+ is required (found {sqlite3.sqlite_version})")


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a connection with the standard PRAGMAs applied."""
    check_sqlite_version()
    conn = sqlite3.connect(
        str(path),
        isolation_level=None,
        check_same_thread=False,
        timeout=BUSY_TIMEOUT_MS / 1000,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")  # persistent; 'memory' for :memory:
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


class Database:
    """Thread-safe handle around one SQLite connection."""

    def __init__(self, conn: sqlite3.Connection, path: str | Path) -> None:
        self._conn = conn
        self.path = str(path)
        self._lock = threading.RLock()

    @classmethod
    def open(cls, path: str | Path, *, migrate: bool = True) -> Database:
        """Connect and (by default) apply pending migrations. Raises ``StorageError``."""
        try:
            conn = connect(path)
        except sqlite3.Error as exc:
            raise StorageError(f"cannot open database: {type(exc).__name__}") from None
        db = cls(conn, path)
        if migrate:
            from nomusyo.storage.migrations import apply_migrations

            try:
                apply_migrations(db)
            except BaseException:
                db.close()
                raise
        return db

    @property
    def connection(self) -> sqlite3.Connection:
        """Raw connection. Callers must hold :meth:`lock` (migrations/backup use it)."""
        return self._conn

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        """Run a single statement outside an explicit transaction (autocommit)."""
        with self._lock:
            return self._conn.execute(sql, params)

    def query(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(
        self, sql: str, params: Sequence[Any] | dict[str, Any] = ()
    ) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """``BEGIN IMMEDIATE`` ... ``COMMIT`` (``ROLLBACK`` on any exception). Re-entrant use is
        not supported: do not nest."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                # COMMIT itself may have failed; roll back only if still in a transaction and
                # never let a rollback error hide the original exception.
                if self._conn.in_transaction:
                    try:
                        self._conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                raise
