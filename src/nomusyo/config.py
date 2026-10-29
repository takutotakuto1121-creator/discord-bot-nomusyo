"""Environment-variable based configuration.

Secret values are never included in repr/str or error messages.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

DEFAULT_MODEL = "claude-haiku-5-5"


class ConfigError(Exception):
    """Raised when configuration is missing or invalid (never contains secret values)."""


def _parse_log_level(raw: str) -> str:
    """Allow-list DEBUG/INFO/WARNING/ERROR; anything else falls back to INFO."""
    level = raw.strip().upper()
    return level if level in ("DEBUG", "INFO", "WARNING", "ERROR") else "INFO"


def _parse_bool(name: str, raw: str) -> bool:
    v = raw.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("", "0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be a boolean (true/false)")


def _parse_int(name: str, raw: str, *, minimum: int = 0) -> int:
    try:
        n = int(raw.strip())
    except ValueError:
        raise ConfigError(f"{name} must be an integer") from None
    if n < minimum:
        raise ConfigError(f"{name} must be >= {minimum}")
    return n


def _parse_ratio(name: str, raw: str) -> float:
    try:
        r = float(raw.strip())
    except ValueError:
        raise ConfigError(f"{name} must be a number between 0 and 1") from None
    if not 0.0 <= r <= 1.0:
        raise ConfigError(f"{name} must be between 0 and 1")
    return r


@dataclass(frozen=True)
class Config:
    discord_webhook_url: str = field(default="", repr=False)
    anthropic_api_key: str = field(default="", repr=False)
    summary_model: str = DEFAULT_MODEL
    news_total_count: int = 10
    news_general_ratio: float = 0.5
    news_max_age_hours: int = 36
    dry_run: bool = False
    log_level: str = "INFO"


def load_config(
    env: Mapping[str, str] | None = None,
    *,
    require_anthropic: bool = True,
    dry_run_override: bool = False,
) -> Config:
    """Load configuration from ``env`` (defaults to ``os.environ``).

    ``dry_run_override`` forces dry-run (e.g. the ``--dry-run`` flag).
    Secrets are required unless DRY_RUN is true (the webhook URL is then not needed;
    the Anthropic key is still required when ``require_anthropic`` is true and
    DRY_RUN is false).
    """
    e = os.environ if env is None else env

    dry_run = dry_run_override or _parse_bool("DRY_RUN", e.get("DRY_RUN", "false"))
    webhook = e.get("DISCORD_WEBHOOK_URL", "").strip()
    api_key = e.get("ANTHROPIC_API_KEY", "").strip()

    missing = []
    if not dry_run:
        if not webhook:
            missing.append("DISCORD_WEBHOOK_URL")
        if require_anthropic and not api_key:
            missing.append("ANTHROPIC_API_KEY")
    if missing:
        raise ConfigError("Missing required environment variable(s): " + ", ".join(missing))

    return Config(
        discord_webhook_url=webhook,
        anthropic_api_key=api_key,
        summary_model=e.get("SUMMARY_MODEL", "").strip() or DEFAULT_MODEL,
        news_total_count=_parse_int("NEWS_TOTAL_COUNT", e.get("NEWS_TOTAL_COUNT", "10"), minimum=1),
        news_general_ratio=_parse_ratio("NEWS_GENERAL_RATIO", e.get("NEWS_GENERAL_RATIO", "0.5")),
        news_max_age_hours=_parse_int(
            "NEWS_MAX_AGE_HOURS", e.get("NEWS_MAX_AGE_HOURS", "36"), minimum=1
        ),
        dry_run=dry_run,
        log_level=_parse_log_level(e.get("LOG_LEVEL", "")),
    )
