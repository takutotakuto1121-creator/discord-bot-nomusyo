import dataclasses

import pytest

from nomusyo.bot.config import BotConfig, load_bot_config
from nomusyo.config import ConfigError

TOKEN = "MTIzNDU2.SECRET-BOT-TOKEN.value"
HEARTBEAT = "https://hc-ping.example.com/SECRET-HEARTBEAT-UUID"
API_KEY = "sk-ant-SECRET-KEY-VALUE"


def full_env(**over):
    env = {
        "DISCORD_BOT_TOKEN": TOKEN,
        "DISCORD_GUILD_ID": "111",
        "ALLOWED_USER_IDS": "222",
        "NOTIFY_CHANNEL_ID": "333",
        "ANTHROPIC_API_KEY": API_KEY,
    }
    env.update(over)
    return {k: v for k, v in env.items() if v is not None}


def test_defaults():
    cfg = load_bot_config(full_env())
    assert cfg.discord_bot_token == TOKEN
    assert cfg.guild_id == 111
    assert cfg.allowed_user_ids == frozenset({222})
    assert cfg.notify_channel_id == 333
    assert cfg.admin_user_id == 222
    assert cfg.heartbeat_url == ""
    assert cfg.log_level == "INFO"
    assert cfg.news.anthropic_api_key == API_KEY


@pytest.mark.parametrize(
    "name",
    [
        "DISCORD_BOT_TOKEN",
        "DISCORD_GUILD_ID",
        "ALLOWED_USER_IDS",
        "NOTIFY_CHANNEL_ID",
        "ANTHROPIC_API_KEY",
    ],
)
def test_missing_required(name):
    with pytest.raises(ConfigError, match=name):
        load_bot_config(full_env(**{name: None}))


@pytest.mark.parametrize(
    "name",
    ["DISCORD_BOT_TOKEN", "DISCORD_GUILD_ID", "ALLOWED_USER_IDS", "NOTIFY_CHANNEL_ID"],
)
def test_blank_required_is_missing(name):
    with pytest.raises(ConfigError, match=name):
        load_bot_config(full_env(**{name: "   "}))


def test_all_missing_listed():
    with pytest.raises(ConfigError) as ei:
        load_bot_config({})
    for name in (
        "DISCORD_BOT_TOKEN",
        "DISCORD_GUILD_ID",
        "ALLOWED_USER_IDS",
        "NOTIFY_CHANNEL_ID",
        "ANTHROPIC_API_KEY",
    ):
        assert name in str(ei.value)


@pytest.mark.parametrize("name", ["DISCORD_GUILD_ID", "NOTIFY_CHANNEL_ID", "ADMIN_USER_ID"])
@pytest.mark.parametrize("value", ["abc", "1.5", "12x"])
def test_non_integer_id(name, value):
    with pytest.raises(ConfigError, match=name):
        load_bot_config(full_env(**{name: value}))


@pytest.mark.parametrize("name", ["DISCORD_GUILD_ID", "NOTIFY_CHANNEL_ID", "ADMIN_USER_ID"])
@pytest.mark.parametrize("value", ["0", "-5"])
def test_non_positive_id(name, value):
    with pytest.raises(ConfigError, match=name):
        load_bot_config(full_env(**{name: value}))


def test_allowed_ids_comma_separated():
    cfg = load_bot_config(full_env(ALLOWED_USER_IDS=" 5 , 6,7 ,,"))
    assert cfg.allowed_user_ids == frozenset({5, 6, 7})


def test_allowed_ids_only_commas_is_missing_or_empty():
    with pytest.raises(ConfigError, match="ALLOWED_USER_IDS"):
        load_bot_config(full_env(ALLOWED_USER_IDS=" , ,"))


@pytest.mark.parametrize("value", ["1,abc", "1,0", "1,-2", "1.5"])
def test_allowed_ids_invalid(value):
    with pytest.raises(ConfigError, match="ALLOWED_USER_IDS"):
        load_bot_config(full_env(ALLOWED_USER_IDS=value))


