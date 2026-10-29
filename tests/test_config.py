import pytest

from nomusyo.config import DEFAULT_MODEL, Config, ConfigError, load_config

WEBHOOK = "https://discord.com/api/webhooks/123/SECRET-TOKEN-WEBHOOK"
API_KEY = "sk-ant-SECRET-KEY-VALUE"


def full_env(**over):
    env = {"DISCORD_WEBHOOK_URL": WEBHOOK, "ANTHROPIC_API_KEY": API_KEY}
    env.update(over)
    return env


def test_defaults():
    cfg = load_config(full_env())
    assert cfg.discord_webhook_url == WEBHOOK
    assert cfg.anthropic_api_key == API_KEY
    assert cfg.summary_model == DEFAULT_MODEL
    assert cfg.news_total_count == 10
    assert cfg.news_general_ratio == 0.5
    assert cfg.news_max_age_hours == 36
    assert cfg.dry_run is False
    assert cfg.log_level == "INFO"


def test_dry_run_needs_no_secrets():
    cfg = load_config({"DRY_RUN": "true"})
    assert cfg.dry_run is True
    assert cfg.discord_webhook_url == ""
    assert cfg.anthropic_api_key == ""


def test_missing_webhook_when_not_dry_run():
    with pytest.raises(ConfigError, match="DISCORD_WEBHOOK_URL"):
        load_config({"ANTHROPIC_API_KEY": API_KEY})


def test_missing_anthropic_key_required_by_default():
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        load_config({"DISCORD_WEBHOOK_URL": WEBHOOK})


def test_all_missing_listed():
    with pytest.raises(ConfigError) as ei:
        load_config({})
    assert "DISCORD_WEBHOOK_URL" in str(ei.value)
    assert "ANTHROPIC_API_KEY" in str(ei.value)


def test_require_anthropic_false_allows_missing_key():
    cfg = load_config({"DISCORD_WEBHOOK_URL": WEBHOOK}, require_anthropic=False)
    assert cfg.anthropic_api_key == ""


def test_require_anthropic_false_still_needs_webhook():
    with pytest.raises(ConfigError, match="DISCORD_WEBHOOK_URL"):
        load_config({}, require_anthropic=False)


def test_whitespace_only_secret_is_missing():
    with pytest.raises(ConfigError, match="DISCORD_WEBHOOK_URL"):
        load_config(full_env(DISCORD_WEBHOOK_URL="   "))


def test_secrets_are_stripped():
    cfg = load_config(full_env(DISCORD_WEBHOOK_URL=f"  {WEBHOOK}\n"))
    assert cfg.discord_webhook_url == WEBHOOK


def test_reads_os_environ_by_default(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK)
    monkeypatch.setenv("ANTHROPIC_API_KEY", API_KEY)
    monkeypatch.setenv("NEWS_TOTAL_COUNT", "7")
    assert load_config().news_total_count == 7


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", " True "])
def test_bool_true(raw):
    assert load_config(full_env(DRY_RUN=raw)).dry_run is True


@pytest.mark.parametrize("raw", ["0", "false", "No", "off", ""])
def test_bool_false(raw):
    assert load_config(full_env(DRY_RUN=raw)).dry_run is False


def test_bool_invalid():
    with pytest.raises(ConfigError, match="DRY_RUN"):
        load_config(full_env(DRY_RUN="maybe"))


def test_numeric_conversion():
    cfg = load_config(
        full_env(
            NEWS_TOTAL_COUNT=" 20 ",
            NEWS_GENERAL_RATIO="0.25",
            NEWS_MAX_AGE_HOURS="12",
            SUMMARY_MODEL=" my-model ",
            LOG_LEVEL="debug",
        )
    )
    assert cfg.news_total_count == 20
    assert cfg.news_general_ratio == 0.25
    assert cfg.news_max_age_hours == 12
    assert cfg.summary_model == "my-model"
    assert cfg.log_level == "DEBUG"


def test_empty_model_and_log_level_fall_back():
    cfg = load_config(full_env(SUMMARY_MODEL="  ", LOG_LEVEL=""))
    assert cfg.summary_model == DEFAULT_MODEL
    assert cfg.log_level == "INFO"


@pytest.mark.parametrize("ratio", ["0", "1", "0.0", "1.0"])
def test_ratio_boundaries_ok(ratio):
    assert load_config(full_env(NEWS_GENERAL_RATIO=ratio)).news_general_ratio == float(ratio)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("NEWS_TOTAL_COUNT", "abc"),
        ("NEWS_TOTAL_COUNT", "1.5"),
        ("NEWS_TOTAL_COUNT", "0"),
        ("NEWS_TOTAL_COUNT", "-3"),
        ("NEWS_TOTAL_COUNT", ""),
        ("NEWS_MAX_AGE_HOURS", "0"),
        ("NEWS_MAX_AGE_HOURS", "x"),
        ("NEWS_GENERAL_RATIO", "1.01"),
        ("NEWS_GENERAL_RATIO", "-0.1"),
        ("NEWS_GENERAL_RATIO", "half"),
    ],
)
def test_invalid_numbers(name, value):
    with pytest.raises(ConfigError, match=name):
        load_config(full_env(**{name: value}))


def test_repr_and_str_hide_secrets():
    cfg = load_config(full_env())
    for text in (repr(cfg), str(cfg)):
        assert "SECRET" not in text
        assert WEBHOOK not in text
        assert API_KEY not in text
    assert isinstance(cfg, Config)


def test_config_is_frozen():
    cfg = load_config(full_env())
    with pytest.raises(AttributeError):
        cfg.dry_run = True  # type: ignore[misc]


def test_error_messages_do_not_leak_secrets():
    env = full_env(NEWS_TOTAL_COUNT="bad", DRY_RUN="false")
    with pytest.raises(ConfigError) as ei:
        load_config(env)
    assert "SECRET" not in str(ei.value)
    with pytest.raises(ConfigError) as ei:
        load_config({"DISCORD_WEBHOOK_URL": WEBHOOK})
    assert "SECRET" not in str(ei.value)
    with pytest.raises(ConfigError) as ei:
        load_config(full_env(DRY_RUN=API_KEY))
    assert "SECRET" not in str(ei.value)


@pytest.mark.parametrize("bad", ["VERBOSE", "trace", "123", "info; rm"])
def test_invalid_log_level_falls_back_to_info(bad):
    assert load_config(full_env(LOG_LEVEL=bad)).log_level == "INFO"
