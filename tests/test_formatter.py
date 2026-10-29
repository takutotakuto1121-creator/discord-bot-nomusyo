from datetime import UTC, datetime

import pytest

from nomusyo.core.models import SummarizedArticle
from nomusyo.discord_io import formatter
from nomusyo.discord_io.formatter import (
    FALLBACK_NOTE,
    MAX_DESCRIPTION,
    MAX_EMBED_CHARS_PER_MESSAGE,
    MAX_EMBEDS_PER_MESSAGE,
    MAX_FOOTER,
    MAX_TITLE,
    MAX_URL,
    build_embed,
    format_payloads,
)

NOW = datetime(2026, 10, 28, 22, 0, tzinfo=UTC)  # 2026-10-29 07:00 JST
ZWSP = "​"


def art(**kw):
    base = {
        "headline": "見出し",
        "summary": "要約です。",
        "link": "https://example.com/a",
        "source": "NHK",
        "genre": "general",
        "summarized": True,
    }
    base.update(kw)
    return SummarizedArticle(**base)


def chars(embed):
    return (
        len(embed.get("title", ""))
        + len(embed.get("description", ""))
        + len(embed.get("footer", {}).get("text", ""))
    )


# ---------- build_embed ----------


def test_build_embed_basic():
    e = build_embed(art())
    assert e["title"] == "見出し"
    assert e["description"] == "要約です。"
    assert e["url"] == "https://example.com/a"
    assert e["footer"]["text"] == "総合 | NHK"
    assert e["color"] == formatter.COLORS["general"]


def test_build_embed_tech_color_and_label():
    e = build_embed(art(genre="tech"))
    assert e["color"] == formatter.COLORS["tech"]
    assert e["footer"]["text"].startswith("テック")


def test_build_embed_truncates_long_fields():
    e = build_embed(art(headline="あ" * 1000, summary="い" * 5000, source="う" * 500))
    assert len(e["title"]) <= MAX_TITLE
    assert e["title"].endswith("…")
    assert len(e["description"]) <= MAX_DESCRIPTION
    assert e["description"].endswith("…")
    assert len(e["footer"]["text"]) <= MAX_FOOTER
    assert len(e["title"]) <= 256
    assert len(e["description"]) <= 4096
    assert len(e["footer"]["text"]) <= 2048


def test_build_embed_empty_summary_omits_description():
    assert "description" not in build_embed(art(summary="   "))


def test_build_embed_empty_headline_placeholder():
    assert build_embed(art(headline="  "))["title"] == "(無題)"


@pytest.mark.parametrize(
    "link",
    [
        "javascript:alert(1)",
        "ftp://example.com/x",
        "file:///etc/passwd",
        "//example.com",
        "data:text/html,hi",
        "",
    ],
)
def test_build_embed_rejects_non_http_url(link):
    assert "url" not in build_embed(art(link=link))


def test_build_embed_accepts_http_and_uppercase_scheme():
    assert build_embed(art(link="http://example.com"))["url"] == "http://example.com"
    assert build_embed(art(link="HTTPS://example.com"))["url"] == "HTTPS://example.com"


def test_build_embed_url_length_limit():
    ok = "https://example.com/" + "a" * (MAX_URL - len("https://example.com/"))
    assert len(ok) == MAX_URL
    assert build_embed(art(link=ok))["url"] == ok
    assert "url" not in build_embed(art(link=ok + "a"))
    assert "url" not in build_embed(art(link="https://example.com/" + "a" * 2500))


@pytest.mark.parametrize(
    "text",
    ["@everyone", "@here", "<@123>", "<@&456>", "<#789>"],
)
def test_build_embed_neutralizes_mentions(text):
    e = build_embed(art(headline=f"x {text}", summary=text, source=text))
    for value in (e["title"], e["description"], e["footer"]["text"]):
        assert "@everyone" not in value
        assert "@here" not in value
        assert "<@" not in value
        assert "<#" not in value
    assert ZWSP in e["description"]


