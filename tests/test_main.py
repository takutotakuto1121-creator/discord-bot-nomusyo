import json
import logging

import pytest

from nomusyo import main as main_mod
from nomusyo.core.models import SummarizedArticle
from nomusyo.core.pipeline import NoArticlesError
from nomusyo.core.summarizer import SummaryResult
from nomusyo.discord_io.webhook import WebhookError

WEBHOOK = "https://discord.com/api/webhooks/111/DUMMY-WEBHOOK-SECRET-xyz"
API_KEY = "sk-ant-DUMMY-API-KEY-SECRET-abc"
SECRETS = (WEBHOOK, API_KEY, "DUMMY-WEBHOOK-SECRET-xyz", "DUMMY-API-KEY-SECRET-abc")


def art(i=0, **kw):
    base = {
        "headline": f"見出し{i}",
        "summary": "要約",
        "link": f"https://example.com/{i}",
        "source": "NHK",
        "genre": "general",
        "summarized": True,
    }
    base.update(kw)
    return SummarizedArticle(**base)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k in (
        "DISCORD_WEBHOOK_URL",
        "ANTHROPIC_API_KEY",
        "DRY_RUN",
        "LOG_LEVEL",
        "SUMMARY_MODEL",
        "NEWS_TOTAL_COUNT",
        "NEWS_GENERAL_RATIO",
        "NEWS_MAX_AGE_HOURS",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK)
    monkeypatch.setenv("ANTHROPIC_API_KEY", API_KEY)


@pytest.fixture
def sleeps(monkeypatch):
    calls = []
    monkeypatch.setattr(main_mod.time, "sleep", calls.append)
    return calls


@pytest.fixture
def posted(monkeypatch):
    calls = []
    monkeypatch.setattr(main_mod, "post_webhook", lambda url, payload: calls.append((url, payload)))
    return calls


def set_articles(monkeypatch, articles):
    monkeypatch.setattr(main_mod, "run_pipeline", lambda config: articles)


def assert_no_secrets(*texts):
    for t in texts:
        for s in SECRETS:
            assert s not in t


