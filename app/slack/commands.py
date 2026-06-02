"""Slack slash-command handlers.

`/draft-post` acks within Slack's 3-second window, then does the real work:
read the channel, gate on whether anything is postworthy, and reply with drafts.
Approval buttons / Block Kit are Phase 3 — for now we reply with plain text.
"""

from __future__ import annotations

from typing import Any

from slack_bolt.async_app import AsyncApp

from app.config import get_settings
from app.logging import get_logger
from app.services.content import generate_post_drafts, load_voice
from app.services.llm import build_client
from app.services.schemas import Draft
from app.slack.ingest import (
    fetch_recent_messages,
    resolve_user_names,
    to_transcript,
    unique_user_ids,
)

logger = get_logger(__name__)


def format_drafts(drafts: list[Draft]) -> str:
    """Render *drafts* as a numbered Slack mrkdwn reply. Pure — no I/O."""
    header = (
        f"Here are {len(drafts)} drafts — reply with the number you like "
        "(approval buttons come next):"
    )
    blocks = [f"*{i}.*\n{d.text}" for i, d in enumerate(drafts, start=1)]
    return header + "\n\n" + "\n\n".join(blocks)


def register(app: AsyncApp) -> None:
    # Build once at registration time, not per invocation.
    settings = get_settings()
    anthropic_client = build_client(settings)
    voice = load_voice()

    @app.command("/draft-post")
    async def draft_post(ack: Any, command: dict[str, Any], client: Any, respond: Any) -> None:
        # 1) Ack immediately — Slack times out at 3s.
        await ack("on it… reading the channel 🧵")

        channel = command["channel_id"]

        # 2) Real work (we're already past the ack, so this can take its time).
        raw = await fetch_recent_messages(client, channel, settings.message_fetch_limit)
        names = await resolve_user_names(client, unique_user_ids(raw))
        transcript = to_transcript(raw, user_names=names)
        line_count = transcript.count("\n") + 1 if transcript else 0

        logger.info("draft_post_ingested", channel=channel, raw=len(raw), kept=line_count)

        if not transcript:
            await respond(
                "I couldn't find any usable messages in this channel. "
                "Make sure I've been invited and there's recent conversation."
            )
            return

        try:
            result = await generate_post_drafts(
                transcript, voice, client=anthropic_client, settings=settings
            )
        except Exception:
            logger.exception("draft_post_generation_failed", channel=channel)
            await respond("Something went wrong while drafting — check the logs.")
            return

        if not result.postworthy:
            await respond(
                "Nothing here looks postworthy right now — try again after some real discussion."
            )
            return

        logger.info("draft_post_replied", channel=channel, draft_count=len(result.drafts))
        await respond(format_drafts(result.drafts))
