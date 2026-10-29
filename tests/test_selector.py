from nomusyo.core.models import Article
from nomusyo.core.selector import MAX_PER_SOURCE, TOPUP_EXTRA_PER_SOURCE, select


def mk(genre, source, n):
    return [
        Article(
            title=f"{source}-{i}",
            link=f"https://{source}.example/{i}",
            description="",
            source=source,
            genre=genre,
        )
        for i in range(n)
    ]


def genres(sel):
    return [a.genre for a in sel]


def test_zero_articles():
    assert select([], total=10, general_ratio=0.5) == []


def test_default_split_five_five():
    items = (
        mk("general", "g1", 3) + mk("general", "g2", 3) + mk("tech", "t1", 3) + mk("tech", "t2", 3)
    )
    sel = select(items, total=10, general_ratio=0.5)
    assert len(sel) == 10
    assert genres(sel).count("general") == 5
    assert genres(sel).count("tech") == 5


def test_general_first_then_tech_order():
    items = mk("tech", "t1", 2) + mk("general", "g1", 2)
    sel = select(items, total=4, general_ratio=0.5)
    assert genres(sel) == ["general", "general", "tech", "tech"]


def test_round_robin_across_sources():
    items = mk("general", "a", 3) + mk("general", "b", 3) + mk("general", "c", 3)
    sel = select(items, total=6, general_ratio=1.0)
    assert [x.title for x in sel] == ["a-0", "b-0", "c-0", "a-1", "b-1", "c-1"]


def test_per_source_cap_is_three_for_normal_allocation():
    assert MAX_PER_SOURCE == 3
    assert TOPUP_EXTRA_PER_SOURCE == 2
    # two genres, each with one big source: normal allocation stops at 3 per source
    items = mk("general", "a", 10) + mk("tech", "t", 10)
    sel = select(items, total=6, general_ratio=0.5)
    assert [x.title for x in sel] == ["a-0", "a-1", "a-2", "t-0", "t-1", "t-2"]


def test_topup_relaxes_cap_by_two_for_single_source():
    sel = select(mk("general", "a", 10), total=10, general_ratio=1.0)
    assert len(sel) == MAX_PER_SOURCE + TOPUP_EXTRA_PER_SOURCE == 5
    assert [x.title for x in sel] == [f"a-{i}" for i in range(5)]


def test_custom_per_source_cap():
    sel = select(mk("general", "a", 10), total=10, general_ratio=1.0, max_per_source=5)
    assert len(sel) == 5 + TOPUP_EXTRA_PER_SOURCE
    sel = select(mk("general", "a", 10), total=4, general_ratio=1.0, max_per_source=2)
    assert len(sel) == 4  # 2 normally + 2 topped up


def test_shortage_in_tech_filled_by_general():
    items = mk("general", "g1", 3) + mk("general", "g2", 3) + mk("general", "g3", 3)
    items += mk("tech", "t1", 1)
    sel = select(items, total=10, general_ratio=0.5)
    assert genres(sel).count("tech") == 1
    assert genres(sel).count("general") == 9
    assert len(sel) == 10


def test_shortage_in_general_filled_by_tech():
    items = mk("general", "g1", 1)
    items += mk("tech", "t1", 3) + mk("tech", "t2", 3) + mk("tech", "t3", 3) + mk("tech", "t4", 3)
    sel = select(items, total=10, general_ratio=0.5)
    assert genres(sel).count("general") == 1
    assert genres(sel).count("tech") == 9


def test_fill_respects_relaxed_cap_and_returns_fewer_than_total():
    items = mk("general", "g1", 10) + mk("tech", "t1", 10)
    sel = select(items, total=10, general_ratio=0.5)
    # 3+3 normally, then +2 per source on top-up: 5 + 5
    assert len(sel) == 10
    assert genres(sel).count("general") == 5
    sel = select(items, total=14, general_ratio=0.5)
    assert len(sel) == 10  # relaxed cap (5 per source) is a hard limit
    assert genres(sel).count("general") == 5
    assert genres(sel).count("tech") == 5


def test_topup_does_not_duplicate_or_reorder_normal_picks():
    items = mk("general", "g1", 6) + mk("tech", "t1", 1)
    sel = select(items, total=6, general_ratio=0.5)
    assert [x.title for x in sel] == ["g1-0", "g1-1", "g1-2", "g1-3", "g1-4", "t1-0"]
    assert len({id(a) for a in sel}) == len(sel)


def test_no_duplicates_in_result():
    items = mk("general", "g1", 3) + mk("general", "g2", 3) + mk("tech", "t1", 2)
    sel = select(items, total=10, general_ratio=0.5)
    assert len({id(a) for a in sel}) == len(sel)
    assert len(sel) == 8


def test_only_one_genre_present():
    sel = select(mk("tech", "t1", 3) + mk("tech", "t2", 3), total=4, general_ratio=0.5)
    assert len(sel) == 4
    assert set(genres(sel)) == {"tech"}


def test_ratio_zero_and_one():
    items = (
        mk("general", "g1", 3) + mk("general", "g2", 3) + mk("tech", "t1", 3) + mk("tech", "t2", 3)
    )
    only_tech = select(items, total=4, general_ratio=0.0)
    assert genres(only_tech) == ["tech"] * 4
    only_general = select(items, total=4, general_ratio=1.0)
    assert genres(only_general) == ["general"] * 4


def test_ratio_rounding():
    items = (
        mk("general", "g1", 3) + mk("general", "g2", 3) + mk("tech", "t1", 3) + mk("tech", "t2", 3)
    )
    sel = select(items, total=5, general_ratio=0.7)  # round(3.5)=4 (banker's) -> 4 general
    assert len(sel) == 5
    assert genres(sel).count("general") == round(5 * 0.7)


def test_total_one_and_zero():
    items = mk("general", "g1", 2) + mk("tech", "t1", 2)
    assert len(select(items, total=1, general_ratio=0.5)) == 1
    assert select(items, total=0, general_ratio=0.5) == []


def test_total_larger_than_available():
    items = mk("general", "g1", 2) + mk("tech", "t1", 1)
    assert len(select(items, total=10, general_ratio=0.5)) == 3


def test_input_order_kept_within_source():
    items = mk("general", "a", 3)
    assert [x.title for x in select(items, total=3, general_ratio=1.0)] == ["a-0", "a-1", "a-2"]


def test_rounding_half_up_not_bankers():
    items = (
        mk("general", "g1", 3) + mk("general", "g2", 3) + mk("tech", "t1", 3) + mk("tech", "t2", 3)
    )
    # floor(x + 0.5): round() would give 2 for 2.5 and 4 for 3.5 (banker's)
    sel = select(items, total=5, general_ratio=0.5)
    assert genres(sel).count("general") == 3
    assert genres(sel).count("tech") == 2
    sel = select(items, total=3, general_ratio=0.5)
    assert genres(sel).count("general") == 2
    sel = select(items, total=5, general_ratio=0.7)
    assert genres(sel).count("general") == 4
    sel = select(items, total=5, general_ratio=0.3)  # 1.5 -> 2
    assert genres(sel).count("general") == 2
