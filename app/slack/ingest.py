"""Slack message ingestion: fetch recent channel history, strip noise, and
normalize into a clean chronological transcript for the LLM.

The Slack API call (`fetch_recent_messages`) is kept separate from the pure
transform functions (`filter_messages`, `to_transcript`) so the filtering logic
is unit-testable against canned payloads with no network.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from app.logging import get_logger

logger = get_logger(__name__)

# conversations.history returns dicts; alias for readability.
RawMessage = dict[str, Any]


@dataclass(frozen=True)
class CleanMessage:
    user: str
    text: str


# Slack markup helpers --------------------------------------------------------
_LINK_RE = re.compile(r"<(https?://[^|>]+)\|([^>]+)>")  # <url|label> -> label
_BARE_LINK_RE = re.compile(r"<(https?://[^>]+)>")  # <url> -> url
_MENTION_RE = re.compile(r"<@([UW][A-Z0-9]+)(?:\|([^>]+))?>")  # <@U..|name> -> @name
_CHANNEL_RE = re.compile(r"<#[CG][A-Z0-9]+\|([^>]+)>")  # <#C..|chan> -> #chan
_SPECIAL_RE = re.compile(r"<!(\w+)>")  # <!here>, <!channel> -> @here


def demarkup(text: str, user_names: dict[str, str] | None = None) -> str:
    """Convert Slack's angle-bracket markup into plain, readable text."""
    names = user_names or {}
    text = _LINK_RE.sub(r"\2", text)
    text = _BARE_LINK_RE.sub(r"\1", text)
    text = _MENTION_RE.sub(lambda m: "@" + (m.group(2) or names.get(m.group(1), m.group(1))), text)
    text = _CHANNEL_RE.sub(r"#\1", text)
    text = _SPECIAL_RE.sub(r"@\1", text)
    return text.strip()


def filter_messages(
    messages: list[RawMessage],
    *,
    bot_user_id: str | None = None,
) -> list[RawMessage]:
    """Drop noise: any message with a subtype (joins/leaves/topic changes/bot
    posts), anything posted by a bot, our own bot's messages, and empty text.
    """
    kept: list[RawMessage] = []
    for m in messages:
        if m.get("subtype"):  # channel_join, channel_leave, bot_message, etc.
            continue
        if m.get("bot_id"):  # anything posted by a bot (incl. our own replies)
            continue
        user = m.get("user")
        if not user:
            continue
        if bot_user_id and user == bot_user_id:
            continue
        if not (m.get("text") or "").strip():
            continue
        kept.append(m)
    return kept


def to_transcript(
    messages: list[RawMessage],
    *,
    user_names: dict[str, str] | None = None,
    bot_user_id: str | None = None,
) -> str:
    """Build a chronological `name: text` transcript from raw history.

    conversations.history returns newest-first, so we reverse to chronological.
    """
    names = user_names or {}
    kept = filter_messages(messages, bot_user_id=bot_user_id)
    cleaned: list[CleanMessage] = []
    for m in reversed(kept):
        user_id = str(m["user"])
        cleaned.append(
            CleanMessage(
                user=names.get(user_id, user_id),
                text=demarkup(str(m["text"]), names),
            )
        )
    return "\n".join(f"{c.user}: {c.text}" for c in cleaned if c.text)


def unique_user_ids(messages: list[RawMessage]) -> list[str]:
    """Return the unique, non-empty `user` ids in *messages*, order-preserving."""
    seen: dict[str, None] = {}
    for m in messages:
        user = m.get("user")
        if user:
            seen.setdefault(str(user), None)
    return list(seen)


def _best_name(profile_resp: dict[str, Any], user_id: str) -> str:
    """Pick the best display name from a users.info response, falling back to id."""
    user = profile_resp.get("user") or {}
    profile = user.get("profile") or {}
    display = (profile.get("display_name") or "").strip()
    if display:
        return display
    real = (profile.get("real_name") or "").strip()
    if real:
        return real
    name = (user.get("name") or "").strip()
    if name:
        return name
    return user_id


async def resolve_user_names(client: Any, user_ids: Iterable[str]) -> dict[str, str]:
    """Resolve Slack user ids to display names via users.info.

    De-duplicates ids before calling. For each id, prefers the profile's
    `display_name`, then `real_name`, then `name`, then the id itself. A failed
    lookup for one id falls back to that id and never breaks the batch.
    """
    names: dict[str, str] = {}
    for user_id in dict.fromkeys(user_ids):
        try:
            resp = await client.users_info(user=user_id)
            names[user_id] = _best_name(dict(resp), user_id)
        except Exception:  # noqa: BLE001 — one bad lookup must not break the batch
            logger.warning("resolve_user_name_failed", user_id=user_id)
            names[user_id] = user_id
    return names


async def fetch_recent_messages(client: Any, channel: str, limit: int) -> list[RawMessage]:
    """Pull the last `limit` messages from a channel via conversations.history."""
    resp = await client.conversations_history(channel=channel, limit=limit)
    messages: list[RawMessage] = resp.get("messages", [])
    return messages
