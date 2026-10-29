from datetime import UTC, datetime, timedelta

import pytest

from nomusyo.schedule.jobs import (
    JOB_MAX_ATTEMPTS,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_SKIPPED,
    claim_job,
    finish_job,
    get_setting,
    job_attempts,
    job_status,
    record_skipped,
)
from nomusyo.storage import Database

NOW = datetime(2026, 10, 29, 6, 55, tzinfo=UTC)
JOB, DAY = "daily_digest", "2026-10-29"


@pytest.fixture
def db(tmp_path):
    d = Database.open(tmp_path / "t.db")
    yield d
    d.close()


def row(db, day=DAY):
    return db.query_one("SELECT * FROM job_runs WHERE job=? AND run_date=?", (JOB, day))


def test_first_claim_true_second_false(db):
    assert claim_job(db, JOB, DAY, NOW) is True
    assert job_status(db, JOB, DAY) == "started"
    assert claim_job(db, JOB, DAY, NOW + timedelta(seconds=30)) is False
    assert row(db)["attempts"] == 1


def test_other_date_and_job_are_independent(db):
    assert claim_job(db, JOB, DAY, NOW)
    assert claim_job(db, JOB, "2026-10-30", NOW)
    assert claim_job(db, "other", DAY, NOW)


def test_job_status_none_when_missing(db):
    assert job_status(db, JOB, DAY) is None


def test_failed_is_reclaimable_until_max_attempts(db):
    t = NOW
    for attempt in range(1, JOB_MAX_ATTEMPTS + 1):
        assert claim_job(db, JOB, DAY, t) is True
        assert row(db)["attempts"] == attempt
        assert row(db)["finished_at_utc"] is None
        assert finish_job(db, JOB, DAY, STATUS_FAILED, t, "HTTPException") is True
        t += timedelta(minutes=5 * attempt, seconds=1)
    assert job_status(db, JOB, DAY) == STATUS_FAILED
    assert claim_job(db, JOB, DAY, t + timedelta(days=1)) is False  # 3 attempts used
    assert row(db)["attempts"] == JOB_MAX_ATTEMPTS
    assert job_attempts(db, JOB, DAY) == JOB_MAX_ATTEMPTS


@pytest.mark.parametrize("attempts", [1, 2])
def test_failed_backoff_boundary(db, attempts):
    t = NOW
    for _ in range(attempts):
        assert claim_job(db, JOB, DAY, t)
        finish_job(db, JOB, DAY, STATUS_FAILED, t, "x")
        if _ < attempts - 1:
            t += timedelta(minutes=30)
    wait = timedelta(minutes=5 * attempts)
    assert not claim_job(db, JOB, DAY, t + wait - timedelta(seconds=1))
    assert not claim_job(db, JOB, DAY, t + wait)  # boundary: not strictly older
    assert claim_job(db, JOB, DAY, t + wait + timedelta(seconds=1))
    assert row(db)["attempts"] == attempts + 1


def test_custom_backoff(db):
    claim_job(db, JOB, DAY, NOW)
    finish_job(db, JOB, DAY, STATUS_FAILED, NOW, "x")
    assert claim_job(db, JOB, DAY, NOW + timedelta(seconds=2), backoff=timedelta(seconds=1))


def test_stale_started_respects_max_attempts(db):
    claim_job(db, JOB, DAY, NOW, max_attempts=1)
    assert not claim_job(db, JOB, DAY, NOW + timedelta(hours=5), max_attempts=1)


def test_custom_stale_after(db):
    claim_job(db, JOB, DAY, NOW)
    later = NOW + timedelta(hours=2)
    assert not claim_job(db, JOB, DAY, later, stale_after=timedelta(hours=3))
    assert claim_job(db, JOB, DAY, later, stale_after=timedelta(hours=1))


def test_finish_job_owner_check(db):
    claim_job(db, JOB, DAY, NOW)
    other = NOW + timedelta(seconds=7)
    assert finish_job(db, JOB, DAY, STATUS_DONE, other, claimed_at=other) is False
    assert job_status(db, JOB, DAY) == "started"
    assert finish_job(db, JOB, DAY, STATUS_DONE, other, claimed_at=NOW) is True
    assert job_status(db, JOB, DAY) == "done"


def test_finish_job_returns_false_when_missing(db):
    assert finish_job(db, JOB, DAY, STATUS_DONE, NOW) is False


def test_old_owner_cannot_finish_after_reclaim(db):
    claim_job(db, JOB, DAY, NOW)
    later = NOW + timedelta(hours=2)
    assert claim_job(db, JOB, DAY, later)
    assert finish_job(db, JOB, DAY, STATUS_DONE, later, claimed_at=NOW) is False
    assert job_status(db, JOB, DAY) == "started"


def test_job_attempts_missing_is_zero(db):
    assert job_attempts(db, JOB, DAY) == 0


def test_custom_max_attempts(db):
    assert claim_job(db, JOB, DAY, NOW, max_attempts=1)
    finish_job(db, JOB, DAY, STATUS_FAILED, NOW, "x")
    assert not claim_job(db, JOB, DAY, NOW, max_attempts=1)


def test_started_within_hour_not_reclaimable(db):
    claim_job(db, JOB, DAY, NOW)
    assert not claim_job(db, JOB, DAY, NOW + timedelta(minutes=59, seconds=59))
    assert not claim_job(db, JOB, DAY, NOW + timedelta(hours=1))  # boundary: not strictly older


def test_stale_started_is_reclaimable(db):
    claim_job(db, JOB, DAY, NOW)
    later = NOW + timedelta(hours=1, seconds=1)
    assert claim_job(db, JOB, DAY, later) is True
    assert row(db)["attempts"] == 2
    assert not claim_job(db, JOB, DAY, later + timedelta(minutes=1))


@pytest.mark.parametrize("status", [STATUS_DONE, STATUS_SKIPPED])
def test_terminal_states_not_reclaimable(db, status):
    claim_job(db, JOB, DAY, NOW)
    finish_job(db, JOB, DAY, status, NOW)
    assert not claim_job(db, JOB, DAY, NOW + timedelta(days=1))
    assert job_status(db, JOB, DAY) == status


def test_finish_job_records_detail_and_time(db):
    claim_job(db, JOB, DAY, NOW)
    finish_job(db, JOB, DAY, STATUS_SKIPPED, NOW + timedelta(seconds=5), "empty")
    r = row(db)
    assert r["status"] == "skipped" and r["detail"] == "empty"
    assert r["finished_at_utc"] is not None


def test_record_skipped_once(db):
    assert record_skipped(db, JOB, DAY, NOW, "grace_exceeded") is True
    assert record_skipped(db, JOB, DAY, NOW, "grace_exceeded") is False
    r = row(db)
    assert (r["status"], r["detail"], r["attempts"]) == ("skipped", "grace_exceeded", 0)
    assert not claim_job(db, JOB, DAY, NOW)


def test_record_skipped_does_not_override_existing(db):
    claim_job(db, JOB, DAY, NOW)
    assert record_skipped(db, JOB, DAY, NOW, "grace_exceeded") is False
    assert job_status(db, JOB, DAY) == "started"


def test_get_setting(db):
    assert get_setting(db, 1, "k") is None
    db.execute("INSERT INTO settings (owner_id, key, value, updated_at_utc) VALUES (1,'k','v','x')")
    assert get_setting(db, 1, "k") == "v"
    assert get_setting(db, 2, "k") is None
