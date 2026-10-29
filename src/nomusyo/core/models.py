"""Data types shared by the core pipeline (Discord independent)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

Genre = Literal["general", "tech"]


@dataclass(frozen=True)
class Source:
    """A news feed definition. Order in the sources list is the priority order."""

    name: str
    url: str
    genre: Genre
    weight: int = 1  # reserved for future use (not used by the current selector)


@dataclass(frozen=True)
class Article:
    """An article taken from an RSS/Atom entry (title/description only, no scraping)."""

    title: str
    link: str
    description: str  # HTML stripped, truncated; may be empty
    source: str  # Source.name
    genre: Genre
    published: datetime | None = None  # timezone-aware (UTC) or None if the feed has no date


@dataclass(frozen=True)
class SummarizedArticle:
    """An article ready to be posted. ``link`` always comes from the original feed entry."""

    headline: str
    summary: str  # may be empty when summarized is False and there was no description
    link: str
    source: str
    genre: Genre
    summarized: bool  # False when the LLM summary was unavailable (fallback)
    published: datetime | None = None
