"""Slack slash-command handlers.

`/draft-post` acks within Slack's 3-second window, then does the real work.
Phase 1 stops at logging a clean transcript; Phase 2 swaps the placeholder reply
for generated drafts.
"""

from __future__ import annotations

from typing import Any

from slack_bolt.async_app import AsyncApp

from app.config import get_settings
from app.logging import get_logger, redact
from app.slack.ingest import fetch_recent_messages, to_transcript

logger = get_logger(__name__)


def register(app: AsyncApp) -> None:
    @app.command("/draft-post")
    async def draft_post(ack: Any, command: dict[str, Any], client: Any, respond: Any) -> None:
        # 1) Ack immediately — Slack times out at 3s.
        await ack("on it… pulling recent messages 🧵")

        settings = get_settings()
        channel = command["channel_id"]

        # 2) Real work (we're already past the ack, so this can take its time).
        raw = await fetch_recent_messages(client, channel, settings.message_fetch_limit)
        transcript = to_transcript(raw)
        line_count = transcript.count("\n") + 1 if transcript else 0

        logger.info(
            "draft_post_ingested",
            channel=channel,
            raw=len(raw),
            kept=line_count,
            preview=redact(transcript, keep=60),  # never log the full transcript
        )

        if not transcript:
            await respond(
                "I couldn't find any usable messages in this channel. "
                "Make sure I've been invited here and there's recent conversation."
            )
            return

        await respond(
            f"Pulled *{line_count}* messages from this channel. "
            "Turning chat into drafts comes next (Phase 2)."
        )
