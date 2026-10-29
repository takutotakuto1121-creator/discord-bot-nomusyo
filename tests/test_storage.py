import os
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from nomusyo.storage import (
    ChecksumMismatchError,
    Database,
    InstanceLockError,
    MigrationError,
    SchemaTooNewError,
    acquire_instance_lock,
    heartbeat_instance_lock,
    new_holder_id,
    release_instance_lock,
)
from nomusyo.storage.migrations import (
    BACKUP_KEEP,
    Migration,
    apply_migrations,
    load_migrations,
    split_statements,
)

NOW = "2026-10-29T00:00:00Z"


@pytest.fixture
def db(tmp_path):
    (tmp_path / "data").mkdir()
    d = Database.open(tmp_path / "data" / "d.db")
    yield d
    d.close()


def tables(d):
    return {r["name"] for r in d.query("SELECT name FROM sqlite_master WHERE type='table'")}


def insert_event(d, **over):
    row = {
        "owner_id": 1,
        "title": "t",
        "dtstart_local": "2026-11-03T15:00:00",
        "start_utc": "2026-11-03T06:00:00Z",
        "created_at_utc": NOW,
        "updated_at_utc": NOW,
    }
    row.update(over)
    cols = ",".join(row)
    marks = ",".join("?" * len(row))
    cur = d.execute(f"INSERT INTO events ({cols}) VALUES ({marks})", list(row.values()))
    return cur.lastrowid


def insert_todo(d, **over):
    row = {"owner_id": 1, "title": "t", "created_at_utc": NOW, "updated_at_utc": NOW}
    row.update(over)
    cols = ",".join(row)
    marks = ",".join("?" * len(row))
    cur = d.execute(f"INSERT INTO todos ({cols}) VALUES ({marks})", list(row.values()))
    return cur.lastrowid


def insert_rule(d, **over):
    row = {"day_offset": 0, "created_at_utc": NOW}
    row.update(over)
    cols = ",".join(row)
    marks = ",".join("?" * len(row))
    d.execute(f"INSERT INTO reminder_rules ({cols}) VALUES ({marks})", list(row.values()))


# ---- migrations ----------------------------------------------------------------------------
def test_empty_db_gets_all_tables_and_history(db):
    t = tables(db)
    assert {
        "events",
        "todos",
        "reminder_rules",
        "settings",
        "job_runs",
        "instance_lock",
        "schema_version",
    } <= t
    rows = db.query("SELECT * FROM schema_version ORDER BY version")
    mig = load_migrations()
    assert [r["version"] for r in rows] == [m.version for m in mig]
    assert rows[0]["name"] == "initial"
    assert rows[0]["checksum"] == mig[0].checksum
    assert len(rows[0]["checksum"]) == 64
    assert rows[0]["applied_at_utc"].endswith("Z")


def test_fresh_db_makes_no_backup(tmp_path):
    p = tmp_path / "d.db"
    Database.open(p).close()
    assert not (tmp_path / "backups").exists()


def test_reapply_is_noop(db):
    assert apply_migrations(db) == []


def test_wal_and_foreign_keys(db):
    assert db.query_one("PRAGMA journal_mode")[0] == "wal"
    assert db.query_one("PRAGMA foreign_keys")[0] == 1


def test_in_memory_db_migrates_without_backup():
    d = Database.open(":memory:")
    assert "events" in tables(d)
    d.close()


LATEST = max(m.version for m in load_migrations())


def _extra(version=LATEST + 1, sql="CREATE TABLE extra (x INTEGER);"):
    return Migration(version=version, name="extra", sql=sql, checksum="c" * 64)


def test_backup_before_applying_to_nonempty_db(db, tmp_path):
    insert_event(db, title="keep me")
    apply_migrations(db, [*load_migrations(), _extra()])
    backups = list((tmp_path / "data" / "backups").glob(f"pre-migrate-v{LATEST}-*.db"))
    assert len(backups) == 1
    assert "extra" in tables(db)
    b = sqlite3.connect(str(backups[0]))
    try:
        assert b.execute("SELECT title FROM events").fetchone()[0] == "keep me"
        assert b.execute("SELECT name FROM sqlite_master WHERE name='extra'").fetchone() is None
    finally:
        b.close()


