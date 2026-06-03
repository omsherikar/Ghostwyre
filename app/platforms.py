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
    strategy: str  # platform-native drafting guidance, injected into the draft prompt


_X_STRATEGY = """\
Write for X:
- A substantive post, not a teaser. Lead with the single sharpest line.
- Punchy and tight; favor short, high-contrast sentences.
- Make it conversation-first — a take people want to reply to beats a bland
  announcement. A developed long-form post is fine when the idea earns it.
- No hashtags; no emojis unless the voice guide allows them."""

_LINKEDIN_STRATEGY = """\
Write for LinkedIn:
- A developed, multi-paragraph post; roughly 1,200-1,800 characters reads best.
- Open with a strong hook — a personal-story angle or a contrarian take lands
  hardest. Earn the first line.
- Short paragraphs with whitespace between them; build the insight with concrete
  specifics, then close with a crisp takeaway.
- Professional, but keep the personality — let the voice show through."""


PLATFORMS: dict[str, PlatformSpec] = {
    "x": PlatformSpec(
        key="x", label="X", char_limit=MAX_TWEET_LEN, publishable=True, strategy=_X_STRATEGY
    ),
    "linkedin": PlatformSpec(
        key="linkedin",
        label="LinkedIn",
        char_limit=3000,
        publishable=False,
        strategy=_LINKEDIN_STRATEGY,
    ),
}
