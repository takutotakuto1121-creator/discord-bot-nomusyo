"""V2-21: single-instance lock wired into startup (storage.lock, app.NomusyoBot, bot.__main__)."""

import asyncio
import logging
import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from discord.ext import commands

from nomusyo.bot import __main__ as bot_main
from nomusyo.bot import app as app_mod
from nomusyo.bot.app import NomusyoBot
from nomusyo.bot.config import BotConfig
from nomusyo.config import Config
from nomusyo.storage import migrations as mig
from nomusyo.storage.db import Database
from nomusyo.storage.errors import InstanceLockError
from nomusyo.storage.lock import (
    acquire_instance_lock,
    has_lock_table,
    open_database_with_lock,
)

TOKEN = "MTIzNDU2.SECRET-BOT-TOKEN.value"
URL = "https://hc-ping.example.com/SECRET-HEARTBEAT-UUID"


def versions(db):
    return [r["version"] for r in db.query("SELECT version FROM schema_version ORDER BY version")]


def holder_row(db):
    return db.query_one("SELECT holder, heartbeat_utc FROM instance_lock WHERE id = 1")


# -- open_database_with_lock -------------------------------------------------------------
def test_fresh_db_migrates_then_locks(tmp_path):
    path = str(tmp_path / "a.db")
    db = open_database_with_lock(path, "me")
    try:
        assert has_lock_table(db)
        assert versions(db) == [m.version for m in mig.load_migrations()]
        assert holder_row(db)["holder"] == "me"
    finally:
        db.close()


def test_fresh_db_order_is_migrate_then_acquire(tmp_path, monkeypatch):
    order = []
    real = mig.apply_migrations

    def spy_apply(db, *a, **k):
        order.append("migrate")
        return real(db, *a, **k)

    monkeypatch.setattr(mig, "apply_migrations", spy_apply)
    from nomusyo.storage import lock as lock_mod

    real_acq = lock_mod.acquire_instance_lock

    def spy_acq(*a, **k):
        order.append("lock")
        return real_acq(*a, **k)

    monkeypatch.setattr(lock_mod, "acquire_instance_lock", spy_acq)
    open_database_with_lock(str(tmp_path / "a.db"), "me").close()
    assert order == ["migrate", "lock"]


def test_existing_db_locks_before_migrating(tmp_path, monkeypatch):
    path = str(tmp_path / "a.db")
    first = Database.open(path, migrate=False)
    mig.apply_migrations(first, mig.load_migrations()[:1])  # leave 0002 pending
    first.close()

    order = []
    real = mig.apply_migrations
    monkeypatch.setattr(
        mig, "apply_migrations", lambda db, *a, **k: (order.append("migrate"), real(db, *a, **k))[1]
    )
    from nomusyo.storage import lock as lock_mod

    real_acq = lock_mod.acquire_instance_lock
    monkeypatch.setattr(
        lock_mod,
        "acquire_instance_lock",
        lambda *a, **k: (order.append("lock"), real_acq(*a, **k))[1],
    )
    db = open_database_with_lock(path, "me")
    try:
        assert order == ["lock", "migrate"]
        assert versions(db) == [1, 2]
    finally:
        db.close()


def test_held_lock_blocks_and_schema_not_advanced(tmp_path):
    path = str(tmp_path / "a.db")
    first = Database.open(path, migrate=False)
    mig.apply_migrations(first, mig.load_migrations()[:1])
    acquire_instance_lock(first, "other")
    first.close()

    closed = []
    real_close = Database.close
    orig = Database.close
    Database.close = lambda self: (closed.append(1), real_close(self))[1]  # type: ignore[method-assign]
    try:
        with pytest.raises(InstanceLockError):
            open_database_with_lock(path, "me")
    finally:
        Database.close = orig  # type: ignore[method-assign]
    assert closed == [1]

    check = Database.open(path, migrate=False)
    try:
        assert versions(check) == [1]  # schema never advanced without the lock
        assert holder_row(check)["holder"] == "other"
    finally:
        check.close()


def test_stale_lock_is_taken_over(tmp_path):
    path = str(tmp_path / "a.db")
    first = open_database_with_lock(path, "old")
    old = datetime.now(UTC) - timedelta(seconds=121)
    first.execute(
        "UPDATE instance_lock SET heartbeat_utc = ? WHERE id = 1",
        (old.strftime("%Y-%m-%dT%H:%M:%SZ"),),
    )
    first.close()
    db = open_database_with_lock(path, "new")
    try:
        assert holder_row(db)["holder"] == "new"
    finally:
        db.close()


