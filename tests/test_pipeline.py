import pytest

from nomusyo.config import Config
from nomusyo.core.models import Article, Source, SummarizedArticle
from nomusyo.core.pipeline import NoArticlesError, run_pipeline

SOURCES = (
    Source("G", "https://g.example/f", "general"),
    Source("T", "https://t.example/f", "tech"),
)


def cfg(**kw):
    base = dict(
        anthropic_api_key="key",
        summary_model="model-x",
        news_total_count=4,
        news_general_ratio=0.5,
        news_max_age_hours=12,
    )
    base.update(kw)
    return Config(**base)


def article(i, genre="general", source="G", title=None, link=None):
    return Article(
        title=title or f"unique headline {chr(65 + i) * 6} {i * 7919}",
        link=link or f"https://{source}.example/{i}",
        description="d",
        source=source,
        genre=genre,
    )


class Recorder:
    def __init__(self, ret):
        self.ret = ret
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.ret


def fake_summarizer(articles, *, api_key, model, client=None):
    return [
        SummarizedArticle("h", "s", a.link, a.source, a.genre, True, a.published) for a in articles
    ]


def test_pipeline_end_to_end_with_replaced_parts():
    arts = [article(i, "general", "G") for i in range(3)] + [
        article(i + 10, "tech", "T") for i in range(3)
    ]
    fetch = Recorder(arts)
    calls = []

    def summ(articles, **kw):
        calls.append((list(articles), kw))
        return fake_summarizer(articles, **kw)

    sentinel = object()
    out = run_pipeline(cfg(), sources=SOURCES, fetch=fetch, summarizer=summ, client=sentinel)
    assert len(out) == 4
    assert [o.genre for o in out].count("general") == 2
    assert [o.genre for o in out].count("tech") == 2
    # fetch gets sources and max age from config
    (args, kwargs) = fetch.calls[0]
    assert args[0] == SOURCES
    assert kwargs["max_age_hours"] == 12
    # summarizer receives api key, model and client
    assert calls[0][1] == {"api_key": "key", "model": "model-x", "client": sentinel}


def test_pipeline_dedupes_before_selecting():
    a = article(
        0, "general", "G", title="same news headline here", link="https://g.example/1?utm_source=x"
    )
    b = article(1, "general", "G", title="totally different story", link="https://g.example/1")
    c = article(2, "tech", "T", title="same news headline here!", link="https://t.example/9")
    captured = []

    def summ(articles, **kw):
        captured.extend(articles)
        return fake_summarizer(articles, **kw)

    run_pipeline(cfg(), sources=SOURCES, fetch=Recorder([a, b, c]), summarizer=summ)
    assert captured == [a]


def test_pipeline_no_articles_raises():
    summ = Recorder([])
    with pytest.raises(NoArticlesError):
        run_pipeline(cfg(), sources=SOURCES, fetch=Recorder([]), summarizer=summ)
    assert summ.calls == []


def test_pipeline_respects_total_count():
    arts = [article(i, "general", f"G{i % 4}") for i in range(20)]
    out = run_pipeline(
        cfg(news_total_count=3, news_general_ratio=1.0),
        sources=SOURCES,
        fetch=Recorder(arts),
        summarizer=fake_summarizer,
    )
    assert len(out) == 3


def test_pipeline_summarizer_fallback_results_passed_through():
    arts = [article(0)]

    def failing(articles, **kw):
        return [SummarizedArticle(a.title, "", a.link, a.source, a.genre, False) for a in articles]

    out = run_pipeline(cfg(), sources=SOURCES, fetch=Recorder(arts), summarizer=failing)
    assert out[0].summarized is False


def test_pipeline_with_real_summarizer_and_mock_client():
    from types import SimpleNamespace

    arts = [article(0, "general", "G"), article(1, "tech", "T")]
    text = '[{"id":0,"headline":"A","summary":"a"},{"id":1,"headline":"B","summary":"b"}]'
    models = []

    def create(**kw):
        models.append(kw["model"])
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    out = run_pipeline(cfg(), sources=SOURCES, fetch=Recorder(arts), client=client)
    assert models == ["model-x"]
    assert [o.headline for o in out] == ["A", "B"]
    assert [o.link for o in out] == [arts[0].link, arts[1].link]
