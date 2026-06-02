"""Real X (Twitter) publishing via tweepy v2.

Kept out of `app/services/publisher.py` (which is always imported) so the dry-run
path never needs the optional `x` extra. `get_publisher` imports this module
lazily only when `PUBLISHER=x`. tweepy is synchronous, so the blocking
`create_tweet` call runs in a worker thread to keep the event loop free.

Failures are split into two buckets the approval handler relies on for
at-most-once publishing:
- definite (the post was NOT created) -> PublishError: safe to re-arm and retry.
- ambiguous (the post may or may not exist) -> PublishUnknownError: do not retry.

Never log the draft text — only the resulting tweet id.
"""

from __future__ import annotations

import asyncio
from typing import Any

import tweepy

from app.config import Settings
from app.logging import get_logger
from app.services.publisher import (
    PublishError,
    PublishResult,
    PublishUnknownError,
    _validate,
)

logger = get_logger(__name__)

# Resolves without knowing the authenticated handle; redirects to the canonical URL.
_STATUS_URL = "https://x.com/i/web/status/{id}"


def build_client(settings: Settings) -> tweepy.Client:
    """Construct an OAuth 1.0a user-context tweepy client from settings.

    The caller (`get_publisher`) guarantees all four credentials are present.
    """
    return tweepy.Client(
        consumer_key=settings.x_api_key,
        consumer_secret=settings.x_api_secret,
        access_token=settings.x_access_token,
        access_token_secret=settings.x_access_token_secret,
    )


def _is_duplicate(exc: Exception) -> bool:
    """True if a 403 is X's duplicate-content rejection (the post may already exist)."""
    return "duplicate" in str(exc).lower()


def _definite_message(exc: Exception) -> str:
    """Human-readable Slack message for a definite (nothing-posted) failure."""
    if isinstance(exc, tweepy.Unauthorized):
        return "X rejected the credentials (auth failed). Check the X_* tokens."
    if isinstance(exc, tweepy.Forbidden):
        return "X refused the post (a permissions or policy problem)."
    if isinstance(exc, tweepy.NotFound):
        return "X endpoint not found."
    return "X rejected the post (bad request)."


class XPublisher:
    """Publishes a draft to X via tweepy. The client is injected so tests can
    pass a fake and `build_client` stays the only place that needs credentials.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    async def publish(self, text: str) -> PublishResult:
        _validate(text)  # empty / >280 -> PublishError, before any network call
        try:
            resp = await asyncio.to_thread(self._client.create_tweet, text=text)
        except tweepy.Forbidden as exc:
            # A 403 "duplicate content" means an identical tweet already exists on
            # the account -> the content is (or may be) live. Treat as ambiguous
            # rather than re-arming, which could create a near-duplicate. Other 403s
            # (permissions/policy) are definite: nothing was posted.
            if _is_duplicate(exc):
                raise PublishUnknownError(
                    "X rejected this as a duplicate — an identical post may already "
                    "be live. Check X before retrying."
                ) from exc
            raise PublishError(_definite_message(exc)) from exc
        except (tweepy.BadRequest, tweepy.Unauthorized, tweepy.NotFound) as exc:
            # 4xx received without creating the tweet -> definitely not posted.
            raise PublishError(_definite_message(exc)) from exc
        except (tweepy.TooManyRequests, tweepy.TwitterServerError) as exc:
            # 429 / 5xx: the create may have succeeded server-side -> unknown.
            raise PublishUnknownError(
                "X didn't confirm the post (rate limit or server error) — check X before retrying."
            ) from exc
        except tweepy.TweepyException as exc:
            # Network/timeout/other -> also unknown; never auto-retry.
            raise PublishUnknownError(
                "X didn't confirm the post (network error) — check X before retrying."
            ) from exc

        tweet_id = resp.data["id"]
        logger.info("x_publish", tweet_id=str(tweet_id))  # id only, never text
        return PublishResult(url=_STATUS_URL.format(id=tweet_id), dry_run=False)
