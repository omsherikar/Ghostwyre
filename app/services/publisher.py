"""Publishing abstraction.

The whole point of this interface is to keep the X (Twitter) API — which needs
a paid tier — off the critical path. The app codes against `PublisherClient`;
`DryRunPublisher` is used everywhere until the tier is provisioned, at which
point `XPublisher` (Phase 4, behind the `x` extra) drops in with no caller
changes. Selection is driven by the `PUBLISHER` setting.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.config import Settings
from app.logging import get_logger, redact

logger = get_logger(__name__)

MAX_TWEET_LEN = 280


class PublishError(Exception):
    """Raised when a post definitely was NOT published (oversized, auth, bad
    request, forbidden/duplicate). Safe for the caller to re-arm and retry —
    nothing went out."""


class PublishUnknownError(Exception):
    """Raised when a publish attempt's outcome is UNKNOWN (rate limit, server
    error, network/timeout) — the post may or may not exist. The caller must NOT
    auto-retry (that risks a double-post); it should surface the ambiguity and
    leave the batch claimed."""


@dataclass(frozen=True)
class PublishResult:
    url: str
    dry_run: bool


class PublisherClient(Protocol):
    async def publish(self, text: str) -> PublishResult: ...


def _validate(text: str) -> None:
    if not text.strip():
        raise PublishError("Refusing to publish an empty post.")
    if len(text) > MAX_TWEET_LEN:
        raise PublishError(f"Post is {len(text)} chars; the limit is {MAX_TWEET_LEN}.")


class DryRunPublisher:
    """Logs what *would* be posted and returns a fake link. No network calls."""

    async def publish(self, text: str) -> PublishResult:
        _validate(text)
        logger.info("dry_run_publish", preview=redact(text, keep=40))
        return PublishResult(url="https://x.com/_dryrun/status/0", dry_run=True)


def get_publisher(settings: Settings) -> PublisherClient:
    """Factory: returns the configured publisher.

    `PUBLISHER=dry` (default) returns the no-network `DryRunPublisher`.
    `PUBLISHER=x` returns the real `XPublisher` — but only if all four X
    credentials are set and the `x` extra (tweepy) is installed; otherwise a
    `PublishError` explains exactly what's missing. tweepy is imported lazily so
    the dry path never requires the optional dependency.
    """
    if settings.publisher == "x":
        missing = [
            name
            for name, value in (
                ("X_API_KEY", settings.x_api_key),
                ("X_API_SECRET", settings.x_api_secret),
                ("X_ACCESS_TOKEN", settings.x_access_token),
                ("X_ACCESS_TOKEN_SECRET", settings.x_access_token_secret),
            )
            if not value
        ]
        if missing:
            raise PublishError(
                "PUBLISHER=x but these credentials are missing: " + ", ".join(missing) + "."
            )
        try:
            from app.services.x_publisher import XPublisher, build_client
        except ModuleNotFoundError as exc:  # the `x` extra (tweepy) isn't installed
            raise PublishError(
                "PUBLISHER=x requires the `x` extra: `uv sync --extra x` "
                "(or `pip install ghostwyre[x]`)."
            ) from exc
        return XPublisher(build_client(settings))
    return DryRunPublisher()