def test_admin_defaults_to_first_allowed_in_order():
    cfg = load_bot_config(full_env(ALLOWED_USER_IDS="900, 5, 7"))
    assert cfg.admin_user_id == 900


def test_admin_explicit_wins():
    cfg = load_bot_config(full_env(ALLOWED_USER_IDS="900,5", ADMIN_USER_ID="42"))
    assert cfg.admin_user_id == 42


def test_blank_admin_falls_back_to_first_allowed():
    cfg = load_bot_config(full_env(ALLOWED_USER_IDS="9,5", ADMIN_USER_ID=" "))
    assert cfg.admin_user_id == 9


def test_heartbeat_https_ok():
    assert load_bot_config(full_env(BOT_HEARTBEAT_URL=HEARTBEAT)).heartbeat_url == HEARTBEAT


@pytest.mark.parametrize(
    "url", ["http://hc.example.com/SECRET-HEARTBEAT-UUID", "ftp://x/SECRET", "hc.example.com/x"]
)
def test_heartbeat_non_https_rejected_without_leaking(url):
    with pytest.raises(ConfigError, match="BOT_HEARTBEAT_URL") as ei:
        load_bot_config(full_env(BOT_HEARTBEAT_URL=url))
    assert "SECRET" not in str(ei.value)
    assert url not in str(ei.value)


def test_news_config_needs_no_webhook():
    cfg = load_bot_config(full_env())  # no DISCORD_WEBHOOK_URL
    assert cfg.news.discord_webhook_url == ""
    assert cfg.news.dry_run is False


