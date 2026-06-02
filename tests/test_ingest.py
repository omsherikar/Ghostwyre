"""Tests for the noise filter + transcript normalization (no network)."""

from __future__ import annotations

from app.slack.ingest import demarkup, filter_messages, to_transcript

# A realistic slice of conversations.history (newest-first, as Slack returns it).
# Slack user IDs are uppercase alphanumerics (no underscores), e.g. U01ABC2DEF.
HISTORY = [
    {"type": "message", "user": "U01ALICE", "text": "shipped the new onboarding flow 🚀"},
    {"type": "message", "subtype": "channel_join", "user": "U02BOB", "text": "has joined"},
    {"type": "message", "bot_id": "B0GHOST", "text": "on it… pulling messages"},
    {"type": "message", "user": "U02BOB", "text": "nice, signups up 12%"},
    {"type": "message", "user": "U01ALICE", "text": "   "},  # empty after strip
    {"type": "message", "user": "U01ALICE", "text": "hit a gnarly <@U02BOB> race condition first"},
]
NAMES = {"U01ALICE": "alice", "U02BOB": "bob"}


def test_filter_drops_subtypes_bots_and_empty() -> None:
    kept = filter_messages(HISTORY)
    texts = [m["text"] for m in kept]
    assert texts == [
        "shipped the new onboarding flow 🚀",
        "nice, signups up 12%",
        "hit a gnarly <@U02BOB> race condition first",
    ]


def test_filter_drops_own_bot_user() -> None:
    msgs = [
        {"user": "U0ME", "text": "ignore me"},
        {"user": "U01ALICE", "text": "keep me"},
    ]
    kept = filter_messages(msgs, bot_user_id="U0ME")
    assert [m["text"] for m in kept] == ["keep me"]


def test_transcript_is_chronological_and_named() -> None:
    transcript = to_transcript(HISTORY, user_names=NAMES)
    assert transcript == (
        "alice: hit a gnarly @bob race condition first\n"
        "bob: nice, signups up 12%\n"
        "alice: shipped the new onboarding flow 🚀"
    )


def test_transcript_empty_when_all_noise() -> None:
    noise = [
        {"subtype": "channel_join", "user": "U_X", "text": "joined"},
        {"bot_id": "B1", "text": "beep"},
    ]
    assert to_transcript(noise) == ""


def test_demarkup_links_mentions_channels() -> None:
    raw = "see <https://x.com/post|the post> from <@U01ALICE> in <#C123|general> <!here>"
    assert demarkup(raw, NAMES) == "see the post from @alice in #general @here"
