import pytest
import requests

from nomusyo.discord_io import webhook
from nomusyo.discord_io.webhook import (
    MAX_RETRIES,
    WebhookError,
    post_webhook,
    send_message,
)

URL = "https://discord.com/api/webhooks/123/SECRET-TOKEN-WEBHOOK"


class FakeResp:
    def __init__(self, status=204, json_data=None, headers=None, json_raises=False):
        self.status_code = status
        self._json = json_data
        self.headers = headers or {}
        self._raises = json_raises

    def json(self):
        if self._raises:
            raise ValueError("no json")
        return self._json


class Poster:
    """Replacement for requests.post returning/raising queued items."""

    def __init__(self, items):
        self.items = list(items)
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        item = self.items.pop(0) if len(self.items) > 1 else self.items[0]
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture
def sleeps():
    return []


def run(monkeypatch, items, sleeps, payload=None, **kw):
    poster = Poster(items)
    monkeypatch.setattr(webhook.requests, "post", poster)
    post_webhook(URL, payload or {"content": "hi"}, sleep=sleeps.append, **kw)
    return poster


def fail(monkeypatch, items, sleeps, **kw):
    poster = Poster(items)
    monkeypatch.setattr(webhook.requests, "post", poster)
    with pytest.raises(WebhookError) as ei:
        post_webhook(URL, {"content": "hi"}, sleep=sleeps.append, **kw)
    return poster, ei.value


def test_success_single_call(monkeypatch, sleeps):
    poster = run(monkeypatch, [FakeResp(204)], sleeps)
    assert len(poster.calls) == 1
    url, kw = poster.calls[0]
    assert url == URL
    assert kw["params"] == {"wait": "true"}
    assert kw["timeout"] == webhook.DEFAULT_TIMEOUT
    assert sleeps == []


def test_custom_timeout_passed(monkeypatch, sleeps):
    poster = run(monkeypatch, [FakeResp(200)], sleeps, timeout=3.5)
    assert poster.calls[0][1]["timeout"] == 3.5


def test_allowed_mentions_default_and_override(monkeypatch, sleeps):
    poster = run(monkeypatch, [FakeResp(204)], sleeps)
    assert poster.calls[0][1]["json"] == {
        "allowed_mentions": {"parse": []},
        "content": "hi",
    }
    custom = {"parse": ["users"]}
    poster = run(
        monkeypatch, [FakeResp(204)], sleeps, payload={"content": "x", "allowed_mentions": custom}
    )
    assert poster.calls[0][1]["json"]["allowed_mentions"] == custom


def test_payload_not_mutated(monkeypatch, sleeps):
    payload = {"content": "hi"}
    run(monkeypatch, [FakeResp(204)], sleeps, payload=payload)
    assert payload == {"content": "hi"}


def test_send_message(monkeypatch, sleeps):
    poster = Poster([FakeResp(204)])
    monkeypatch.setattr(webhook.requests, "post", poster)
    send_message(URL, "hello", sleep=sleeps.append)
    assert poster.calls[0][1]["json"]["content"] == "hello"


def test_429_json_retry_after_preferred(monkeypatch, sleeps):
    r429 = FakeResp(429, json_data={"retry_after": 2.5}, headers={"Retry-After": "9"})
    poster = run(monkeypatch, [r429, FakeResp(204)], sleeps)
    assert len(poster.calls) == 2
    assert sleeps == [2.5]


def test_429_header_fallback(monkeypatch, sleeps):
    r429 = FakeResp(429, headers={"Retry-After": "4"}, json_raises=True)
    run(monkeypatch, [r429, FakeResp(204)], sleeps)
    assert sleeps == [4.0]


def test_429_json_without_retry_after_uses_header(monkeypatch, sleeps):
    r429 = FakeResp(429, json_data={"message": "rate"}, headers={"Retry-After": "3"})
    run(monkeypatch, [r429, FakeResp(204)], sleeps)
    assert sleeps == [3.0]


def test_429_no_info_defaults_to_one_second(monkeypatch, sleeps):
    r429 = FakeResp(429, json_raises=True)
    run(monkeypatch, [r429, FakeResp(204)], sleeps)
    assert sleeps == [1.0]


def test_429_capped_at_60(monkeypatch, sleeps):
    r429 = FakeResp(429, json_data={"retry_after": 9999})
    run(monkeypatch, [r429, FakeResp(204)], sleeps)
    assert sleeps == [60.0]


def test_429_negative_clamped_to_zero(monkeypatch, sleeps):
    r429 = FakeResp(429, json_data={"retry_after": -5})
    run(monkeypatch, [r429, FakeResp(204)], sleeps)
    assert sleeps == [0.0]


def test_429_exhausts_retries(monkeypatch, sleeps):
    poster, err = fail(monkeypatch, [FakeResp(429, json_data={"retry_after": 1})], sleeps)
    assert len(poster.calls) == MAX_RETRIES + 1
    assert len(sleeps) == MAX_RETRIES
    assert "429" in str(err)


