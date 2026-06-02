"""Slack message ingestion: fetch recent channel history, strip noise, and
normalize into a clean chronological transcript for the LLM.

The Slack API call (`fetch_recent_messages`) is kept separate from the pure
transform functions (`filter_messages`, `to_transcript`) so the filtering logic
is unit-testable against canned payloads with no network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

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


async def fetch_recent_messages(client: Any, channel: str, limit: int) -> list[RawMessage]:
    """Pull the last `limit` messages from a channel via conversations.history."""
    resp = await client.conversations_history(channel=channel, limit=limit)
    messages: list[RawMessage] = resp.get("messages", [])
    return messages
