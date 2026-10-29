"""Build Discord webhook payloads from summarized articles (pure functions, no I/O).

Limits below are deliberately set on the safe side of Discord's documented limits
(embeds per message 10, embed total 6000 chars, title 256, description 4096,
footer 2048, content 2000, embed url 2048).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from nomusyo.core.models import SummarizedArticle

# Discord: max 10 embeds per message.
MAX_EMBEDS_PER_MESSAGE = 10
# Discord: sum of all embed text per message <= 6000; keep a margin.
MAX_EMBED_CHARS_PER_MESSAGE = 5500
# Discord: title <= 256.
MAX_TITLE = 250
# Discord: description <= 4096; summaries are ~120 chars so a small cap is plenty.
MAX_DESCRIPTION = 1000
# Discord: footer text <= 2048.
MAX_FOOTER = 100
# Discord: content <= 2000.
MAX_CONTENT = 1900
# Discord: embed url <= 2048.
MAX_URL = 2000

COLORS = {"general": 0x3498DB, "tech": 0x2ECC71}
DEFAULT_COLOR = 0x95A5A6
GENRE_LABELS = {"general": "総合", "tech": "テック"}
FALLBACK_NOTE = "※ 一部の記事は要約に失敗したため、RSS概要を表示しています。"

JST = timezone(timedelta(hours=9))
_ZWSP = "​"


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _sanitize(text: str) -> str:
    """Neutralize mentions (@everyone, <@id>, <#id>), markdown links and bare URLs."""
    text = text.replace("@", "@" + _ZWSP).replace("<", "<" + _ZWSP)
    text = text.replace("://", "://" + _ZWSP)  # stops auto-linking of bare URLs
    return text.replace("[", "\\[").replace("]", "\\]")


def _fit(text: str, limit: int) -> str:
    """Truncate the raw text first, then sanitize. If escaping pushes the result over
    ``limit``, shorten the *raw* text again, so escapes are never cut in half."""
    raw = _truncate(text, limit)
    out = _sanitize(raw)
    if len(out) <= limit:
        return out
    body = text.strip()
    n = min(len(body), limit)
    while n > 0:
        out = _sanitize(body[:n].rstrip()) + "…"
        if len(out) <= limit:
            return out
        n -= 1
    return ""


def build_embed(article: SummarizedArticle) -> dict[str, Any]:
    """One embed for one article. Text fields are sanitized and truncated."""
    embed: dict[str, Any] = {
        "title": _fit(article.headline, MAX_TITLE) or "(無題)",
        "color": COLORS.get(article.genre, DEFAULT_COLOR),
        "footer": {
            "text": _fit(
                f"{GENRE_LABELS.get(article.genre, article.genre)} | {article.source}",
                MAX_FOOTER,
            )
        },
    }
    if article.summary.strip():
        embed["description"] = _fit(article.summary, MAX_DESCRIPTION)
    link = article.link.strip()
    if link.lower().startswith(("http://", "https://")) and len(link) <= MAX_URL:
        embed["url"] = link
    return embed


def _embed_chars(embed: dict[str, Any]) -> int:
    return (
        len(embed.get("title", ""))
        + len(embed.get("description", ""))
        + len(embed.get("footer", {}).get("text", ""))
    )


def format_payloads(
    articles: Sequence[SummarizedArticle],
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return one or more webhook payloads, in posting order.

    Splits so that no message exceeds MAX_EMBEDS_PER_MESSAGE embeds or
    MAX_EMBED_CHARS_PER_MESSAGE embed characters. The first message carries the JST date
    heading; later ones are marked as continuations. If any article is unsummarized, a
    note is appended to the last message's content. Every payload has
    ``allowed_mentions: {"parse": []}``.
    """
    current = (now or datetime.now(JST)).astimezone(JST)
    heading = f"📰 {current:%Y-%m-%d} 今日のニュース"

    groups: list[list[dict[str, Any]]] = []
    group: list[dict[str, Any]] = []
    chars = 0
    for article in articles:
        embed = build_embed(article)
        n = _embed_chars(embed)
        if group and (
            len(group) >= MAX_EMBEDS_PER_MESSAGE or chars + n > MAX_EMBED_CHARS_PER_MESSAGE
        ):
            groups.append(group)
            group, chars = [], 0
        group.append(embed)
        chars += n
    if group:
        groups.append(group)
    if not groups:
        groups = [[]]

    total = len(groups)
    contents = [heading if i == 0 else f"{heading} ({i + 1}/{total})" for i in range(total)]
    if any(not a.summarized for a in articles):
        contents[-1] = f"{contents[-1]}\n{FALLBACK_NOTE}"

    payloads = []
    for content, embeds in zip(contents, groups, strict=True):
        payload: dict[str, Any] = {
            "content": _truncate(content, MAX_CONTENT),
            "allowed_mentions": {"parse": []},
        }
        if embeds:
            payload["embeds"] = embeds
        payloads.append(payload)
    return payloads
