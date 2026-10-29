"""Morning digest decisions (Discord-independent, pure where possible).

Timing: a digest scheduled at ``digest_time`` (local, ``tz``) is "wait" before that time,
"due" until ``DIGEST_GRACE`` after it, and "expired" afterwards (skipped and recorded).
If the grace window of yesterday's digest still reaches into the current local day
(e.g. 22:00 + 3h), yesterday's run is "due" until it ends.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Literal

from nomusyo.schedule import parsing
from nomusyo.schedule.errors import ParseError
from nomusyo.schedule.models import get_zone
from nomusyo.storage.db import Database

JOB_DAILY_DIGEST = "daily_digest"
DIGEST_GRACE = timedelta(hours=3)

KEY_DIGEST_TIME = "digest_time"
KEY_SKIP_EMPTY = "digest_skip_empty"
KEY_NOTIFY_CHANNEL = "notify_channel_id"

DigestState = Literal["wait", "due", "expired"]


@dataclass(frozen=True)
class DigestTiming:
    run_date: str  # local date, the idempotency key
    scheduled: datetime
    state: DigestState


def digest_timing(
    now: datetime, digest_time: time, tz: str, grace: timedelta = DIGEST_GRACE
) -> DigestTiming:
    zone = get_zone(tz)
    local = now.astimezone(zone)
    scheduled = datetime.combine(local.date(), digest_time.replace(tzinfo=None), tzinfo=zone)
    if local < scheduled:
        prev = scheduled - timedelta(days=1)
        # wall-clock difference (same tz): the window may cross midnight for late digest times
        if local - prev <= grace:
            return DigestTiming(prev.date().isoformat(), prev, "due")
        state: DigestState = "wait"
    elif local - scheduled <= grace:
        state = "due"
    else:
        state = "expired"
    return DigestTiming(local.date().isoformat(), scheduled, state)


def should_skip_empty(is_empty: bool, skip_empty: bool) -> bool:
    return is_empty and skip_empty


def parse_hhmm(raw: str | None) -> time | None:
    if raw is None:
        return None
    if not raw.isascii():  # stored values are ASCII; no NFKC folding here
        return None
    try:  # shared with the command parser: H:MM or HH:MM (minutes need two digits)
        return parsing.parse_hhmm(raw)
    except ParseError:
        return None


def parse_bool(raw: str | None, default: bool) -> bool:
    if raw is None:
        return default
    v = raw.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default


@dataclass(frozen=True)
class DigestSettings:
    digest_time: time
    skip_empty: bool
    channel_id: int


def load_digest_settings(
    db: Database, owner_id: int, *, default_time: time, default_channel_id: int
) -> DigestSettings:
    """settings > env defaults. Invalid stored values fall back to the defaults."""
    from nomusyo.schedule.jobs import get_setting

    t = parse_hhmm(get_setting(db, owner_id, KEY_DIGEST_TIME)) or default_time
    skip = parse_bool(get_setting(db, owner_id, KEY_SKIP_EMPTY), False)
    raw = get_setting(db, owner_id, KEY_NOTIFY_CHANNEL)
    channel = int(raw) if raw and raw.strip().isascii() and raw.strip().isdigit() else 0
    return DigestSettings(t, skip, channel if channel > 0 else default_channel_id)
