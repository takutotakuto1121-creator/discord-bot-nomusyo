import math

import pytest

from nomusyo.bot.cogs.admin import build_status_text, format_uptime


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "0分0秒"),
        (59.9, "0分59秒"),
        (61, "1分1秒"),
        (3600, "1時間0分0秒"),
        (3661, "1時間1分1秒"),
        (86400, "1日0時間0分0秒"),
        (90061, "1日1時間1分1秒"),
        (-5, "0分0秒"),
    ],
)
def test_format_uptime(seconds, expected):
    assert format_uptime(seconds) == expected


def test_build_status_text():
    text = build_status_text(uptime_seconds=3661, version="1.2.3", latency=0.0456, guild_count=2)
    assert "1時間1分1秒" in text
    assert "1.2.3" in text
    assert "46ms" in text
    assert "参加サーバー数: 2" in text


@pytest.mark.parametrize("latency", [math.nan, math.inf])
def test_build_status_text_latency_unavailable(latency):
    text = build_status_text(uptime_seconds=1, version="v", latency=latency, guild_count=0)
    assert "Gateway 遅延: -" in text
