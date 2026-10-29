"""Migration runner (docs/design/schedule-data-model.md section 5.5).

* Files: ``migrations/NNNN_name.sql`` (4-digit, gap-free from 0001). Packaged as package data.
* History in ``schema_version`` (version, name, checksum SHA-256, applied_at_utc).
* One migration = one ``BEGIN IMMEDIATE`` transaction (SQLite DDL is transactional).
  Statements are split with ``sqlite3.complete_statement`` and run one by one because
  ``executescript`` may implicitly COMMIT.
* Foreign keys are OFF while migrating (so table rebuilds work) and ``PRAGMA foreign_key_check``
  must be empty before COMMIT. They are switched back ON afterwards.
* Before applying anything to a non-empty DB, a backup is made with the sqlite3 backup API.
* No down-migrations. A DB newer than the code, or an edited applied file, refuses to start.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

from nomusyo.storage.db import Database
from nomusyo.storage.errors import (
    ChecksumMismatchError,
    MigrationError,
    SchemaTooNewError,
    StorageError,
)

log = logging.getLogger(__name__)

BACKUP_KEEP = 5
_FILE_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str


def load_migrations() -> list[Migration]:
    """Discover packaged migrations. Raises ``MigrationError`` on gaps/duplicates/bad names."""
    root = resources.files("nomusyo.storage") / "migrations"
    found: dict[int, Migration] = {}
    for entry in root.iterdir():
        if not entry.name.endswith(".sql"):
            continue
        m = _FILE_RE.match(entry.name)
        if not m:
            raise MigrationError(f"bad migration file name: {entry.name}")
        version = int(m.group(1))
        if version in found:
            raise MigrationError(f"duplicate migration version: {version:04d}")
        # Normalize CRLF so the checksum does not depend on git autocrlf / editors.
        raw = entry.read_bytes().replace(b"\r\n", b"\n")
        found[version] = Migration(
            version=version,
            name=m.group(2),
            sql=raw.decode("utf-8"),
            checksum=hashlib.sha256(raw).hexdigest(),
        )
    versions = sorted(found)
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError("migration versions must be consecutive starting at 0001")
    return [found[v] for v in versions]


def split_statements(sql: str) -> list[str]:
    """Split a script into statements (handles comments, triggers and quoted semicolons)."""
    statements: list[str] = []
    buf = ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            statements.append(buf.strip())
            buf = ""
    leftover = [ln for ln in buf.splitlines() if ln.strip() and not ln.strip().startswith("--")]
    if leftover:
        raise MigrationError("migration ends with an incomplete statement")
    return statements


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


_SCHEMA_VERSION_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
  version        INTEGER PRIMARY KEY,
  name           TEXT NOT NULL,
  checksum       TEXT NOT NULL,
  applied_at_utc TEXT NOT NULL
)
"""


def _is_empty(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        " AND name <> 'schema_version'"
    ).fetchone()
    return row[0] == 0


def _backup(conn: sqlite3.Connection, db_path: str, current_version: int) -> Path | None:
    """Copy the DB with the backup API (safe under WAL). Skipped for in-memory DBs."""
    if db_path in ("", ":memory:") or db_path.startswith("file::memory:"):
        return None
    directory = Path(db_path).resolve().parent / "backups"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = directory / f"pre-migrate-v{current_version}-{stamp}.db"
    dest = sqlite3.connect(str(target))
    try:
        conn.backup(dest)
    finally:
        dest.close()
    olds = sorted(directory.glob("pre-migrate-v*.db"), key=lambda p: p.stat().st_mtime)
    for old in olds[:-BACKUP_KEEP]:
        old.unlink(missing_ok=True)
    return target


def apply_migrations(db: Database, migrations: list[Migration] | None = None) -> list[int]:
    """Verify history and apply pending migrations. Returns the applied versions."""
    code = load_migrations() if migrations is None else migrations
    code_max = code[-1].version if code else 0
    by_version = {m.version: m for m in code}

    with db.lock:
        conn = db.connection
        conn.execute(_SCHEMA_VERSION_DDL)
        applied = {
            r["version"]: r
            for r in conn.execute("SELECT version, name, checksum FROM schema_version")
        }
        db_max = max(applied, default=0)
        if db_max > code_max:
            raise SchemaTooNewError(
                f"database schema v{db_max} is newer than this code (v{code_max});"
                " refusing to start"
            )
        for version, row in applied.items():
            m = by_version.get(version)
            if m is None or m.checksum != row["checksum"] or m.name != row["name"]:
                raise ChecksumMismatchError(
                    f"applied migration {version:04d} does not match the packaged file"
                )
        pending = [m for m in code if m.version not in applied]
        if not pending:
            return []

        if not _is_empty(conn):
            try:
                path = _backup(conn, db.path, db_max)
            except (OSError, sqlite3.Error) as exc:
                raise StorageError(f"pre-migration backup failed: {type(exc).__name__}") from None
            if path is not None:
                log.info("pre-migration backup written: %s", path.name)

        done: list[int] = []
        conn.execute("PRAGMA foreign_keys = OFF")  # no-op inside a transaction, so set it first
        try:
            for m in pending:
                if _apply_one(conn, m):
                    done.append(m.version)
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
        return done


def _apply_one(conn: sqlite3.Connection, m: Migration) -> bool:
    """Apply one migration. Returns False if another process already applied it."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Re-check under the write lock: a concurrent starter may have applied it after our
        # (unlocked) pending computation. Without this the INSERT would fail on the PK.
        if conn.execute("SELECT 1 FROM schema_version WHERE version = ?", (m.version,)).fetchone():
            conn.execute("ROLLBACK")
            log.info("migration %04d_%s already applied by another process", m.version, m.name)
            return False
        for stmt in split_statements(m.sql):
            conn.execute(stmt)
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise MigrationError(f"migration {m.version:04d} violates foreign keys")
        conn.execute(
            "INSERT INTO schema_version (version, name, checksum, applied_at_utc)"
            " VALUES (?, ?, ?, ?)",
            (m.version, m.name, m.checksum, _utc_now()),
        )
        conn.execute("COMMIT")
    except BaseException as exc:
        if conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        if isinstance(exc, MigrationError):
            raise
        if isinstance(exc, sqlite3.Error):
            raise MigrationError(f"migration {m.version:04d} failed: {type(exc).__name__}") from exc
        raise
    log.info("applied migration %04d_%s", m.version, m.name)
    return True