def test_backup_keeps_five_generations(db, tmp_path):
    bdir = tmp_path / "data" / "backups"
    bdir.mkdir()
    for i in range(7):
        f = bdir / f"pre-migrate-v{LATEST}-2020010{i}T000000Z.db"
        f.write_bytes(b"x")
        os.utime(f, (1_000_000 + i, 1_000_000 + i))
    insert_event(db)
    apply_migrations(db, [*load_migrations(), _extra()])
    files = sorted(bdir.glob("pre-migrate-v*.db"))
    assert len(files) == BACKUP_KEEP
    # the oldest ones were pruned
    names = {f.name for f in files}
    assert f"pre-migrate-v{LATEST}-20200100T000000Z.db" not in names
    assert f"pre-migrate-v{LATEST}-20200101T000000Z.db" not in names


def test_failed_migration_rolls_back_and_keeps_backup(db, tmp_path):
    insert_event(db, title="orig")
    bad = _extra(sql="CREATE TABLE half (x INTEGER);\nINSERT INTO no_such_table VALUES (1);")
    with pytest.raises(MigrationError):
        apply_migrations(db, [*load_migrations(), bad])
    assert "half" not in tables(db)
    assert db.query_one("SELECT max(version) FROM schema_version")[0] == LATEST
    assert db.query_one("SELECT title FROM events")["title"] == "orig"
    assert list((tmp_path / "data" / "backups").glob(f"pre-migrate-v{LATEST}-*.db"))
    # connection usable and FK enforcement restored
    assert db.query_one("PRAGMA foreign_keys")[0] == 1
    assert not db.connection.in_transaction


def test_failed_migration_error_has_no_row_data(db):
    bad = _extra(sql="INSERT INTO events (title) VALUES ('SECRET-VALUE');")
    with pytest.raises(MigrationError) as ei:
        apply_migrations(db, [*load_migrations(), bad])
    assert "SECRET-VALUE" not in str(ei.value)


def test_migration_violating_foreign_keys_is_rejected(db):
    bad = _extra(
        sql="INSERT INTO reminder_rules (event_id, offset_min, day_offset, created_at_utc)"
        " VALUES (999, 5, 0, 'x');"
    )
    with pytest.raises(MigrationError):
        apply_migrations(db, [*load_migrations(), bad])
    assert db.query("SELECT * FROM reminder_rules") == []


def test_schema_too_new(tmp_path):
    p = tmp_path / "d.db"
    d = Database.open(p)
    d.execute("INSERT INTO schema_version VALUES (99, 'future', 'abc', ?)", (NOW,))
    d.close()
    with pytest.raises(SchemaTooNewError):
        Database.open(p)


def test_checksum_mismatch(tmp_path):
    p = tmp_path / "d.db"
    d = Database.open(p)
    d.execute("UPDATE schema_version SET checksum = 'deadbeef' WHERE version = 1")
    d.close()
    with pytest.raises(ChecksumMismatchError):
        Database.open(p)


def test_name_mismatch_is_detected(tmp_path):
    p = tmp_path / "d.db"
    d = Database.open(p)
    d.execute("UPDATE schema_version SET name = 'renamed' WHERE version = 1")
    d.close()
    with pytest.raises(ChecksumMismatchError):
        Database.open(p)


def test_split_statements_handles_quoted_semicolons_and_comments():
    sql = "-- c\nCREATE TABLE a (x TEXT DEFAULT ';');\n-- more\nINSERT INTO a VALUES ('a;b');\n"
    st = split_statements(sql)
    assert len(st) == 2
    assert "';'" in st[0]


def test_split_statements_incomplete_raises():
    with pytest.raises(MigrationError):
        split_statements("CREATE TABLE a (x")


def test_load_migrations_are_consecutive():
    mig = load_migrations()
    assert [m.version for m in mig] == list(range(1, len(mig) + 1))


