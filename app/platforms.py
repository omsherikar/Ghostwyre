"""Per-platform draft config: the single source of truth for labels, char
limits, and publishability, keyed by the same strings as `DraftPlatform`.

It lives in a neutral module (not in `blocks.py`) so both the Slack layer
(`blocks.py` — labels/limits/Approve gating) and `publisher.py` can read it
without a Slack→publisher import cycle.

The X `char_limit` here is only a nominal default — the *effective* X limit at
runtime is `Settings.x_char_limit` (raise it for X Premium long posts). LinkedIn
is copy-paste only (no API publisher), so its limit is purely for display.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.publisher import MAX_TWEET_LEN


@dataclass(frozen=True)
class PlatformSpec:
    key: str  # matches DraftPlatform values
    label: str
    char_limit: int
    publishable: bool


PLATFORMS: dict[str, PlatformSpec] = {
    "x": PlatformSpec(key="x", label="X", char_limit=MAX_TWEET_LEN, publishable=True),
    "linkedin": PlatformSpec(key="linkedin", label="LinkedIn", char_limit=3000, publishable=False),
}