def test_build_embed_neutralizes_markdown_link():
    e = build_embed(art(headline="[x](http://evil)", summary="[x](http://evil)"))
    for value in (e["title"], e["description"]):
        assert "[x]" not in value
        assert "\\[x\\]" in value


# ---------- format_payloads ----------


def test_zero_articles_single_heading_payload():
    payloads = format_payloads([], now=NOW)
    assert len(payloads) == 1
    p = payloads[0]
    assert "embeds" not in p
    assert p["allowed_mentions"] == {"parse": []}
    assert "2026-10-29" in p["content"]
    assert FALLBACK_NOTE not in p["content"]


def test_heading_uses_jst_date():
    # 22:00 UTC on the 28th is already the 29th in JST.
    assert "2026-10-29" in format_payloads([art()], now=NOW)[0]["content"]
    early = datetime(2026, 10, 28, 14, 59, tzinfo=UTC)  # 23:59 JST on the 28th
    assert "2026-10-28" in format_payloads([art()], now=early)[0]["content"]


def test_heading_naive_default_now_works():
    payloads = format_payloads([art()])
    assert "今日のニュース" in payloads[0]["content"]


def test_single_payload_no_part_suffix():
    payloads = format_payloads([art() for _ in range(3)], now=NOW)
    assert len(payloads) == 1
    assert len(payloads[0]["embeds"]) == 3
    assert "(1/" not in payloads[0]["content"]


def test_split_by_embed_count():
    n = MAX_EMBEDS_PER_MESSAGE * 2 + 1
    payloads = format_payloads([art() for _ in range(n)], now=NOW)
    assert [len(p["embeds"]) for p in payloads] == [MAX_EMBEDS_PER_MESSAGE] * 2 + [1]
    assert "(" not in payloads[0]["content"]
    assert payloads[1]["content"].endswith(" (2/3)")
    assert payloads[2]["content"].endswith(" (3/3)")


def test_exactly_max_embeds_is_one_payload():
    payloads = format_payloads([art() for _ in range(MAX_EMBEDS_PER_MESSAGE)], now=NOW)
    assert len(payloads) == 1


def test_split_by_total_chars_and_limits_hold():
    # Each embed is large (~1000 desc + title + footer) so chars force splitting before 10 embeds.
    articles = [
        art(headline=f"見出し{i}" * 40, summary="あ" * 2000, source="s" * 300) for i in range(12)
    ]
    payloads = format_payloads(articles, now=NOW)
    assert len(payloads) > 2
    total_embeds = 0
    for p in payloads:
        embeds = p["embeds"]
        total_embeds += len(embeds)
        assert len(embeds) <= MAX_EMBEDS_PER_MESSAGE <= 10
        total = sum(chars(e) for e in embeds)
        assert total <= MAX_EMBED_CHARS_PER_MESSAGE <= 6000
        assert len(p["content"]) <= 2000
    assert total_embeds == 12
    # fewer than 10 per message due to chars
    assert any(len(p["embeds"]) < MAX_EMBEDS_PER_MESSAGE for p in payloads[:-1])
    n = len(payloads)
    for i, p in enumerate(payloads[1:], start=2):
        assert p["content"].endswith(f" ({i}/{n})")


def test_split_preserves_order():
    articles = [art(headline=f"H{i}") for i in range(25)]
    payloads = format_payloads(articles, now=NOW)
    titles = [e["title"] for p in payloads for e in p["embeds"]]
    assert titles == [f"H{i}" for i in range(25)]


def test_allowed_mentions_in_every_payload():
    payloads = format_payloads([art() for _ in range(25)], now=NOW)
    assert len(payloads) == 3
    assert all(p["allowed_mentions"] == {"parse": []} for p in payloads)


