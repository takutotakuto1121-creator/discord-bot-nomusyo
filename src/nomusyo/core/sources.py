"""News source definitions.

All URLs were verified by an actual fetch (HTTP 200, valid RSS/Atom with entries).
The list order is the priority order (earlier sources win in de-duplication).
"""

from __future__ import annotations

from nomusyo.core.models import Source

SOURCES: tuple[Source, ...] = (
    # --- general ---
    Source("NHKニュース 主要", "https://www.nhk.or.jp/rss/news/cat0.xml", "general"),
    Source("NHKニュース 社会", "https://www.nhk.or.jp/rss/news/cat1.xml", "general"),
    Source("NHKニュース 政治", "https://www.nhk.or.jp/rss/news/cat4.xml", "general"),
    Source("NHKニュース 経済", "https://www.nhk.or.jp/rss/news/cat5.xml", "general"),
    # --- tech ---
    Source("ITmedia NEWS", "https://rss.itmedia.co.jp/rss/2.0/news_bursts.xml", "tech"),
    Source("Publickey", "https://www.publickey1.jp/atom.xml", "tech"),
    Source("GIGAZINE", "https://gigazine.net/news/rss_2.0/", "tech"),
    Source("Zenn トレンド", "https://zenn.dev/feed", "tech"),
    Source("はてなブックマーク IT", "https://b.hatena.ne.jp/hotentry/it.rss", "tech"),
    Source("Hacker News", "https://hnrss.org/frontpage", "tech"),
)
