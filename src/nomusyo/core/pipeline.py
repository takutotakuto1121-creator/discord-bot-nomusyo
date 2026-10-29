"""fetch -> dedupe -> select -> summarize. Discord independent; public API of ``core``."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

from nomusyo.config import Config
from nomusyo.core.dedupe import dedupe
from nomusyo.core.fetcher import fetch_all
from nomusyo.core.models import Article, Source, SummarizedArticle
from nomusyo.core.selector import select
from nomusyo.core.sources import SOURCES
from nomusyo.core.summarizer import summarize

log = logging.getLogger(__name__)


class NoArticlesError(Exception):
    """Raised when no article could be collected (all sources failed or all too old)."""


def run_pipeline(
    config: Config,
    *,
    sources: Sequence[Source] = SOURCES,
    fetch: Callable[..., list[Article]] = fetch_all,
    summarizer: Callable[..., list[SummarizedArticle]] = summarize,
    client: Any | None = None,
) -> list[SummarizedArticle]:
    """Run the whole pipeline. Raises NoArticlesError when there is nothing to post.
    Summarization failures degrade to ``summarized=False`` items instead of raising."""
    fetched = fetch(sources, max_age_hours=config.news_max_age_hours)
    unique = dedupe(fetched)
    selected = select(
        unique, total=config.news_total_count, general_ratio=config.news_general_ratio
    )
    log.info("fetched=%d unique=%d selected=%d", len(fetched), len(unique), len(selected))
    if not selected:
        raise NoArticlesError("no articles collected")
    # The result may carry a ``summary_error`` attribute (see ``SummaryResult``); keep it.
    return summarizer(
        selected, api_key=config.anthropic_api_key, model=config.summary_model, client=client
    )