def test_webhook_url_not_carried_into_news():
    cfg = load_bot_config(full_env(DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/1/WH"))
    assert cfg.news.discord_webhook_url == ""


def test_dry_run_env_is_ignored_for_news():
    cfg = load_bot_config(full_env(DRY_RUN="true"))
    assert cfg.news.dry_run is False


def test_news_settings_and_log_level_passed_through():
    cfg = load_bot_config(full_env(NEWS_TOTAL_COUNT="4", LOG_LEVEL="debug"))
    assert cfg.news.news_total_count == 4
    assert cfg.log_level == "DEBUG"


def test_invalid_news_setting_raises():
    with pytest.raises(ConfigError, match="NEWS_TOTAL_COUNT"):
        load_bot_config(full_env(NEWS_TOTAL_COUNT="0"))


def test_secrets_not_in_repr_or_str():
    cfg = load_bot_config(full_env(BOT_HEARTBEAT_URL=HEARTBEAT))
    for text in (repr(cfg), str(cfg), repr(cfg.news), str(cfg.news)):
        assert TOKEN not in text
        assert "SECRET" not in text
        assert HEARTBEAT not in text
        assert API_KEY not in text


@pytest.mark.parametrize(
    "over",
    [
        {"DISCORD_GUILD_ID": "bad"},
        {"ALLOWED_USER_IDS": "x"},
        {"NOTIFY_CHANNEL_ID": "-1"},
        {"ADMIN_USER_ID": "nope"},
        {"BOT_HEARTBEAT_URL": "http://insecure/SECRET-HEARTBEAT-UUID"},
        {"DISCORD_GUILD_ID": None},
        {"ANTHROPIC_API_KEY": None},
    ],
)
def test_error_messages_do_not_leak_secrets(over):
    with pytest.raises(ConfigError) as ei:
        load_bot_config(full_env(**over))
    msg = str(ei.value)
    assert TOKEN not in msg
    assert API_KEY not in msg
    assert "SECRET" not in msg


def test_config_is_frozen():
    cfg = load_bot_config(full_env())
    assert isinstance(cfg, BotConfig)
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.guild_id = 1  # type: ignore[misc]


# ---- schedule / storage related settings -------------------------------------------------
def test_new_setting_defaults():
    from datetime import time
    from pathlib import Path

    cfg = load_bot_config(full_env())
    assert cfg.db_path == Path("/data/nomusyo.db")
    assert cfg.tz_default == "Asia/Tokyo"
    assert cfg.news_post_time == time(7, 0)
    assert cfg.news_post_enabled is False
    assert cfg.admin_channel_id is None


def test_digest_time_default():
    from datetime import time

    assert load_bot_config(full_env()).digest_time == time(6, 55)


def test_new_settings_override():
    from datetime import time
    from pathlib import Path

    cfg = load_bot_config(
        full_env(
            DB_PATH="/tmp/x.db",
            TZ_DEFAULT="America/New_York",
            DIGEST_TIME="8:05",
            NEWS_POST_TIME="21:30",
            NEWS_POST_ENABLED="true",
            ADMIN_CHANNEL_ID="444",
        )
    )
    assert cfg.db_path == Path("/tmp/x.db")
    assert cfg.tz_default == "America/New_York"
    assert cfg.digest_time == time(8, 5)
    assert cfg.news_post_time == time(21, 30)
    assert cfg.news_post_enabled is True
    assert cfg.admin_channel_id == 444


def test_blank_new_settings_use_defaults():
    from datetime import time

    cfg = load_bot_config(
        full_env(
            DB_PATH=" ", TZ_DEFAULT="", DIGEST_TIME=" ", NEWS_POST_TIME="", ADMIN_CHANNEL_ID=" "
        )
    )
    assert cfg.tz_default == "Asia/Tokyo"
    assert cfg.news_post_time == time(7, 0)
    assert cfg.admin_channel_id is None
    assert str(cfg.db_path) == "/data/nomusyo.db"


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on"])
def test_news_post_enabled_true(raw):
    assert load_bot_config(full_env(NEWS_POST_ENABLED=raw)).news_post_enabled is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", ""])
def test_news_post_enabled_false(raw):
    assert load_bot_config(full_env(NEWS_POST_ENABLED=raw)).news_post_enabled is False


@pytest.mark.parametrize(
    ("name", "bad"),
    [
        ("TZ_DEFAULT", "Mars/Secret-Zone"),
        ("DIGEST_TIME", "25:00"),
        ("DIGEST_TIME", "7"),
        ("DIGEST_TIME", "07:60"),
        ("DIGEST_TIME", "ab:cd"),
        ("NEWS_POST_TIME", "24:00"),
        ("NEWS_POST_TIME", "7:00:00"),
        ("NEWS_POST_ENABLED", "maybe-secret"),
        ("ADMIN_CHANNEL_ID", "abc-secret"),
        ("ADMIN_CHANNEL_ID", "0"),
        ("ADMIN_CHANNEL_ID", "-5"),
    ],
)
def test_invalid_new_settings_name_only(name, bad):
    with pytest.raises(ConfigError) as ei:
        load_bot_config(full_env(**{name: bad}))
    msg = str(ei.value)
    assert name in msg
    assert bad not in msg
    assert TOKEN not in msg and API_KEY not in msg


def test_backward_compatible_without_new_vars():
    cfg = load_bot_config(full_env())
    assert cfg.guild_id == 111 and cfg.notify_channel_id == 333
    assert cfg.admin_user_id == 222


def test_owner_user_id_is_first_allowed():
    cfg = load_bot_config(full_env(ALLOWED_USER_IDS="999, 222,111"))
    assert cfg.owner_user_id == 999
    assert cfg.admin_user_id == 999
    assert cfg.allowed_user_ids == frozenset({999, 222, 111})


def test_owner_independent_of_admin_override():
    cfg = load_bot_config(full_env(ALLOWED_USER_IDS="999,222", ADMIN_USER_ID="222"))
    assert cfg.owner_user_id == 999 and cfg.admin_user_id == 222


def test_owner_skips_empty_entries():
    assert load_bot_config(full_env(ALLOWED_USER_IDS=" , 555,666")).owner_user_id == 555


@pytest.mark.parametrize("bad", ["VERBOSE", "trace", "123", "info; rm"])
def test_invalid_log_level_falls_back_to_info(bad):
    assert load_bot_config(full_env(LOG_LEVEL=bad)).log_level == "INFO"
