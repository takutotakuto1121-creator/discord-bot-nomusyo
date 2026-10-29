import time
from datetime import UTC, datetime

import pytest
import requests

from nomusyo.core.fetcher import (
    FETCH_DEADLINE_SECONDS,
    MAX_DESCRIPTION_CHARS,
    MAX_PER_SOURCE,
    MAX_RESPONSE_BYTES,
    clean_text,
    fetch_all,
    fetch_source,
    parse_feed,
)
from nomusyo.core.models import Source

NOW = datetime(2026, 10, 29, 0, 0, 0, tzinfo=UTC)
SRC = Source("Test", "https://example.com/feed.xml", "general")
SRC2 = Source("Other", "https://example.org/rss", "tech")

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>t</title>
<item><title>新しい記事</title><link>https://example.com/a</link>
<description>&lt;p&gt;本文&lt;b&gt;太字&lt;/b&gt; &amp;amp; 続き&lt;/p&gt;</description>
<pubDate>Wed, 28 Oct 2026 20:00:00 +0000</pubDate></item>
<item><title>古い記事</title><link>https://example.com/b</link>
<description>old</description>
<pubDate>Mon, 26 Oct 2026 00:00:00 +0000</pubDate></item>
<item><title>日付なし</title><link>https://example.com/c</link><description>nodate</description></item>
<item><title></title><link>https://example.com/d</link></item>
<item><title>リンクなし</title></item>
<item><title>不正スキーム</title><link>javascript:alert(1)</link></item>
</channel></rss>""".encode()

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>a</title>
<entry><title>Atom記事</title><link href="https://example.net/x"/>
<updated>2026-10-28T23:00:00Z</updated><summary>atom summary</summary></entry>
</feed>""".encode()