def test_released_lock_is_taken_over(tmp_path):
    path = str(tmp_path / "a.db")
    first = open_database_with_lock(path, "old")
    first.execute("DELETE FROM instance_lock WHERE id = 1")
    first.close()
    db = open_database_with_lock(path, "new")
    try:
        assert holder_row(db)["holder"] == "new"
    finally:
        db.close()


def test_fresh_lock_is_not_taken_over(tmp_path):
    path = str(tmp_path / "a.db")
    first = open_database_with_lock(path, "old")
    try:
        with pytest.raises(InstanceLockError):
            open_database_with_lock(path, "new")
        assert holder_row(first)["holder"] == "old"
    finally:
        first.close()


def test_concurrent_apply_migrations_no_error_no_duplicates(tmp_path):
    path = str(tmp_path / "a.db")
    dbs = [Database.open(path, migrate=False) for _ in range(4)]
    results: list = []
    errors: list = []
    barrier = threading.Barrier(len(dbs))

    def run(db):
        barrier.wait()
        try:
            results.append(mig.apply_migrations(db))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(d,)) for d in dbs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    try:
        assert errors == []
        applied = sorted(v for r in results for v in r)
        expected = [m.version for m in mig.load_migrations()]
        assert applied == expected  # each migration reported done exactly once
        assert versions(dbs[0]) == expected
    finally:
        for d in dbs:
            d.close()


# -- NomusyoBot --------------------------------------------------------------------------
def make_bot(**over):
    kw = dict(
        discord_bot_token=TOKEN,
        guild_id=111,
        allowed_user_ids=frozenset({222}),
        notify_channel_id=333,
        admin_user_id=222,
        heartbeat_url=URL,
        news=Config(anthropic_api_key="sk-ant-x"),
    )
    kw.update(over)
    bot = NomusyoBot(BotConfig(**kw))
    return bot


def run_loop(bot, db, monkeypatch, hb):
    monkeypatch.setattr(app_mod, "LOCK_HEARTBEAT_SECONDS", 0)
    monkeypatch.setattr(app_mod, "heartbeat_instance_lock", hb)
    monkeypatch.setattr(commands.Bot, "close", AsyncMock())
    bot.db = db
    bot._lock_held = True

    async def go():
        await asyncio.wait_for(bot._lock_heartbeat_loop(), 5)

    asyncio.run(go())


def test_heartbeat_updates_row_while_held(tmp_path, monkeypatch):
    db = open_database_with_lock(str(tmp_path / "a.db"), "me")
    old = "2000-01-01T00:00:00Z"
    db.execute("UPDATE instance_lock SET heartbeat_utc = ?", (old,))
    bot = make_bot()
    bot.lock_holder = "me"
    calls = []
    stop = []
    monkeypatch.setattr(bot, "is_closed", lambda: bool(stop))
    real = app_mod.heartbeat_instance_lock

    def hb(d, h):
        calls.append(1)
        ok = real(d, h)
        stop.append(1)
        return ok

    run_loop(bot, db, monkeypatch, hb)
    try:
        assert calls == [1]
        assert holder_row(db)["heartbeat_utc"] != old
        assert bot.lock_lost is False
    finally:
        db.close()


def test_heartbeat_false_sets_lock_lost_and_closes(monkeypatch):
    bot = make_bot()
    db = MagicMock()
    run_loop(bot, db, monkeypatch, lambda d, h: False)
    assert bot.lock_lost is True
    assert bot._lock_held is False
    commands.Bot.close.assert_awaited()  # type: ignore[attr-defined]


def test_three_consecutive_failures_lose_lock(monkeypatch, caplog):
    bot = make_bot()
    calls = []

    def hb(d, h):
        calls.append(1)
        raise RuntimeError(f"boom {TOKEN} {URL}")

    with caplog.at_level(logging.DEBUG):
        run_loop(bot, MagicMock(), monkeypatch, hb)
    assert len(calls) == app_mod.LOCK_HEARTBEAT_MAX_FAILURES == 3
    assert bot.lock_lost is True
    assert TOKEN not in caplog.text and URL not in caplog.text
    assert "RuntimeError" in caplog.text


def test_failures_reset_on_success(monkeypatch):
    bot = make_bot()
    seq = iter([RuntimeError, RuntimeError, True, RuntimeError, RuntimeError, True, False])

    def hb(d, h):
        item = next(seq)
        if item is RuntimeError:
            raise RuntimeError("x")
        return item

    run_loop(bot, MagicMock(), monkeypatch, hb)
    assert bot.lock_lost is True
    assert next(seq, "done") == "done"  # consumed all: never lost before the explicit False


