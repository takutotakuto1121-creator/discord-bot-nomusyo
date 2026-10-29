"""``python -m nomusyo.bot``: run the resident bot."""

from __future__ import annotations

import sys

import discord

from nomusyo.bot.app import (
    NomusyoBot,
    collect_secrets,
    configure_logging,
    describe_error,
)
from nomusyo.bot.config import load_bot_config
from nomusyo.config import ConfigError
from nomusyo.storage.errors import InstanceLockError


def main() -> int:
    try:
        config = load_bot_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    configure_logging(config.log_level, collect_secrets(config))
    bot = NomusyoBot(config)
    try:
        # log_handler=None: logging is configured above (discord's default handler is skipped).
        bot.run(config.discord_bot_token, log_handler=None)
    except InstanceLockError:
        print("Another instance is already running (instance lock held); exiting", file=sys.stderr)
        return 1
    except discord.LoginFailure:
        print("Login failed: check DISCORD_BOT_TOKEN", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - report type only, never the message
        print(f"Bot stopped with an error ({describe_error(exc)})", file=sys.stderr)
        return 1
    if bot.lock_lost:
        print("Instance lock was lost; exited", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
