"""Bot configuration from environment variables.

Secret values (token, heartbeat URL, API keys) never appear in repr/str or error messages.
The existing ``Config`` is untouched; the news part is derived from it via ``load_config``.
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from nomusyo.config import Config, ConfigError, load_config

DEFAULT_DB_PATH = "/data/nomusyo.db"
DEFAULT_TZ = "Asia/Tokyo"
DEFAULT_DIGEST_TIME = "06:55"
DEFAULT_NEWS_POST_TIME = "07:00"


def _parse_snowflake(name: str, raw: str) -> int:
    try:
        n = int(raw.strip())
    except ValueError:
        raise ConfigError(f"{name} must be an integer ID") from None
    if n <= 0:
        raise ConfigError(f"{name} must be a positive integer ID")
    return n


def _parse_id_list(name: str, raw: str) -> frozenset[int]:
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    ids: list[int] = []
    for part in parts:
        try:
            n = int(part)
        except ValueError:
            raise ConfigError(f"{name} must be comma-separated integer IDs") from None
        if n <= 0:
            raise ConfigError(f"{name} must be comma-separated positive integer IDs")
        ids.append(n)
    return frozenset(ids)


def _parse_hhmm(name: str, raw: str) -> time:
    parts = raw.strip().split(":")
    if len(parts) != 2 or not all(p.isascii() and p.isdigit() for p in parts):
        raise ConfigError(f"{name} must be HH:MM (24h)")
    h, m = int(parts[0]), int(parts[1])
    if h > 23 or m > 59:
        raise ConfigError(f"{name} must be HH:MM (24h)")
    return time(h, m)


def _parse_bool(name: str, raw: str) -> bool:
    v = raw.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("", "0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be a boolean (true/false)")


def _parse_tz(name: str, raw: str) -> str:
    value = raw.strip()
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ConfigError(f"{name} must be a valid IANA time zone name") from None
    return value


@dataclass(frozen=True)
class BotConfig:
    discord_bot_token: str = field(repr=False)
    guild_id: int
    allowed_user_ids: frozenset[int]
    notify_channel_id: int
    admin_user_id: int
    heartbeat_url: str = field(default="", repr=False)
    news: Config = field(default_factory=Config)
    log_level: str = "INFO"
    db_path: Path = Path(DEFAULT_DB_PATH)
    tz_default: str = DEFAULT_TZ
    digest_time: time = time(6, 55)
    news_post_time: time = time(7, 0)
    news_post_enabled: bool = False
    admin_channel_id: int | None = None
    owner_user_id: int = 0  # first entry of ALLOWED_USER_IDS (single-user data owner)


def load_bot_config(env: Mapping[str, str] | None = None) -> BotConfig:
    """Load and validate the bot configuration. Raises ``ConfigError`` (no secret values)."""
    e = os.environ if env is None else env

    token = e.get("DISCORD_BOT_TOKEN", "").strip()
    guild_raw = e.get("DISCORD_GUILD_ID", "").strip()
    allowed_raw = e.get("ALLOWED_USER_IDS", "").strip()
    channel_raw = e.get("NOTIFY_CHANNEL_ID", "").strip()
    api_key = e.get("ANTHROPIC_API_KEY", "").strip()

    missing = [
        name
        for name, value in (
            ("DISCORD_BOT_TOKEN", token),
            ("DISCORD_GUILD_ID", guild_raw),
            ("ALLOWED_USER_IDS", allowed_raw),
            ("NOTIFY_CHANNEL_ID", channel_raw),
            ("ANTHROPIC_API_KEY", api_key),
        )
        if not value
    ]
    if missing:
        raise ConfigError("Missing required environment variable(s): " + ", ".join(missing))

    allowed = _parse_id_list("ALLOWED_USER_IDS", allowed_raw)
    if not allowed:
        raise ConfigError("ALLOWED_USER_IDS must contain at least one user ID")
    # Ordered first entry is the default admin; frozenset loses order, so re-parse in order.
    first_allowed = int(next(p for p in allowed_raw.split(",") if p.strip()))

    admin_raw = e.get("ADMIN_USER_ID", "").strip()
    admin = _parse_snowflake("ADMIN_USER_ID", admin_raw) if admin_raw else first_allowed

    heartbeat = e.get("BOT_HEARTBEAT_URL", "").strip()
    if heartbeat and not heartbeat.lower().startswith("https://"):
        raise ConfigError("BOT_HEARTBEAT_URL must be an https:// URL")

    # The webhook URL is not needed when posting through the bot. Reuse the existing loader in
    # dry-run mode (which does not require it), then clear the flag and the webhook value.
    base = load_config(e, require_anthropic=True, dry_run_override=True)
    news = dataclasses.replace(base, discord_webhook_url="", dry_run=False)

    db_path_raw = e.get("DB_PATH", "").strip() or DEFAULT_DB_PATH
    tz_default = _parse_tz("TZ_DEFAULT", e.get("TZ_DEFAULT", "").strip() or DEFAULT_TZ)
    digest_time = _parse_hhmm(
        "DIGEST_TIME", e.get("DIGEST_TIME", "").strip() or DEFAULT_DIGEST_TIME
    )
    news_post_time = _parse_hhmm(
        "NEWS_POST_TIME", e.get("NEWS_POST_TIME", "").strip() or DEFAULT_NEWS_POST_TIME
    )
    news_post_enabled = _parse_bool("NEWS_POST_ENABLED", e.get("NEWS_POST_ENABLED", ""))
    admin_channel_raw = e.get("ADMIN_CHANNEL_ID", "").strip()
    admin_channel = (
        _parse_snowflake("ADMIN_CHANNEL_ID", admin_channel_raw) if admin_channel_raw else None
    )

    return BotConfig(
        discord_bot_token=token,
        guild_id=_parse_snowflake("DISCORD_GUILD_ID", guild_raw),
        allowed_user_ids=allowed,
        notify_channel_id=_parse_snowflake("NOTIFY_CHANNEL_ID", channel_raw),
        admin_user_id=admin,
        heartbeat_url=heartbeat,
        news=news,
        log_level=news.log_level,
        db_path=Path(db_path_raw),
        tz_default=tz_default,
        digest_time=digest_time,
        news_post_time=news_post_time,
        news_post_enabled=news_post_enabled,
        admin_channel_id=admin_channel,
        owner_user_id=first_allowed,
    )
