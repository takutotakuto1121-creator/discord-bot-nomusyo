"""User settings stored in the ``settings`` table (Discord-independent).

Priority: stored row > env (``BotConfig``) > code default. Keys are a whitelist; every write is
validated first, and an invalid value never reaches the database. Reads of the digest keys are
shared with ``nomusyo.schedule.digest`` (same key constants and ``get_setting``).
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime, time

from nomusyo.schedule.digest import (
    KEY_DIGEST_TIME,
    KEY_NOTIFY_CHANNEL,
    KEY_SKIP_EMPTY,
    load_digest_settings,
    parse_bool,
    parse_hhmm,
)
from nomusyo.schedule.errors import ScheduleError
from nomusyo.schedule.models import to_utc_str
from nomusyo.schedule.parsing import ParseError, parse_reminders
from nomusyo.storage.db import Database

KEY_DEFAULT_REMIND = "default_remind"
KEY_DEFAULT_REMIND_TODO = "default_remind_todo"
KEY_NOTIFY_DM = "notify_dm"

EDITABLE_KEYS = (
    KEY_DIGEST_TIME,
    KEY_SKIP_EMPTY,
    KEY_DEFAULT_REMIND,
    KEY_DEFAULT_REMIND_TODO,
    KEY_NOTIFY_CHANNEL,
    KEY_NOTIFY_DM,
)
DEFAULT_REMIND = "10m"
DEFAULT_REMIND_TODO = ""
MAX_REMIND_TEXT = 100

MSG_BAD_TIME = "時刻は HH:MM(24時間、例 06:55)の形式で指定してください。"
MSG_BAD_CHANNEL = "チャンネルIDが不正です。"
MSG_BAD_KEY = "変更できない設定項目です。"
MSG_BAD_BOOL = "オン/オフで指定してください。"


def get_setting(db: Database, owner_id: int, key: str) -> str | None:
    """Raw stored value of ``key`` (``None`` if unset). Canonical home; ``jobs.get_setting`` is
    the legacy location."""
    row = db.query_one("SELECT value FROM settings WHERE owner_id=? AND key=?", (owner_id, key))
    return None if row is None else row["value"]


class SettingsError(ScheduleError):
    """Invalid setting key/value (message is safe to show the user)."""


def normalize_value(key: str, raw: str) -> str:
    """Validate ``raw`` for ``key`` and return the canonical stored text. Raises SettingsError."""
    if key not in EDITABLE_KEYS:
        raise SettingsError(MSG_BAD_KEY)
    text = unicodedata.normalize("NFKC", raw).strip()
    if key == KEY_DIGEST_TIME:
        t = parse_hhmm(text)
        if t is None:
            raise SettingsError(MSG_BAD_TIME)
        return f"{t.hour:02d}:{t.minute:02d}"
    if key in (KEY_SKIP_EMPTY, KEY_NOTIFY_DM):
        low = text.lower()
        if low in ("1", "true", "yes", "on"):
            return "true"
        if low in ("0", "false", "no", "off"):
            return "false"
        raise SettingsError(MSG_BAD_BOOL)
    if key in (KEY_DEFAULT_REMIND, KEY_DEFAULT_REMIND_TODO):
        if len(text) > MAX_REMIND_TEXT:
            raise SettingsError("リマインドの指定が長すぎます。")
        try:
            specs = parse_reminders(text)
        except ParseError as exc:
            raise SettingsError(str(exc)) from None
        return text.lower() if specs else ""
    # KEY_NOTIFY_CHANNEL
    if not (text.isascii() and text.isdigit()) or int(text) <= 0 or len(text) > 20:
        raise SettingsError(MSG_BAD_CHANNEL)
    return str(int(text))


def set_setting(db: Database, owner_id: int, key: str, raw: str, now: datetime) -> str:
    """Validate and upsert. Returns the stored canonical value. Invalid input writes nothing."""
    value = normalize_value(key, raw)
    db.execute(
        "INSERT INTO settings (owner_id, key, value, updated_at_utc) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(owner_id, key) DO UPDATE SET value=excluded.value, "
        "updated_at_utc=excluded.updated_at_utc",
        (owner_id, key, value, to_utc_str(now)),
    )
    return value


@dataclass(frozen=True)
class UserSettings:
    digest_time: time
    skip_empty: bool
    default_remind: str
    default_remind_todo: str
    channel_id: int
    notify_dm: bool


def load_user_settings(
    db: Database, owner_id: int, *, default_time: time, default_channel_id: int
) -> UserSettings:
    d = load_digest_settings(
        db, owner_id, default_time=default_time, default_channel_id=default_channel_id
    )
    remind = get_setting(db, owner_id, KEY_DEFAULT_REMIND)
    remind_todo = get_setting(db, owner_id, KEY_DEFAULT_REMIND_TODO)
    return UserSettings(
        digest_time=d.digest_time,
        skip_empty=d.skip_empty,
        default_remind=DEFAULT_REMIND if remind is None else remind,
        default_remind_todo=DEFAULT_REMIND_TODO if remind_todo is None else remind_todo,
        channel_id=d.channel_id,
        notify_dm=parse_bool(get_setting(db, owner_id, KEY_NOTIFY_DM), False),
    )
