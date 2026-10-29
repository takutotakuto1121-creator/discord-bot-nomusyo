"""Discord webhook sender with timeout and retry.

The webhook URL is a secret: it is never included in exceptions or logs.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10.0
MAX_RETRIES = 3
MAX_RETRY_AFTER = 60.0


class WebhookError(Exception):
    """Webhook delivery failed. The message never contains the webhook URL."""


def _retry_after(resp: requests.Response) -> float:
    delay: float | None = None
    try:
        delay = float(resp.json().get("retry_after"))
    except (ValueError, TypeError, AttributeError):
        delay = None
    if delay is None:
        try:
            delay = float(resp.headers.get("Retry-After", ""))
        except ValueError:
            delay = 1.0
    return min(max(delay, 0.0), MAX_RETRY_AFTER)


def post_webhook(
    url: str,
    payload: dict[str, Any],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    max_retries: int = MAX_RETRIES,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """POST ``payload`` to the webhook. Raises WebhookError on failure.

    429 waits for Retry-After and retries; 5xx and connection errors use exponential
    backoff (a read timeout is never retried, to avoid duplicate posts); other 4xx fail
    immediately. ``allowed_mentions`` is forced to disable all mentions unless the caller set it.
    """
    body = {"allowed_mentions": {"parse": []}, **payload}
    attempt = 0
    while True:
        status: int | None = None
        delay = 0.0
        try:
            resp = requests.post(url, params={"wait": "true"}, json=body, timeout=timeout)
            status = resp.status_code
            if 200 <= status < 300:
                return
            if status == 429:
                delay = _retry_after(resp)
            elif status >= 500:
                delay = float(2**attempt)
            else:
                raise WebhookError(f"Discord webhook rejected the request (HTTP {status})")
        except requests.ReadTimeout:
            # The request may have been delivered; retrying could post twice.
            raise WebhookError("Discord webhook read timed out (not retried)") from None
        except requests.RequestException as exc:
            # Do not chain: the exception text may contain the webhook URL.
            err = type(exc).__name__
            if attempt >= max_retries:
                raise WebhookError(f"Discord webhook request failed ({err})") from None
            delay = float(2**attempt)
            status = None
        if attempt >= max_retries:
            raise WebhookError(f"Discord webhook failed after retries (HTTP {status})")
        attempt += 1
        logger.warning(
            "Webhook retry %d/%d in %.1fs (status=%s)", attempt, max_retries, delay, status
        )
        sleep(delay)


def send_message(url: str, content: str, **kwargs: Any) -> None:
    """Send a plain text message."""
    post_webhook(url, {"content": content}, **kwargs)