# ---- constraints ---------------------------------------------------------------------------
def test_event_constraints(db):
    insert_event(db)
    with pytest.raises(sqlite3.IntegrityError):
        insert_event(db, title="")
    with pytest.raises(sqlite3.IntegrityError):
        insert_event(db, title="a" * 201)
    insert_event(db, title="a" * 200)
    with pytest.raises(sqlite3.IntegrityError):
        insert_event(db, kind="bogus")
    with pytest.raises(sqlite3.IntegrityError):
        insert_event(db, all_day=2)
    with pytest.raises(sqlite3.IntegrityError):
        insert_event(db, dtstart_local="2026-11-03")
    with pytest.raises(sqlite3.IntegrityError):
        insert_event(db, start_utc="2026-11-03")
    with pytest.raises(sqlite3.IntegrityError):
        insert_event(db, duration_min=0)
    with pytest.raises(sqlite3.IntegrityError):
        insert_event(db, note="n" * 2001)
    with pytest.raises(sqlite3.IntegrityError):
        insert_event(db, place="p" * 201)


def test_reminder_kind_cannot_recur(db):
    with pytest.raises(sqlite3.IntegrityError):
        insert_event(db, kind="reminder", rrule="FREQ=DAILY")
    insert_event(db, kind="event", rrule="FREQ=DAILY")


def test_event_external_uid_unique(db):
    insert_event(db, source="ics", external_uid="u1")
    with pytest.raises(sqlite3.IntegrityError):
        insert_event(db, source="ics", external_uid="u1")
    insert_event(db, source="ics", external_uid="u2")
    insert_event(db, owner_id=2, source="ics", external_uid="u1")
    insert_event(db)  # NULL uid may repeat
    insert_event(db)


def test_event_series_fk(db):
    with pytest.raises(sqlite3.IntegrityError):
        insert_event(db, series_id=12345)


def test_todo_constraints(db):
    insert_todo(db)
    with pytest.raises(sqlite3.IntegrityError):
        insert_todo(db, title="")
    with pytest.raises(sqlite3.IntegrityError):
        insert_todo(db, priority=4)
    with pytest.raises(sqlite3.IntegrityError):
        insert_todo(db, priority=-1)
    # due_local and due_utc must be both set or both NULL
    with pytest.raises(sqlite3.IntegrityError):
        insert_todo(db, due_local="2026-11-03T00:00:00")
    with pytest.raises(sqlite3.IntegrityError):
        insert_todo(db, due_utc="2026-11-02T15:00:00Z")
    insert_todo(db, due_local="2026-11-03T00:00:00", due_utc="2026-11-02T15:00:00Z")
    # all-day flag requires a due
    with pytest.raises(sqlite3.IntegrityError):
        insert_todo(db, due_all_day=1)
    insert_todo(db, due_local="2026-11-03T00:00:00", due_utc="2026-11-02T15:00:00Z", due_all_day=1)


def test_reminder_rule_exclusive_target(db):
    e = insert_event(db)
    t = insert_todo(db)
    insert_rule(db, event_id=e, offset_min=10)
    insert_rule(db, todo_id=t, offset_min=10)
    with pytest.raises(sqlite3.IntegrityError):  # neither target
        insert_rule(db, offset_min=10)
    with pytest.raises(sqlite3.IntegrityError):  # both targets
        insert_rule(db, event_id=e, todo_id=t, offset_min=10)


def test_reminder_rule_exclusive_timing(db):
    e = insert_event(db)
    with pytest.raises(sqlite3.IntegrityError):  # neither offset nor at_local
        insert_rule(db, event_id=e)
    with pytest.raises(sqlite3.IntegrityError):  # both
        insert_rule(db, event_id=e, offset_min=5, at_local="20:00")
    with pytest.raises(sqlite3.IntegrityError):
        insert_rule(db, event_id=e, offset_min=-1)
    with pytest.raises(sqlite3.IntegrityError):
        insert_rule(db, event_id=e, at_local="8:00")
    with pytest.raises(sqlite3.IntegrityError):
        insert_rule(db, event_id=e, at_local="20:00", day_offset=-1)
    insert_rule(db, event_id=e, at_local="20:00", day_offset=1)


def test_reminder_rule_unique_and_fk(db):
    e = insert_event(db)
    insert_rule(db, event_id=e, offset_min=10)
    with pytest.raises(sqlite3.IntegrityError):
        insert_rule(db, event_id=e, offset_min=10)
    insert_rule(db, event_id=e, offset_min=11)
    insert_rule(db, event_id=e, at_local="09:00")
    with pytest.raises(sqlite3.IntegrityError):
        insert_rule(db, event_id=e, at_local="09:00")
    with pytest.raises(sqlite3.IntegrityError):  # dangling FK
        insert_rule(db, event_id=9999, offset_min=1)
    with pytest.raises(sqlite3.IntegrityError):
        insert_rule(db, todo_id=9999, offset_min=1)


