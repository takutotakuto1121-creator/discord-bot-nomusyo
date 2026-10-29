"""Pick the final articles: genre split, source round-robin, per-source cap."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable

from nomusyo.core.models import Article, Genre

MAX_PER_SOURCE = 3
# Extra per-source allowance used only when topping up a short genre.
TOPUP_EXTRA_PER_SOURCE = 2


def _round_robin(
    articles: list[Article], count: int, max_per_source: int, exclude: set[int]
) -> list[Article]:
    """Take up to ``count`` articles, one per source in turn (sources in first-seen order).
    Within a source, input order (newest first as delivered by feeds) is kept."""
    queues: dict[str, list[Article]] = defaultdict(list)
    for a in articles:
        if id(a) not in exclude:
            queues[a.source].append(a)
    for name in queues:
        queues[name] = queues[name][:max_per_source]
    picked: list[Article] = []
    while len(picked) < count and any(queues.values()):
        for name in list(queues):
            if queues[name] and len(picked) < count:
                picked.append(queues[name].pop(0))
    return picked


def select(
    articles: Iterable[Article],
    *,
    total: int,
    general_ratio: float,
    max_per_source: int = MAX_PER_SOURCE,
) -> list[Article]:
    """Select up to ``total`` articles; general first, then tech.

    The general quota is ``floor(total * ratio + 0.5)`` (round half up); a genre that cannot
    fill its quota is topped up from the other genre (the per-source cap is relaxed by
    ``TOPUP_EXTRA_PER_SOURCE`` for the top-up only)."""
    items = list(articles)
    quota: dict[Genre, int] = {"general": math.floor(total * general_ratio + 0.5)}
    quota["tech"] = total - quota["general"]
    by_genre: dict[Genre, list[Article]] = {
        "general": [a for a in items if a.genre == "general"],
        "tech": [a for a in items if a.genre == "tech"],
    }
    picked: dict[Genre, list[Article]] = {
        g: _round_robin(by_genre[g], quota[g], max_per_source, set()) for g in quota
    }
    shortage = total - sum(len(v) for v in picked.values())
    if shortage > 0:
        for g in ("general", "tech"):
            chosen = {id(a) for a in picked[g]}
            # Fill within the genre that still has candidates, counting what it already has.
            extra = _round_robin(
                by_genre[g],
                len(picked[g]) + shortage,
                max_per_source + TOPUP_EXTRA_PER_SOURCE,
                set(),
            )
            extra = [a for a in extra if id(a) not in chosen]
            take = extra[:shortage]
            picked[g].extend(take)
            shortage -= len(take)
            if shortage <= 0:
                break
    return picked["general"] + picked["tech"]