def test_malicious_article_text_is_neutralized_in_payloads():
    evil = "@everyone @here <@123> [x](http://evil)"
    payloads = format_payloads([art(headline=evil, summary=evil, source=evil)], now=NOW)
    p = payloads[0]
    e = p["embeds"][0]
    for value in (e["title"], e["description"], e["footer"]["text"]):
        assert "@everyone" not in value
        assert "@here" not in value
        assert "<@123>" not in value
        assert "[x](" not in value
    assert p["allowed_mentions"] == {"parse": []}


def test_fallback_note_only_on_last_payload_when_any_unsummarized():
    articles = [art() for _ in range(11)]
    articles[0] = art(summarized=False)
    payloads = format_payloads(articles, now=NOW)
    assert len(payloads) == 2
    assert FALLBACK_NOTE not in payloads[0]["content"]
    assert FALLBACK_NOTE in payloads[1]["content"]


def test_fallback_note_single_payload():
    payloads = format_payloads([art(summarized=False)], now=NOW)
    assert FALLBACK_NOTE in payloads[0]["content"]


def test_no_fallback_note_when_all_summarized():
    payloads = format_payloads([art(), art()], now=NOW)
    assert all(FALLBACK_NOTE not in p["content"] for p in payloads)


# ---------- bare URL neutralization / _fit ----------


def test_bare_url_is_neutralized_after_scheme_separator():
    e = build_embed(art(summary="詳細は https://evil.example/x を参照 http://a.example"))
    d = e["description"]
    assert "://" + ZWSP in d
    assert "://evil" not in d and "://a.example" not in d
    assert d.count("://" + ZWSP) == 2


def test_bare_url_neutralized_in_title_and_footer():
    e = build_embed(art(headline="see https://x.example", source="https://src.example"))
    assert "://" + ZWSP in e["title"]
    assert "://" + ZWSP in e["footer"]["text"]


def test_embed_link_url_field_stays_intact():
    assert build_embed(art(link="https://example.com/a?x=1"))["url"] == "https://example.com/a?x=1"


def _no_dangling_escape(text):
    # every backslash must be followed by [ or ] (the only things we escape)
    i = 0
    while i < len(text):
        if text[i] == "\\":
            assert i + 1 < len(text) and text[i + 1] in "[]", text
            i += 2
        else:
            i += 1
    # every @ must be followed by ZWSP
    for j, c in enumerate(text):
        if c == "@":
            assert text[j + 1 : j + 2] == ZWSP, text


@pytest.mark.parametrize("limit", range(1, 40))
def test_fit_never_exceeds_limit_or_splits_escapes(limit):
    for raw in ("[[[[[[[[[[[[[[[[[[[[", "@@@@@@@@@@@@@@@@@@@@", "a[b]c@d [x](y) @everyone " * 3):
        out = formatter._fit(raw, limit)
        assert len(out) <= limit
        _no_dangling_escape(out)


def test_fit_truncates_expanded_text_with_ellipsis():
    out = formatter._fit("[" * 100, 11)
    assert len(out) <= 11
    assert out.endswith("…")
    assert "\\" not in out.rstrip("…").replace("\\[", "")


def test_fit_short_text_unchanged_except_escapes():
    assert formatter._fit("a[b]", 50) == "a\\[b\\]"
    assert formatter._fit("  plain  ", 50) == "plain"


def test_fit_all_at_signs_with_tight_limit():
    out = formatter._fit("@" * 50, 10)
    assert len(out) <= 10
    _no_dangling_escape(out)


def test_build_embed_fields_within_limits_with_hostile_text():
    e = build_embed(art(headline="[" * 400, summary="@" * 3000, source="@" * 300))
    assert len(e["title"]) <= MAX_TITLE
    assert len(e["description"]) <= MAX_DESCRIPTION
    assert len(e["footer"]["text"]) <= MAX_FOOTER
    for t in (e["title"], e["description"], e["footer"]["text"]):
        _no_dangling_escape(t)