def test_rules_cascade_on_parent_delete(db):
    e = insert_event(db)
    t = insert_todo(db)
    insert_rule(db, event_id=e, offset_min=10)
    insert_rule(db, todo_id=t, offset_min=10)
    db.execute("DELETE FROM events WHERE id = ?", (e,))
    db.execute("DELETE FROM todos WHERE id = ?", (t,))
    assert db.query("SELECT * FROM reminder_rules") == []


def test_job_runs_status_check_and_pk(db):
    db.execute(
        "INSERT INTO job_runs (job, run_date, status, started_at_utc)"
        " VALUES ('d','2026-10-29','done',?)",
        (NOW,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO job_runs (job, run_date, status, started_at_utc)"
            " VALUES ('d','2026-10-29','done',?)",
            (NOW,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO job_runs (job, run_date, status, started_at_utc)"
            " VALUES ('d','2026-10-30','weird',?)",
            (NOW,),
        )


def test_settings_pk(db):
    db.execute("INSERT INTO settings VALUES (1,'k','v',?)", (NOW,))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO settings VALUES (1,'k','v2',?)", (NOW,))


def test_transaction_rolls_back_on_error(db):
    with pytest.raises(RuntimeError):
        with db.transaction() as conn:
            conn.execute("INSERT INTO settings VALUES (1,'k','v',?)", (NOW,))
            raise RuntimeError("boom")
    assert db.query("SELECT * FROM settings") == []
    with db.transaction() as conn:
        conn.execute("INSERT INTO settings VALUES (1,'k','v',?)", (NOW,))
    assert len(db.query("SELECT * FROM settings")) == 1


# ---- SQL injection -------------------------------------------------------------------------
@pytest.mark.parametrize(
    "evil",
    [
        "'; DROP TABLE events; --",
        "x'); DELETE FROM todos; --",
        '" OR 1=1 --',
        "Robert'); DROP TABLE todos;--",
    ],
)
def test_parameterized_values_are_stored_verbatim(db, evil):
    insert_event(db, title=evil)
    insert_todo(db, title=evil)
    assert db.query_one("SELECT title FROM events")["title"] == evil
    assert db.query_one("SELECT title FROM todos")["title"] == evil
    assert {"events", "todos"} <= tables(db)


# ---- instance lock -------------------------------------------------------------------------
def test_instance_lock_blocks_second_holder(db):
    acquire_instance_lock(db, "a")
    with pytest.raises(InstanceLockError):
        acquire_instance_lock(db, "b")


def test_instance_lock_reacquire_by_same_holder(db):
    acquire_instance_lock(db, "a")
    acquire_instance_lock(db, "a")
    assert len(db.query("SELECT * FROM instance_lock")) == 1


def test_instance_lock_after_release(db):
    acquire_instance_lock(db, "a")
    release_instance_lock(db, "a")
    acquire_instance_lock(db, "b")
    assert db.query_one("SELECT holder FROM instance_lock")["holder"] == "b"


def test_release_by_non_holder_does_nothing(db):
    acquire_instance_lock(db, "a")
    release_instance_lock(db, "b")
    assert db.query_one("SELECT holder FROM instance_lock")["holder"] == "a"


def test_stale_lock_can_be_taken_over(db):
    t0 = datetime(2026, 10, 29, 0, 0, tzinfo=UTC)
    acquire_instance_lock(db, "a", now=t0)
    with pytest.raises(InstanceLockError):
        acquire_instance_lock(db, "b", now=t0 + timedelta(seconds=119))
    acquire_instance_lock(db, "b", now=t0 + timedelta(seconds=120))
    assert db.query_one("SELECT holder FROM instance_lock")["holder"] == "b"


def test_heartbeat_keeps_lock_alive_and_detects_loss(db):
    t0 = datetime(2026, 10, 29, 0, 0, tzinfo=UTC)
    acquire_instance_lock(db, "a", now=t0)
    assert heartbeat_instance_lock(db, "a", now=t0 + timedelta(seconds=100)) is True
    with pytest.raises(InstanceLockError):
        acquire_instance_lock(db, "b", now=t0 + timedelta(seconds=200))
    assert heartbeat_instance_lock(db, "b", now=t0) is False
    release_instance_lock(db, "a")
    assert heartbeat_instance_lock(db, "a") is False


def test_acquire_rejects_naive_now(db):
    with pytest.raises(ValueError):
        acquire_instance_lock(db, "a", now=datetime(2026, 1, 1))


def test_lock_between_two_connections_to_same_file(tmp_path):
    p = tmp_path / "d.db"
    d1 = Database.open(p)
    d2 = Database.open(p)
    try:
        acquire_instance_lock(d1, new_holder_id())
        with pytest.raises(InstanceLockError):
            acquire_instance_lock(d2, new_holder_id())
    finally:
        d1.close()
        d2.close()


def test_new_holder_id_unique():
    assert new_holder_id() != new_holder_id()


# ---- hardening: commit failure, backup failure, CRLF, future heartbeat ----------------------
class FailingCommitConn:
    """Proxy for sqlite3.Connection whose COMMIT raises (the transaction stays open)."""

    def __init__(self, real):
        self._real = real

    def execute(self, sql, *args):
        if sql.strip().upper() == "COMMIT":
            raise sqlite3.OperationalError("disk I/O error")
        return self._real.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_commit_failure_keeps_original_exception_and_rolls_back(db):
    real = db.connection
    db._conn = FailingCommitConn(real)
    try:
        with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
            with db.transaction() as conn:
                conn.execute("INSERT INTO settings VALUES (1,'k','v',?)", (NOW,))
        assert real.in_transaction is False
    finally:
        db._conn = real
    assert db.query("SELECT * FROM settings") == []
    with db.transaction() as conn:  # database is usable afterwards
        conn.execute("INSERT INTO settings VALUES (1,'k','v',?)", (NOW,))
    assert len(db.query("SELECT * FROM settings")) == 1


def test_backup_failure_raises_storage_error_and_applies_nothing(db, monkeypatch):
    from nomusyo.storage import migrations as mig
    from nomusyo.storage.errors import StorageError

    def boom(*a, **k):
        raise OSError("disk full /secret/path")

    monkeypatch.setattr(mig, "_backup", boom)
    with pytest.raises(StorageError) as ei:
        apply_migrations(db, [*load_migrations(), _extra()])
    assert "OSError" in str(ei.value) and "/secret/path" not in str(ei.value)
    assert "extra" not in tables(db)


def test_checksum_normalizes_crlf(tmp_path, monkeypatch):
    from nomusyo.storage import migrations as mig

    lf = tmp_path / "lf"
    crlf = tmp_path / "crlf"
    for d, nl in ((lf, "\n"), (crlf, "\r\n")):
        (d / "migrations").mkdir(parents=True)
        (d / "migrations" / "0001_init.sql").write_bytes(
            f"CREATE TABLE a (x INTEGER);{nl}-- c{nl}".encode()
        )
    sums = []
    for d in (lf, crlf):
        monkeypatch.setattr(mig.resources, "files", lambda _pkg, d=d: d)
        (m,) = load_migrations()
        sums.append(m.checksum)
        assert "\r" not in m.sql
    assert sums[0] == sums[1]


def test_future_heartbeat_beyond_tolerance_is_stale(db):
    t0 = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
    acquire_instance_lock(db, "a", now=t0)
    # clock rewound: holder's heartbeat is 10 s in the future -> takeover allowed
    acquire_instance_lock(db, "b", now=t0 - timedelta(seconds=10))
    assert db.query_one("SELECT holder FROM instance_lock")["holder"] == "b"


def test_slightly_future_heartbeat_within_tolerance_still_blocks(db):
    t0 = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
    acquire_instance_lock(db, "a", now=t0)
    with pytest.raises(InstanceLockError):
        acquire_instance_lock(db, "b", now=t0 - timedelta(seconds=4))


def test_heartbeat_rejects_naive_now(db):
    acquire_instance_lock(db, "a")
    with pytest.raises(ValueError):
        heartbeat_instance_lock(db, "a", now=datetime(2026, 1, 1))