def test_close_cancels_heartbeat_task_and_orders_steps(monkeypatch):
    bot = make_bot()
    order = []
    db = MagicMock()
    db.close.side_effect = lambda: order.append("db.close")

    async def fake_super(self):
        order.append("super")

    monkeypatch.setattr(commands.Bot, "close", fake_super)
    monkeypatch.setattr(app_mod, "release_instance_lock", lambda d, h: order.append(("release", h)))

    async def go():
        bot.db = db
        bot._lock_held = True
        bot._lock_task = asyncio.create_task(asyncio.sleep(100))
        task = bot._lock_task
        await bot.close()
        await bot.close()
        await asyncio.sleep(0)
        assert task.cancelled()

    asyncio.run(go())
    assert order == ["super", ("release", bot.lock_holder), "db.close", "super"]
    # second close(): super again, but release/db.close happen only once
    assert bot.db is None


def test_close_after_lock_lost_does_not_release(monkeypatch):
    bot = make_bot()
    db = MagicMock()
    release = MagicMock()
    monkeypatch.setattr(commands.Bot, "close", AsyncMock())
    monkeypatch.setattr(app_mod, "release_instance_lock", release)
    bot.db = db
    bot._lock_held = False
    asyncio.run(bot.close())
    release.assert_not_called()
    db.close.assert_called_once()


def test_setup_hook_instance_lock_error_logged_and_raised(monkeypatch, tmp_path, caplog):
    bot = make_bot(db_path=tmp_path / "bot.db")

    def locked(path, holder):
        raise InstanceLockError("another")

    monkeypatch.setattr(app_mod, "open_database_with_lock", locked)
    with caplog.at_level(logging.DEBUG), pytest.raises(InstanceLockError):
        asyncio.run(bot.setup_hook())
    assert bot.db is None
    assert bot._lock_task is None
    assert "InstanceLockError" in caplog.text
    assert TOKEN not in caplog.text


def test_setup_hook_passes_holder_and_starts_heartbeat(monkeypatch, tmp_path):
    bot = make_bot(db_path=tmp_path / "bot.db")
    monkeypatch.setattr(bot.tree, "sync", AsyncMock(return_value=[]))
    seen = {}
    db = MagicMock()

    def opener(path, holder):
        seen["args"] = (path, holder)
        return db

    monkeypatch.setattr(app_mod, "open_database_with_lock", opener)
    monkeypatch.setattr(commands.Bot, "close", AsyncMock())
    monkeypatch.setattr(app_mod, "release_instance_lock", MagicMock())

    async def go():
        await bot.setup_hook()
        assert bot._lock_task is not None and not bot._lock_task.done()
        assert bot._lock_held is True
        for name in list(bot.extensions):
            await bot.unload_extension(name)
        await bot.close()

    asyncio.run(go())
    assert seen["args"][1] == bot.lock_holder


# -- main() exit codes ---------------------------------------------------------------------
def patch_main(monkeypatch, run=None, lock_lost=False):
    cfg = MagicMock()
    cfg.log_level = "INFO"
    cfg.discord_bot_token = TOKEN
    monkeypatch.setattr(bot_main, "load_bot_config", lambda: cfg)
    monkeypatch.setattr(bot_main, "configure_logging", lambda *a, **k: None)
    monkeypatch.setattr(bot_main, "collect_secrets", lambda c: ())
    fake = MagicMock()
    fake.lock_lost = lock_lost
    fake.run.side_effect = run
    monkeypatch.setattr(bot_main, "NomusyoBot", lambda c: fake)
    return fake


def test_main_returns_0_on_clean_exit(monkeypatch):
    patch_main(monkeypatch)
    assert bot_main.main() == 0


def test_main_returns_1_when_lock_lost(monkeypatch, capsys):
    patch_main(monkeypatch, lock_lost=True)
    assert bot_main.main() == 1
    assert "lost" in capsys.readouterr().err


def test_main_returns_1_on_instance_lock_error(monkeypatch, capsys):
    def run(*a, **k):
        raise InstanceLockError("held")

    patch_main(monkeypatch, run=run)
    assert bot_main.main() == 1
    assert "already running" in capsys.readouterr().err


def test_main_unexpected_error_does_not_print_secret(monkeypatch, capsys):
    def run(*a, **k):
        raise OSError(f"leak {TOKEN}")

    patch_main(monkeypatch, run=run)
    assert bot_main.main() == 1
    captured = capsys.readouterr()
    assert TOKEN not in captured.err + captured.out
    assert "OSError" in captured.err
