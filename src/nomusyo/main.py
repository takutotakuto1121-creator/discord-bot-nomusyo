"""Entry point: fetch news, summarize, format and post to a Discord webhook."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections.abc import Sequence

from nomusyo.config import ConfigError, load_config
from nomusyo.core.pipeline import NoArticlesError, run_pipeline
from nomusyo.discord_io.formatter import format_payloads
from nomusyo.discord_io.webhook import WebhookError, post_webhook

# Short pause between split messages to stay clear of webhook rate limits.
INTER_MESSAGE_DELAY = 1.0
_QUIET_LOGGERS = ("urllib3", "httpx", "httpcore", "anthropic", "requests")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nomusyo")
    parser.add_argument("--dry-run", action="store_true", help="print instead of posting")
    args = parser.parse_args(argv)

    try:
        config = load_config(dry_run_override=args.dry_run)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Keep HTTP libraries quiet even with LOG_LEVEL=DEBUG: they log request URLs
    # (which include the webhook secret).
    for noisy in _QUIET_LOGGERS:
        logging.getLogger(noisy).setLevel(logging.WARNING)
    log = logging.getLogger("nomusyo")
    dry_run = config.dry_run

    try:
        articles = run_pipeline(config)
    except NoArticlesError as exc:
        log.error("%s", exc)
        return 1

    payloads = format_payloads(articles)
    # Set by the summarizer on permanent API errors (bad key/model/request): post the
    # fallback digest anyway, then fail the run so the problem gets noticed.
    summary_error = getattr(articles, "summary_error", None)

    if dry_run:
        for payload in payloads:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        log.info("Dry run: %d message(s) not posted", len(payloads))
        return 1 if summary_error else 0

    try:
        for i, payload in enumerate(payloads):
            if i:
                time.sleep(INTER_MESSAGE_DELAY)
            post_webhook(config.discord_webhook_url, payload)
    except WebhookError as exc:
        log.error("Failed to post message %d/%d: %s", i + 1, len(payloads), exc)
        return 1
    log.info("Posted %d message(s)", len(payloads))
    if summary_error:
        log.error("Summarization failed permanently (%s); posted fallback digest", summary_error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
