"""Duplicate removal: URL normalization and title similarity."""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from nomusyo.core.models import Article

TITLE_SIMILARITY_THRESHOLD = 0.85
_TRACKING_PARAMS = {"fbclid", "gclid", "yclid", "mc_cid", "mc_eid", "igshid", "ref_src"}


def normalize_url(url: str) -> str:
    """Drop utm_*/tracking params and fragment, unify http/https, trailing slash and host case."""
    parts = urlsplit(url.strip())
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    ]
    path = parts.path.rstrip("/") or ""
    scheme = parts.scheme.lower()
    if scheme == "http":
        scheme = "https"  # treat http/https variants of a page as the same URL
    return urlunsplit((scheme, parts.netloc.lower(), path, urlencode(sorted(query)), ""))


def normalize_title(title: str) -> str:
    """NFKC, lowercase, remove whitespace/punctuation/symbols."""
    t = unicodedata.normalize("NFKC", title).lower()
    return "".join(c for c in t if unicodedata.category(c)[0] in ("L", "N"))


def is_similar(a: str, b: str, threshold: float = TITLE_SIMILARITY_THRESHOLD) -> bool:
    """Compare already-normalized titles."""
    if not a or not b:
        return False
    if a == b:
        return True
    sm = SequenceMatcher(None, a, b)
    return (
        sm.real_quick_ratio() >= threshold
        and sm.quick_ratio() >= threshold
        and (sm.ratio() >= threshold)
    )


def dedupe(
    articles: Iterable[Article], *, threshold: float = TITLE_SIMILARITY_THRESHOLD
) -> list[Article]:
    """Remove duplicates keeping the first occurrence (input order = source priority)."""
    seen_urls: set[str] = set()
    kept: list[Article] = []
    kept_titles: list[str] = []
    for a in articles:
        url = normalize_url(a.link)
        if url in seen_urls:
            continue
        title = normalize_title(a.title)
        if any(is_similar(title, t, threshold) for t in kept_titles):
            continue
        seen_urls.add(url)
        kept.append(a)
        kept_titles.append(title)
    return kept
