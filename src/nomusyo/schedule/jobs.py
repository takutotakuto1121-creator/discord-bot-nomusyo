"""Discord-independent helpers for the ``job_runs`` and ``settings`` tables.

``job_runs (job, run_date)`` is the idempotency key for periodic jobs: only the process whose
``INSERT OR IGNORE`` succeeds (or that re-acquires a retryable row) runs the job.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from nomusyo.schedule.models import to_utc_str
from nomusyo.storage.db import Database

JOB_MAX_ATTEMPTS = 3
JOB_STALE_AFTER = timedelta(hours=1)  # a 'started' row older than this is a crash trace
JOB_RETRY_BACKOFF = timedelta(minutes=5)  # a failed row is retried after backoff * attempts

STATUS_STARTED = "started"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"


def claim_job(
    db: Database,
    job: str,
    run_date: str,
    now: datetime,
    *,
    max_attempts: int = JOB_MAX_ATTEMPTS,
    stale_after: timedelta = JOB_STALE_AFTER,
    backoff: timedelta = JOB_RETRY_BACKOFF,
) -> bool:
    """Try to take the right to run ``job`` for ``run_date``. True means the caller must run it.

    A ``failed`` row is re-claimed after ``backoff * attempts``; a ``started`` row (crash trace)
    after ``stale_after``. Both are capped by ``max_attempts``. Callers whose job may have had a
    visible side effect (posting) should pass a ``stale_after`` longer than their run window.
    The caller keeps ``now`` and passes it to :func:`finish_job` as ``claimed_at``.
    """
    now_s = to_utc_str(now)
    cur = db.execute(
        "INSERT OR IGNORE INTO job_runs (job, run_date, status, attempts, started_at_utc) "
        "VALUES (?, ?, 'started', 1, ?)",
        (job, run_date, now_s),
    )
    if cur.rowcount == 1:
        return True
    stale = to_utc_str(now - stale_after)
    # Backoff depends on attempts, so evaluate it per row in Python and then update guarded by
    # the observed (status, attempts, started_at_utc) so concurrent claimers cannot both win.
    row = db.query_one(
        "SELECT status, attempts, started_at_utc, finished_at_utc FROM job_runs "
        "WHERE job=? AND run_date=?",
        (job, run_date),
    )
    if row is None or row["attempts"] >= max_attempts:
        return False
    if row["status"] == STATUS_FAILED:
        finished = row["finished_at_utc"]
        if finished is not None and finished >= to_utc_str(now - backoff * row["attempts"]):
            return False
    elif row["status"] == STATUS_STARTED:
        if row["started_at_utc"] >= stale:
            return False
    else:
        return False
    cur = db.execute(
        "UPDATE job_runs SET status='started', attempts=attempts+1, started_at_utc=?, "
        "finished_at_utc=NULL WHERE job=? AND run_date=? AND status=? AND attempts=? "
        "AND started_at_utc=?",
        (now_s, job, run_date, row["status"], row["attempts"], row["started_at_utc"]),
    )
    return cur.rowcount == 1


def finish_job(
    db: Database,
    job: str,
    run_date: str,
    status: str,
    now: datetime,
    detail: str | None = None,
    *,
    claimed_at: datetime | None = None,
) -> bool:
    """Record the outcome. ``detail`` must be a short code (no personal data, no message text).

    With ``claimed_at`` (the ``now`` given to :func:`claim_job`) the row is updated only if this
    caller still owns the claim. Returns True if a row was updated.
    """
    sql = "UPDATE job_runs SET status=?, detail=?, finished_at_utc=? WHERE job=? AND run_date=?"
    params: list = [status, detail, to_utc_str(now), job, run_date]
    if claimed_at is not None:
        sql += " AND started_at_utc=?"
        params.append(to_utc_str(claimed_at))
    return db.execute(sql, params).rowcount == 1


def job_attempts(db: Database, job: str, run_date: str) -> int:
    row = db.query_one("SELECT attempts FROM job_runs WHERE job=? AND run_date=?", (job, run_date))
    return 0 if row is None else int(row["attempts"])


def record_skipped(db: Database, job: str, run_date: str, now: datetime, detail: str) -> bool:
    """Insert a ``skipped`` row. True only for the process that created it (notify once)."""
    now_s = to_utc_str(now)
    cur = db.execute(
        "INSERT OR IGNORE INTO job_runs "
        "(job, run_date, status, attempts, detail, started_at_utc, finished_at_utc) "
        "VALUES (?, ?, 'skipped', 0, ?, ?, ?)",
        (job, run_date, detail, now_s, now_s),
    )
    return cur.rowcount == 1


def job_status(db: Database, job: str, run_date: str) -> str | None:
    row = db.query_one("SELECT status FROM job_runs WHERE job=? AND run_date=?", (job, run_date))
    return None if row is None else row["status"]


def get_setting(db: Database, owner_id: int, key: str) -> str | None:
    row = db.query_one("SELECT value FROM settings WHERE owner_id=? AND key=?", (owner_id, key))
    return None if row is None else row["value"]