HN = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>hn</title>
<item><title>Show HN: thing</title><link>https://example.com/hn</link>
<description>&lt;p&gt;Article URL: &lt;a href="https://example.com/hn"&gt;x&lt;/a&gt;&lt;/p&gt;
&lt;p&gt;Points: 10&lt;/p&gt;</description>
<pubDate>Wed, 28 Oct 2026 20:00:00 +0000</pubDate></item></channel></rss>"""


class FakeResp:
    def __init__(self, content=b"", status=200, headers=None, history=None, url=None, chunks=None):
        self.content = content
        self.status_code = status
        self.headers = headers or {}
        self.history = history or []
        self.url = url or "https://example.com/feed.xml"
        self._chunks = chunks
        self.closed = False

    def iter_content(self, chunk_size=1):
        if self._chunks is not None:
            yield from self._chunks
            return
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i : i + chunk_size]

    def close(self):
        self.closed = True

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


class FakeSession:
    """Maps url -> bytes / exception / FakeResp."""

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        item = self.mapping[url]
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, bytes):
            return FakeResp(item)
        return item


# ---- clean_text ----


def test_clean_text_strips_html_and_entities():
    assert clean_text("<p>a <b>b</b>&amp;c&nbsp;d</p>") == "a b &c d"


def test_clean_text_collapses_whitespace():
    assert clean_text("  a \n\t b   c ") == "a b c"


def test_clean_text_truncates_and_rstrips():
    assert clean_text("abc def", 4) == "abc"
    assert clean_text("abcdef", 6) == "abcdef"
    assert len(clean_text("あ" * 600, MAX_DESCRIPTION_CHARS)) == MAX_DESCRIPTION_CHARS


def test_clean_text_empty_and_none():
    assert clean_text("") == ""
    assert clean_text(None) == ""  # type: ignore[arg-type]


# ---- parse_feed ----


def test_parse_rss_basic_and_filters_invalid_entries():
    arts = parse_feed(RSS, SRC)
    assert [a.title for a in arts] == ["新しい記事", "古い記事", "日付なし"]
    first = arts[0]
    assert first.link == "https://example.com/a"
    assert first.description == "本文 太字 & 続き"
    assert "<" not in first.description
    assert first.source == "Test"
    assert first.genre == "general"
    assert first.published == datetime(2026, 10, 28, 20, 0, tzinfo=UTC)
    assert arts[2].published is None


def test_parse_atom():
    arts = parse_feed(ATOM, SRC2)
    assert len(arts) == 1
    a = arts[0]
    assert a.title == "Atom記事"
    assert a.link == "https://example.net/x"
    assert a.description == "atom summary"
    assert a.genre == "tech"
    assert a.published == datetime(2026, 10, 28, 23, 0, tzinfo=UTC)


def test_parse_hn_article_url_description_is_empty():
    arts = parse_feed(HN, SRC2)
    assert len(arts) == 1
    assert arts[0].description == ""


def test_parse_description_truncated():
    long = "x" * 2000
    xml = (
        f'<rss version="2.0"><channel><title>t</title><item><title>T</title>'
        f"<link>https://e.com/1</link><description>{long}</description></item></channel></rss>"
    ).encode()
    assert len(parse_feed(xml, SRC)[0].description) == MAX_DESCRIPTION_CHARS


def test_parse_limits_entries_per_source():
    items = "".join(
        f"<item><title>T{i}</title><link>https://e.com/{i}</link></item>" for i in range(50)
    )
    xml = f'<rss version="2.0"><channel><title>t</title>{items}</channel></rss>'.encode()
    assert len(parse_feed(xml, SRC)) == MAX_PER_SOURCE


def test_parse_broken_feed_raises():
    with pytest.raises(ValueError):
        parse_feed(b"this is not a feed", SRC)


@pytest.mark.parametrize("content", [b"", b"   \n", b"<html><body>hi</body></html>"])
def test_parse_empty_or_html_raises(content):
    with pytest.raises(ValueError):
        parse_feed(content, SRC)


def test_parse_sorts_newest_first_undated_last_then_caps():
    items = "<item><title>nodate</title><link>https://e.com/nd</link></item>"
    for i in range(25):  # i=0 oldest ... i=24 newest
        items += (
            f"<item><title>T{i}</title><link>https://e.com/{i}</link>"
            f"<pubDate>Wed, 28 Oct 2026 {i % 24:02d}:00:00 +0000</pubDate></item>"
        )
    xml = f'<rss version="2.0"><channel><title>t</title>{items}</channel></rss>'.encode()
    arts = parse_feed(xml, SRC)
    assert len(arts) == MAX_PER_SOURCE
    times = [a.published for a in arts]
    assert times == sorted(times, reverse=True)
    # the undated item is sorted behind all dated ones, so the cap drops it
    assert "nodate" not in [a.title for a in arts]


def test_parse_sort_keeps_undated_last_when_room():
    xml = (
        b'<rss version="2.0"><channel><title>t</title>'
        b"<item><title>nodate</title><link>https://e.com/nd</link></item>"
        b"<item><title>old</title><link>https://e.com/o</link>"
        b"<pubDate>Mon, 26 Oct 2026 00:00:00 +0000</pubDate></item>"
        b"<item><title>new</title><link>https://e.com/n</link>"
        b"<pubDate>Wed, 28 Oct 2026 00:00:00 +0000</pubDate></item>"
        b"</channel></rss>"
    )
    assert [a.title for a in parse_feed(xml, SRC)] == ["new", "old", "nodate"]


def test_parse_valid_feed_with_zero_entries_is_empty():
    xml = b'<rss version="2.0"><channel><title>t</title></channel></rss>'
    assert parse_feed(xml, SRC) == []


# ---- fetch_source ----


def test_fetch_source_filters_by_age_keeps_undated():
    session = FakeSession({SRC.url: RSS})
    arts = fetch_source(SRC, max_age_hours=36, now=NOW, session=session)
    # NOW - 36h = 2026-10-27 12:00; the 10-26 article is dropped
    assert [a.title for a in arts] == ["新しい記事", "日付なし"]


def test_fetch_source_age_boundary_is_inclusive():
    session = FakeSession({SRC.url: RSS})
    # article A is exactly 4h before NOW
    kept = fetch_source(SRC, max_age_hours=4, now=NOW, session=session)
    assert "新しい記事" in [a.title for a in kept]
    dropped = fetch_source(SRC, max_age_hours=3, now=NOW, session=session)
    assert "新しい記事" not in [a.title for a in dropped]
    assert "日付なし" in [a.title for a in dropped]


def test_fetch_source_passes_timeout_and_user_agent():
    session = FakeSession({SRC.url: RSS})
    fetch_source(SRC, max_age_hours=36, now=NOW, session=session, timeout=3.5)
    url, kw = session.calls[0]
    assert url == SRC.url
    assert kw["timeout"] == 3.5
    assert "NomusyoBot" in kw["headers"]["User-Agent"]


def test_fetch_source_uses_requests_get_without_session(monkeypatch):
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return FakeResp(RSS)

    monkeypatch.setattr(requests, "get", fake_get)
    arts = fetch_source(SRC, max_age_hours=36, now=NOW)
    assert calls == [SRC.url]
    assert arts


@pytest.mark.parametrize(
    "exc", [requests.Timeout("t"), requests.ConnectionError("c")], ids=["timeout", "connection"]
)
def test_fetch_source_propagates_network_errors(exc):
    session = FakeSession({SRC.url: exc})
    with pytest.raises(type(exc)):
        fetch_source(SRC, max_age_hours=36, now=NOW, session=session)


def test_fetch_source_http_error_raises():
    session = FakeSession({SRC.url: FakeResp(b"", status=500)})
    with pytest.raises(requests.HTTPError):
        fetch_source(SRC, max_age_hours=36, now=NOW, session=session)


def test_fetch_source_too_large_raises():
    session = FakeSession({SRC.url: FakeResp(b"x" * (MAX_RESPONSE_BYTES + 1))})
    with pytest.raises(ValueError, match="too large"):
        fetch_source(SRC, max_age_hours=36, now=NOW, session=session)


def test_fetch_source_broken_feed_raises():
    session = FakeSession({SRC.url: b"garbage"})
    with pytest.raises(ValueError):
        fetch_source(SRC, max_age_hours=36, now=NOW, session=session)


def test_fetch_source_uses_stream_and_closes_response():
    resp = FakeResp(RSS)
    session = FakeSession({SRC.url: resp})
    fetch_source(SRC, max_age_hours=36, now=NOW, session=session)
    assert session.calls[0][1]["stream"] is True
    assert resp.closed


def test_fetch_source_closes_response_on_error():
    resp = FakeResp(b"", status=500)
    with pytest.raises(requests.HTTPError):
        fetch_source(SRC, max_age_hours=36, now=NOW, session=FakeSession({SRC.url: resp}))
    assert resp.closed


@pytest.mark.parametrize("content", [b"", b"<html><body>hi</body></html>"])
def test_fetch_source_empty_or_html_raises(content):
    session = FakeSession({SRC.url: content})
    with pytest.raises(ValueError):
        fetch_source(SRC, max_age_hours=36, now=NOW, session=session)


def test_fetch_source_rejects_content_length_over_limit():
    resp = FakeResp(RSS, headers={"Content-Length": str(MAX_RESPONSE_BYTES + 1)})
    with pytest.raises(ValueError, match="too large"):
        fetch_source(SRC, max_age_hours=36, now=NOW, session=FakeSession({SRC.url: resp}))


def test_fetch_source_content_length_ignored_when_not_digits():
    resp = FakeResp(RSS, headers={"Content-Length": "abc"})
    assert fetch_source(SRC, max_age_hours=36, now=NOW, session=FakeSession({SRC.url: resp}))


def test_fetch_source_stream_exceeding_limit_without_content_length():
    # no Content-Length header: the limit is enforced while streaming
    chunk = b"x" * (MAX_RESPONSE_BYTES // 2 + 1)
    resp = FakeResp(chunks=[chunk, chunk, chunk])
    with pytest.raises(ValueError, match="too large"):
        fetch_source(SRC, max_age_hours=36, now=NOW, session=FakeSession({SRC.url: resp}))
    assert resp.closed


def test_fetch_source_https_redirect_allowed():
    hop = FakeResp(history=None, url="https://example.com/feed.xml")
    resp = FakeResp(RSS, history=[hop], url="https://cdn.example.com/feed.xml")
    arts = fetch_source(SRC, max_age_hours=36, now=NOW, session=FakeSession({SRC.url: resp}))
    assert arts


@pytest.mark.parametrize(
    "final",
    ["http://example.com/feed.xml"],
)
def test_fetch_source_rejects_redirect_to_http(final):
    first = FakeResp(url=SRC.url)
    resp = FakeResp(RSS, history=[first], url=final)
    with pytest.raises(ValueError, match="non-https"):
        fetch_source(SRC, max_age_hours=36, now=NOW, session=FakeSession({SRC.url: resp}))
    assert resp.closed


def test_fetch_source_rejects_intermediate_http_hop():
    first = FakeResp(url=SRC.url)
    mid = FakeResp(url="http://mid.example.com/feed")
    resp = FakeResp(RSS, history=[first, mid], url="https://example.com/final")
    with pytest.raises(ValueError, match="non-https"):
        fetch_source(SRC, max_age_hours=36, now=NOW, session=FakeSession({SRC.url: resp}))


def test_fetch_source_deadline_exceeded_while_streaming():
    resp = FakeResp(RSS)
    deadline = time.monotonic() - 1  # already past
    with pytest.raises(TimeoutError):
        fetch_source(
            SRC, max_age_hours=36, now=NOW, session=FakeSession({SRC.url: resp}), deadline=deadline
        )
    assert resp.closed


def test_fetch_source_deadline_in_future_ok():
    session = FakeSession({SRC.url: RSS})
    deadline = time.monotonic() + 100
    assert fetch_source(SRC, max_age_hours=36, now=NOW, session=session, deadline=deadline)


# ---- fetch_all ----


def test_fetch_deadline_default():
    assert FETCH_DEADLINE_SECONDS == 120.0


def test_fetch_all_total_timeout_zero_skips_all_sources(caplog):
    session = FakeSession({SRC.url: RSS, SRC2.url: ATOM})
    with caplog.at_level("WARNING"):
        arts = fetch_all([SRC, SRC2], max_age_hours=36, now=NOW, session=session, total_timeout=-1)
    assert arts == []
    assert session.calls == []
    assert sum("deadline" in r.message for r in caplog.records) == 2


def test_fetch_all_skips_remaining_after_deadline(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr("nomusyo.core.fetcher.time.monotonic", lambda: clock["t"])

    class SlowResp(FakeResp):
        def close(self):
            clock["t"] += 100  # the whole fetch of this source "took" 100s
            super().close()

    session = FakeSession({SRC.url: SlowResp(RSS), SRC2.url: ATOM})
    arts = fetch_all([SRC, SRC2], max_age_hours=36, now=NOW, session=session, total_timeout=50)
    assert {a.source for a in arts} == {"Test"}  # second source never started
    assert len(session.calls) == 1


def test_fetch_all_collects_in_source_order():
    session = FakeSession({SRC.url: RSS, SRC2.url: ATOM})
    arts = fetch_all([SRC, SRC2], max_age_hours=36, now=NOW, session=session)
    assert [a.source for a in arts] == ["Test", "Test", "Other"]


def test_fetch_all_isolates_failures(caplog):
    third = Source("Third", "https://third.example/feed", "tech")
    broken = Source("Broken", "https://broken.example/feed", "tech")
    http500 = Source("Http", "https://http.example/feed", "tech")
    session = FakeSession(
        {
            SRC.url: requests.Timeout("slow"),
            broken.url: b"not a feed",
            http500.url: FakeResp(b"", 503),
            third.url: ATOM,
            SRC2.url: requests.ConnectionError("down"),
        }
    )
    with caplog.at_level("WARNING"):
        arts = fetch_all(
            [SRC, broken, http500, SRC2, third], max_age_hours=36, now=NOW, session=session
        )
    assert [a.title for a in arts] == ["Atom記事"]
    assert len(session.calls) == 5
    assert sum("failed" in r.message for r in caplog.records) == 4


def test_fetch_all_all_fail_returns_empty():
    session = FakeSession({SRC.url: requests.Timeout("x"), SRC2.url: b""})
    assert fetch_all([SRC, SRC2], max_age_hours=36, now=NOW, session=session) == []


def test_fetch_all_no_sources():
    assert fetch_all([], max_age_hours=36, now=NOW, session=FakeSession({})) == []
