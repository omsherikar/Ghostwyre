"""Phase 0 smoke tests for the publish abstraction."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.services.publisher import (
    DryRunPublisher,
    PublishError,
    get_publisher,
)


async def test_dry_run_publish_returns_fake_link() -> None:
    result = await DryRunPublisher().publish("hello world")
    assert result.dry_run is True
    assert result.url.startswith("https://")


async def test_dry_run_rejects_empty() -> None:
    with pytest.raises(PublishError):
        await DryRunPublisher().publish("   ")


async def test_dry_run_rejects_oversized() -> None:
    with pytest.raises(PublishError):
        await DryRunPublisher().publish("x" * 281)


async def test_dry_run_configurable_limit_accepts_long() -> None:
    # A raised limit (e.g. X Premium) lets a long post through.
    result = await DryRunPublisher(max_len=4000).publish("x" * 1000)
    assert result.dry_run is True


def test_factory_defaults_to_dry_run() -> None:
    assert isinstance(get_publisher(Settings(publisher="dry")), DryRunPublisher)


async def test_factory_threads_x_char_limit() -> None:
    # get_publisher wires settings.x_char_limit into the publisher, so a long post
    # is accepted when the limit is raised.
    publisher = get_publisher(Settings(publisher="dry", x_char_limit=5000))
    result = await publisher.publish("y" * 2000)
    assert result.dry_run is True


def test_factory_x_missing_creds_raises() -> None:
    # PUBLISHER=x with no credentials fails fast (before any tweepy import) with a
    # PublishError naming what's missing.
    with pytest.raises(PublishError):
        get_publisher(Settings(publisher="x"))
