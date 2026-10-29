import pytest

from nomusyo.core.dedupe import dedupe, is_similar, normalize_title, normalize_url
from nomusyo.core.models import Article


def art(title, link, source="S", genre="general"):
    return Article(title=title, link=link, description="", source=source, genre=genre)


# ---- normalize_url ----


def test_utm_params_removed():
    assert normalize_url("https://e.com/a?utm_source=x&UTM_Medium=y&id=1") == "https://e.com/a?id=1"


def test_tracking_params_removed():
    assert normalize_url("https://e.com/a?fbclid=abc&gclid=1") == "https://e.com/a"


def test_scheme_and_host_lowercased_path_kept():
    assert normalize_url("HTTPS://Example.COM/Path/A") == "https://example.com/Path/A"


def test_fragment_removed():
    assert normalize_url("https://e.com/a#section") == "https://e.com/a"


def test_trailing_slash_unified():
    assert normalize_url("https://e.com/a/") == normalize_url("https://e.com/a")
    assert normalize_url("https://e.com/") == normalize_url("https://e.com")


def test_query_order_insensitive():
    assert normalize_url("https://e.com/a?b=1&a=2") == normalize_url("https://e.com/a?a=2&b=1")


def test_surrounding_whitespace_stripped():
    assert normalize_url("  https://e.com/a \n") == "https://e.com/a"


def test_meaningful_query_kept():
    assert normalize_url("https://e.com/a?id=1") != normalize_url("https://e.com/a?id=2")


# ---- normalize_title / is_similar ----


def test_normalize_title_fullwidth_case_space_symbols():
    assert normalize_title("ＡＢＣ　Def！ 「テスト」 1-2") == "abcdefテスト12"


def test_is_similar_empty_never_similar():
    assert not is_similar("", "")
    assert not is_similar("abc", "")


def test_is_similar_identical_and_threshold():
    assert is_similar("abc", "abc")
    assert is_similar("abcdefghij", "abcdefghix")  # ratio 0.9
    assert not is_similar("abcde", "vwxyz")


def test_is_similar_threshold_boundary():
    from nomusyo.core.dedupe import TITLE_SIMILARITY_THRESHOLD

    assert TITLE_SIMILARITY_THRESHOLD == 0.85
    assert is_similar("abcdefghij", "abcdxxxxxx") is False  # 0.4
    assert not is_similar("abcde", "abcdx")  # 2*4/10 = 0.8 < 0.85
    assert not is_similar("abcd", "abxy")  # 0.5
    assert is_similar("abcdefghij", "abcdefghix")  # 2*9/20 = 0.9
    # exactly 0.85 is inclusive: 17 matching of 20 chars
    assert is_similar("a" * 17 + "bcd", "a" * 17 + "xyz")


def test_is_similar_custom_threshold():
    assert is_similar("abcde", "abcdx", threshold=0.8)


def test_normalize_url_unifies_http_and_https():
    assert normalize_url("http://Example.com/a/") == normalize_url("https://example.com/a")
    assert normalize_url("http://example.com/a?utm_x=1") == "https://example.com/a"


def test_dedupe_treats_http_and_https_as_same_url():
    mk = lambda link, t: Article(  # noqa: E731
        title=t, link=link, description="", source="s", genre="general"
    )
    out = dedupe([mk("https://example.com/a", "Alpha one"), mk("http://example.com/a/", "Zzz two")])
    assert [a.title for a in out] == ["Alpha one"]


# ---- dedupe ----


def test_dedupe_empty():
    assert dedupe([]) == []


def test_dedupe_same_url_after_normalization_keeps_first():
    a = art("ニュースA", "https://e.com/x?utm_source=tw", source="First")
    b = art("全く別のタイトル", "https://E.com/x/#top", source="Second")
    assert dedupe([a, b]) == [a]


def test_dedupe_similar_titles_keeps_first():
    a = art("政府が新しい経済対策を発表した", "https://a.com/1", source="NHK")
    b = art("政府が新しい経済対策を発表", "https://b.com/2", source="Other")
    assert dedupe([a, b]) == [a]


def test_dedupe_similar_title_across_genres():
    a = art("OpenAI releases new model today", "https://a.com/1", genre="general")
    b = art("OpenAI Releases New Model Today!", "https://b.com/2", genre="tech")
    assert dedupe([a, b]) == [a]
    assert dedupe([b, a]) == [b]  # first occurrence wins regardless of genre


def test_dedupe_different_articles_kept_in_order():
    items = [
        art("天気予報 明日は雨", "https://a.com/1"),
        art("新型スマートフォン発売", "https://a.com/2"),
        art("株価が大幅上昇", "https://a.com/3"),
    ]
    assert dedupe(items) == items


def test_dedupe_custom_threshold():
    a = art("abcdefghij", "https://a.com/1")
    b = art("abcdefgxyz", "https://a.com/2")  # ratio 0.7
    assert dedupe([a, b]) == [a, b]
    assert dedupe([a, b], threshold=0.6) == [a]


def test_dedupe_is_transitive_against_kept_only():
    # c is similar to b (dropped) but not to a (kept) -> c survives
    a = art("aaaaaaaaaa", "https://a.com/1")
    b = art("aaaaaaaaab", "https://a.com/2")
    c = art("aaaaaaaabb", "https://a.com/3")
    result = dedupe([a, b, c], threshold=0.9)
    assert a in result and b not in result


def test_dedupe_symbol_only_titles_not_merged():
    a = art("!!!", "https://a.com/1")
    b = art("???", "https://a.com/2")
    assert dedupe([a, b]) == [a, b]


@pytest.mark.parametrize("n", [1, 2])
def test_dedupe_accepts_generator(n):
    gen = (art(f"{chr(97 + i) * 8}", f"https://a.com/{i}") for i in range(n))
    assert len(dedupe(gen)) == n
