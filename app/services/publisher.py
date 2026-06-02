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
    """Raised when a post cannot be published (oversized, auth, rate limit, …)."""


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

    The `x` branch is intentionally not implemented yet — it ships in Phase 4
    once the X Basic tier exists. Until then `PUBLISHER=dry` is the only path.
    """
    if settings.publisher == "x":
        raise NotImplementedError(
            "XPublisher is not implemented yet (Phase 4). Set PUBLISHER=dry, "
            "or provision the X Basic tier and add the XPublisher implementation."
        )
    return DryRunPublisher()