def test_5xx_exponential_backoff_then_success(monkeypatch, sleeps):
    items = [FakeResp(500), FakeResp(502), FakeResp(503), FakeResp(204)]
    poster = run(monkeypatch, items, sleeps)
    assert len(poster.calls) == 4
    assert sleeps == [1.0, 2.0, 4.0]


def test_5xx_exhausts_retries(monkeypatch, sleeps):
    poster, err = fail(monkeypatch, [FakeResp(500)], sleeps)
    assert len(poster.calls) == MAX_RETRIES + 1
    assert sleeps == [1.0, 2.0, 4.0]
    assert "500" in str(err)


def test_max_retries_custom_and_zero(monkeypatch, sleeps):
    poster, _ = fail(monkeypatch, [FakeResp(500)], sleeps, max_retries=1)
    assert len(poster.calls) == 2
    assert sleeps == [1.0]
    sleeps.clear()
    poster, _ = fail(monkeypatch, [FakeResp(500)], sleeps, max_retries=0)
    assert len(poster.calls) == 1
    assert sleeps == []


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413])
def test_other_4xx_fail_immediately(monkeypatch, sleeps, status):
    poster, err = fail(monkeypatch, [FakeResp(status)], sleeps)
    assert len(poster.calls) == 1
    assert sleeps == []
    assert str(status) in str(err)


def test_network_error_retried_then_success(monkeypatch, sleeps):
    items = [requests.ConnectionError("boom"), requests.Timeout("slow"), FakeResp(204)]
    poster = run(monkeypatch, items, sleeps)
    assert len(poster.calls) == 3
    assert sleeps == [1.0, 2.0]


def test_network_error_exhausts_retries(monkeypatch, sleeps):
    poster, err = fail(monkeypatch, [requests.ConnectionError("boom")], sleeps)
    assert len(poster.calls) == MAX_RETRIES + 1
    assert sleeps == [1.0, 2.0, 4.0]
    assert "ConnectionError" in str(err)


def test_url_not_leaked_from_network_error(monkeypatch, sleeps):
    exc = requests.ConnectionError(f"HTTPSConnectionPool failed for url: {URL}")
    _, err = fail(monkeypatch, [exc], sleeps)
    assert "SECRET" not in str(err)
    assert URL not in str(err)
    assert "SECRET" not in repr(err)
    assert err.__cause__ is None


@pytest.mark.parametrize("resp", [FakeResp(500), FakeResp(429), FakeResp(404)])
def test_url_not_leaked_from_http_errors(monkeypatch, sleeps, resp):
    _, err = fail(monkeypatch, [resp], sleeps)
    assert "SECRET" not in str(err)
    assert err.__cause__ is None


def test_no_real_sleep(monkeypatch, sleeps):
    def boom(_):
        raise AssertionError("real sleep used")

    monkeypatch.setattr(webhook.time, "sleep", boom)
    # default sleep is bound at definition time, so only the injected one is safe
    run(monkeypatch, [FakeResp(500), FakeResp(204)], sleeps)
    assert sleeps == [1.0]


# ---- timeouts ----


def test_read_timeout_is_not_retried_and_hides_url(monkeypatch, sleeps):
    exc = requests.ReadTimeout(f"HTTPSConnectionPool read timed out for url: {URL}")
    poster, err = fail(monkeypatch, [exc], sleeps)
    assert len(poster.calls) == 1
    assert sleeps == []
    assert URL not in str(err) and "SECRET-TOKEN-WEBHOOK" not in str(err)
    assert err.__cause__ is None and err.__suppress_context__


def test_read_timeout_after_earlier_retry_still_not_retried(monkeypatch, sleeps):
    poster, _ = fail(monkeypatch, [FakeResp(500), requests.ReadTimeout("t")], sleeps)
    assert len(poster.calls) == 2


def test_connect_timeout_is_retried(monkeypatch, sleeps):
    poster = run(monkeypatch, [requests.ConnectTimeout("c"), FakeResp(204)], sleeps)
    assert len(poster.calls) == 2
    assert sleeps == [1.0]


def test_connect_timeout_exhausts_retries(monkeypatch, sleeps):
    poster, err = fail(monkeypatch, [requests.ConnectTimeout(f"x {URL}")], sleeps)
    assert len(poster.calls) == MAX_RETRIES + 1
    assert "ConnectTimeout" in str(err)
    assert URL not in str(err)


def test_connection_error_is_retried(monkeypatch, sleeps):
    poster = run(
        monkeypatch,
        [requests.ConnectionError("a"), requests.ConnectionError("b"), FakeResp(204)],
        sleeps,
    )
    assert len(poster.calls) == 3
    assert sleeps == [1.0, 2.0]
