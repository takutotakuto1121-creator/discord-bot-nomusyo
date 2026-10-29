"""RSS/Atom fetching. A failure in one source never affects the others."""

from __future__ import annotations

import calendar
import html
import logging
import re
import time
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

import feedparser
import requests

from nomusyo.core.models import Article, Source

log = logging.getLogger(__name__)

USER_AGENT = "NomusyoBot/0.1 (personal news digest; +https://github.com/)"
DEFAULT_TIMEOUT = 10.0
MAX_PER_SOURCE = 20
MAX_DESCRIPTION_CHARS = 500
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
CHUNK_BYTES = 64 * 1024
# Overall budget for fetching every source (sources are fetched serially).
FETCH_DEADLINE_SECONDS = 120.0

_TAG_RE = re.compile(r"<[^>]*>")
_WS_RE = re.compile(r"\s+")


def clean_text(raw: str, limit: int | None = None) -> str:
    """Strip HTML tags/entities, collapse whitespace, optionally truncate."""
    text = html.unescape(_TAG_RE.sub(" ", raw or ""))
    text = _WS_RE.sub(" ", text).strip()
    if limit is not None and len(text) > limit:
        text = text[:limit].rstrip()
    return text


def _entry_datetime(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            try:
                return datetime.fromtimestamp(calendar.timegm(st), tz=UTC)
            except (OverflowError, ValueError, OSError):
                return None
    return None


def parse_feed(content: bytes, source: Source) -> list[Article]:
    """Parse feed bytes into articles (no age filtering). Raises ValueError if unusable."""
    if not content or not content.strip():
        raise ValueError("empty response")
    parsed = feedparser.parse(content)
    if not parsed.entries and (parsed.bozo or not parsed.get("version")):
        raise ValueError("feed could not be parsed")
    articles: list[Article] = []
    for entry in parsed.entries:
        title = clean_text(entry.get("title", ""))
        link = (entry.get("link") or "").strip()
        if not title or not link.startswith(("http://", "https://")):
            continue
        description = clean_text(entry.get("summary", ""), MAX_DESCRIPTION_CHARS)
        if description.startswith("Article URL:"):  # hnrss: only links/points, no content
            description = ""
        articles.append(
            Article(
                title=title,
                link=link,
                description=description,
                source=source.name,
                genre=source.genre,
                published=_entry_datetime(entry),
            )
        )
    # Newest first (undated last; sort is stable), then cap per source.
    articles.sort(
        key=lambda a: a.published.timestamp() if a.published else float("-inf"), reverse=True
    )
    return articles[:MAX_PER_SOURCE]


def _read_limited(resp: Any, deadline: float | None) -> bytes:
    """Read the body in chunks, enforcing MAX_RESPONSE_BYTES and the overall deadline."""
    declared = resp.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
        raise ValueError("response too large")
    buf = bytearray()
    for chunk in resp.iter_content(chunk_size=CHUNK_BYTES):
        buf.extend(chunk)
        if len(buf) > MAX_RESPONSE_BYTES:
            raise ValueError("response too large")
        if deadline is not None and time.monotonic() > deadline:
            raise TimeoutError("fetch deadline exceeded")
    return bytes(buf)


def fetch_source(
    source: Source,
    *,
    max_age_hours: int,
    now: datetime | None = None,
    session: requests.Session | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    deadline: float | None = None,
) -> list[Article]:
    """Fetch one source. Raises on network/parse errors (including empty/non-feed responses,
    oversized bodies and redirects to non-https URLs). Articles older than the limit are
    dropped; articles without a date are kept. ``deadline`` is a ``time.monotonic()`` value."""
    getter = session.get if session is not None else requests.get
    resp = getter(source.url, timeout=timeout, headers={"User-Agent": USER_AGENT}, stream=True)
    try:
        resp.raise_for_status()
        if resp.history:  # redirected: every hop after the first request must be https
            hops = [r.url for r in resp.history[1:]] + [resp.url]
            if not all(str(u).lower().startswith("https://") for u in hops):
                raise ValueError("redirected to non-https URL")
        content = _read_limited(resp, deadline)
    finally:
        resp.close()
    cutoff = (now or datetime.now(UTC)) - timedelta(hours=max_age_hours)
    return [a for a in parse_feed(content, source) if a.published is None or a.published >= cutoff]


def fetch_all(
    sources: Iterable[Source],
    *,
    max_age_hours: int,
    now: datetime | None = None,
    session: requests.Session | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    total_timeout: float = FETCH_DEADLINE_SECONDS,
) -> list[Article]:
    """Fetch every source in order; failed sources are logged and skipped. Sources not
    started before ``total_timeout`` seconds have elapsed are skipped."""
    result: list[Article] = []
    deadline = time.monotonic() + total_timeout
    for source in sources:
        if time.monotonic() > deadline:
            log.warning("source %r skipped: fetch deadline exceeded", source.name)
            continue
        try:
            items = fetch_source(
                source,
                max_age_hours=max_age_hours,
                now=now,
                session=session,
                timeout=timeout,
                deadline=deadline,
            )
        except Exception as exc:  # isolate per-source failures
            log.warning("source %r failed: %s", source.name, type(exc).__name__)
            continue
        log.info("source %r: %d articles", source.name, len(items))
        result.extend(items)
    return result