def test_success_posts_and_exit_0(monkeypatch, posted, sleeps, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    set_articles(monkeypatch, [art(i) for i in range(3)])
    assert main_mod.main([]) == 0
    assert len(posted) == 1
    url, payload = posted[0]
    assert url == WEBHOOK
    assert len(payload["embeds"]) == 3
    assert payload["allowed_mentions"] == {"parse": []}
    assert sleeps == []
    out = capsys.readouterr()
    assert_no_secrets(out.out, out.err, caplog.text)


def test_success_split_sleeps_between_messages(monkeypatch, posted, sleeps):
    set_articles(monkeypatch, [art(i) for i in range(25)])
    assert main_mod.main([]) == 0
    assert len(posted) == 3
    assert sleeps == [main_mod.INTER_MESSAGE_DELAY] * 2


def test_dry_run_flag_prints_json_without_posting(monkeypatch, posted, sleeps, capsys):
    set_articles(monkeypatch, [art(i) for i in range(2)])
    assert main_mod.main(["--dry-run"]) == 0
    assert posted == []
    data = json.loads(capsys.readouterr().out)
    assert len(data["embeds"]) == 2
    assert data["allowed_mentions"] == {"parse": []}


def test_dry_run_env_prints_json_without_posting(monkeypatch, posted, sleeps, capsys):
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.delenv("DISCORD_WEBHOOK_URL")
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    set_articles(monkeypatch, [art()])
    assert main_mod.main([]) == 0
    assert posted == []
    assert json.loads(capsys.readouterr().out)["embeds"]


def test_dry_run_split_outputs_each_payload(monkeypatch, posted, sleeps, capsys):
    set_articles(monkeypatch, [art(i) for i in range(12)])
    assert main_mod.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    decoder = json.JSONDecoder()
    idx, found = 0, []
    while idx < len(out):
        if out[idx].isspace():
            idx += 1
            continue
        obj, idx = decoder.raw_decode(out, idx)
        found.append(obj)
    assert len(found) == 2
    assert posted == []


def test_dry_run_does_not_leak_secrets(monkeypatch, posted, sleeps, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    set_articles(monkeypatch, [art()])
    assert main_mod.main(["--dry-run"]) == 0
    out = capsys.readouterr()
    assert_no_secrets(out.out, out.err, caplog.text)


def test_no_articles_exit_1(monkeypatch, posted, sleeps, capsys, caplog):
    def boom(config):
        raise NoArticlesError("no articles")

    monkeypatch.setattr(main_mod, "run_pipeline", boom)
    assert main_mod.main([]) == 1
    assert posted == []
    out = capsys.readouterr()
    assert_no_secrets(out.out, out.err, caplog.text)


def test_config_error_exit_1(monkeypatch, posted, capsys, caplog):
    monkeypatch.setenv("NEWS_TOTAL_COUNT", "abc")
    called = []
    monkeypatch.setattr(main_mod, "run_pipeline", lambda c: called.append(1) or [art()])
    assert main_mod.main([]) == 1
    assert called == [] and posted == []
    out = capsys.readouterr()
    assert_no_secrets(out.out, out.err, caplog.text)


def test_missing_secrets_config_error_exit_1(monkeypatch, posted, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    set_articles(monkeypatch, [art()])
    assert main_mod.main([]) == 1
    assert posted == []
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_webhook_error_exit_1(monkeypatch, sleeps, capsys, caplog):
    def fail(url, payload):
        raise WebhookError("Discord returned HTTP 400")

    monkeypatch.setattr(main_mod, "post_webhook", fail)
    set_articles(monkeypatch, [art()])
    assert main_mod.main([]) == 1
    out = capsys.readouterr()
    assert "HTTP 400" in caplog.text
    assert_no_secrets(out.out, out.err, caplog.text)


def test_webhook_error_midway_stops_immediately(monkeypatch, sleeps):
    calls = []

    def flaky(url, payload):
        calls.append(payload)
        if len(calls) == 2:
            raise WebhookError("boom")

    monkeypatch.setattr(main_mod, "post_webhook", flaky)
    set_articles(monkeypatch, [art(i) for i in range(25)])  # 3 payloads
    assert main_mod.main([]) == 1
    assert len(calls) == 2  # third never sent, first not re-sent
    assert sleeps == [main_mod.INTER_MESSAGE_DELAY]


def test_webhook_url_missing_non_dry_run_exit_1(monkeypatch, posted, capsys):
    # The webhook URL check lives in load_config; main must turn the ConfigError into exit 1.
    real = main_mod.load_config
    seen = {}

    def cfg(*, dry_run_override=False):
        seen["dry_run_override"] = dry_run_override
        return real(
            {"ANTHROPIC_API_KEY": API_KEY, "DISCORD_WEBHOOK_URL": ""},
            dry_run_override=dry_run_override,
        )

    monkeypatch.setattr(main_mod, "load_config", cfg)
    set_articles(monkeypatch, [art()])
    assert main_mod.main([]) == 1
    assert seen["dry_run_override"] is False
    assert posted == []
    assert "DISCORD_WEBHOOK_URL" in capsys.readouterr().err


def test_dry_run_flag_is_passed_to_load_config(monkeypatch, posted, capsys):
    real = main_mod.load_config
    seen = {}

    def cfg(*, dry_run_override=False):
        seen["dry_run_override"] = dry_run_override
        return real({}, dry_run_override=dry_run_override)

    monkeypatch.setattr(main_mod, "load_config", cfg)
    set_articles(monkeypatch, [art()])
    assert main_mod.main(["--dry-run"]) == 0
    assert seen["dry_run_override"] is True
    assert posted == []


def test_dry_run_flag_works_without_any_secrets(monkeypatch, posted, sleeps, capsys, caplog):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL")
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    set_articles(monkeypatch, [art()])
    assert main_mod.main(["--dry-run"]) == 0
    assert posted == []
    assert json.loads(capsys.readouterr().out)["embeds"]


@pytest.fixture
def restore_loggers():
    names = ("urllib3", "httpx", "httpcore", "anthropic", "requests", "nomusyo")
    saved = {n: logging.getLogger(n).level for n in names}
    yield
    for n, lvl in saved.items():
        logging.getLogger(n).setLevel(lvl)


def test_debug_log_level_keeps_http_libraries_quiet(
    monkeypatch, posted, sleeps, restore_loggers, capsys
):
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    set_articles(monkeypatch, [art()])
    assert main_mod.main([]) == 0
    for name in ("urllib3", "httpx", "httpcore", "anthropic", "requests"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING, name
        assert not logging.getLogger(name).isEnabledFor(logging.DEBUG)


# ---- permanent summarizer errors ----


def failed(articles, err="AuthenticationError"):
    res = SummaryResult(articles)
    res.summary_error = err
    return res


def test_summary_error_posts_fallback_then_exit_1(monkeypatch, posted, sleeps, caplog):
    set_articles(monkeypatch, failed([art(i, summarized=False) for i in range(2)]))
    assert main_mod.main([]) == 1
    assert len(posted) == 1  # fallback digest is still posted
    assert len(posted[0][1]["embeds"]) == 2
    assert "AuthenticationError" in caplog.text
    assert_no_secrets(caplog.text)


def test_summary_error_dry_run_prints_and_exit_1(monkeypatch, posted, sleeps, capsys):
    set_articles(monkeypatch, failed([art(summarized=False)], "NotFoundError"))
    assert main_mod.main(["--dry-run"]) == 1
    assert posted == []
    assert json.loads(capsys.readouterr().out)["embeds"]


def test_summary_error_with_webhook_failure_exit_1(monkeypatch, sleeps):
    def fail(url, payload):
        raise WebhookError("boom")

    monkeypatch.setattr(main_mod, "post_webhook", fail)
    set_articles(monkeypatch, failed([art(summarized=False)]))
    assert main_mod.main([]) == 1


def test_transient_failure_fallback_exit_0(monkeypatch, posted, sleeps):
    # plain list without summary_error (transient errors): fallback digest, exit 0
    set_articles(monkeypatch, SummaryResult([art(summarized=False)]))
    assert main_mod.main([]) == 0
    assert len(posted) == 1
    assert "要約に失敗" in posted[0][1]["content"]


def test_webhook_url_unset_env_exit_1(monkeypatch, posted, capsys):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL")
    set_articles(monkeypatch, [art()])
    assert main_mod.main([]) == 1
    assert posted == []


def test_secrets_not_in_config_repr():
    cfg = main_mod.load_config()
    assert_no_secrets(repr(cfg), str(cfg))
