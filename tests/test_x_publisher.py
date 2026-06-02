"""Phase 4 tests for the real X publisher.

No network: a hand-rolled FakeTweepyClient stands in for tweepy.Client, and real
tweepy exception instances drive the definite-vs-ambiguous error mapping. tweepy
is a dev dependency so its exception classes are importable here.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import tweepy

from app.config import Settings
from app.services.publisher import PublishError, PublishUnknownError, get_publisher
from app.services.x_publisher import XPublisher


class FakeTweepyClient:
    """Stand-in for tweepy.Client: records create_tweet calls; returns a canned
    response or raises a configured exception."""

    def __init__(self, *, result: Any = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[str] = []

    def create_tweet(self, *, text: str) -> Any:
        self.calls.append(text)
        if self.error is not None:
            raise self.error
        return self.result


def _http_exc(cls: type[tweepy.HTTPException], status: int) -> tweepy.HTTPException:
    """Build a real tweepy HTTPException subclass without a live response."""
    resp = SimpleNamespace(status_code=status, reason=cls.__name__)
    return cls(resp, response_json={})


def _ok_response(tweet_id: str) -> SimpleNamespace:
    return SimpleNamespace(data={"id": tweet_id})


async def test_publish_success_returns_live_url() -> None:
    fake = FakeTweepyClient(result=_ok_response("1799999999999999999"))
    result = await XPublisher(fake).publish("hello from a test")
    assert result.dry_run is False
    assert "1799999999999999999" in result.url
    assert fake.calls == ["hello from a test"]


async def test_publish_empty_rejected_before_api() -> None:
    fake = FakeTweepyClient(result=_ok_response("1"))
    with pytest.raises(PublishError):
        await XPublisher(fake).publish("   ")
    assert fake.calls == []  # _validate runs before create_tweet


async def test_publish_oversized_rejected_before_api() -> None:
    fake = FakeTweepyClient(result=_ok_response("1"))
    with pytest.raises(PublishError):
        await XPublisher(fake).publish("x" * 281)
    assert fake.calls == []


@pytest.mark.parametrize(
    ("exc_cls", "status"),
    [
        (tweepy.BadRequest, 400),
        (tweepy.Unauthorized, 401),
        (tweepy.Forbidden, 403),
        (tweepy.NotFound, 404),
    ],
)
async def test_definite_failures_map_to_publish_error(
    exc_cls: type[tweepy.HTTPException], status: int
) -> None:
    fake = FakeTweepyClient(error=_http_exc(exc_cls, status))
    with pytest.raises(PublishError):
        await XPublisher(fake).publish("a real post")


@pytest.mark.parametrize(
    "error",
    [
        _http_exc(tweepy.TooManyRequests, 429),
        _http_exc(tweepy.TwitterServerError, 503),
        tweepy.TweepyException("network boom"),
    ],
)
async def test_ambiguous_failures_map_to_unknown(error: Exception) -> None:
    fake = FakeTweepyClient(error=error)
    with pytest.raises(PublishUnknownError):
        await XPublisher(fake).publish("a real post")


def test_get_publisher_x_returns_xpublisher() -> None:
    settings = Settings(
        publisher="x",
        x_api_key="key",
        x_api_secret="secret",
        x_access_token="token",
        x_access_token_secret="token-secret",
    )
    assert isinstance(get_publisher(settings), XPublisher)
