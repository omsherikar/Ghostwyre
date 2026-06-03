"""Slack slash-command handlers.

`/draft-post` acks within Slack's 3-second window, then does the real work:
read the channel, gate on whether anything is postworthy, and — when there's
something to post — persist the batch and post one living Block Kit approval
card in-channel (Phase 3). Persist BEFORE posting so the handlers can resolve
everything by id, then record the card's message ts on the batch.

Never logs the transcript or draft text (CONFIDENTIAL at rest) — ids/counts only.
"""

from __future__ import annotations

from typing import Any

from slack_bolt.async_app import AsyncApp
from slack_sdk.errors import SlackApiError

from app import repo
from app.config import get_settings
from app.db import SessionLocal
from app.db.models import BatchStatus, DraftPlatform
from app.logging import get_logger
from app.services.content import build_voices, generate_idea_drafts, load_voice, rank_channel_ideas
from app.services.llm import build_client
from app.services.schemas import Draft
from app.slack.blocks import (
    build_draft_blocks,
    build_history_blocks,
    build_idea_blocks,
    fallback_text,
    history_fallback,
    idea_fallback_text,
)
from app.slack.ingest import (
    fetch_recent_messages,
    resolve_user_names,
    to_transcript,
    unique_user_ids,
)

logger = get_logger(__name__)

# Slack web errors that mean "I'm not in this channel" — actionable for the user.
_NOT_IN_CHANNEL_ERRORS = {"not_in_channel", "channel_not_found", "is_archived"}


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
        raw = await fetch_recent_messages(client, channel, settings.idea_scan_limit)
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

        # Scan + rank the channel's candidate ideas: a cheap postworthy gate, then a
        # scored/deduped/ranked shortlist with evidence (no drafting yet).
        try:
            ranking = await rank_channel_ideas(
                transcript, client=anthropic_client, settings=settings
            )
        except Exception:
            logger.exception("draft_post_ranking_failed", channel=channel)
            await respond("Something went wrong while reading the channel — check the logs.")
            return

        if not ranking.postworthy or not ranking.ideas:
            await respond(
                "Nothing here looks postworthy right now — try again after some real discussion."
            )
            return

        user_id = command["user_id"]
        ideas = ranking.ideas

        # Persist BEFORE posting so the card's handlers resolve everything by id, and
        # build blocks/fallback inside the session while the batch is attached.
        if len(ideas) == 1:
            # One clear idea — skip the picker; draft it now (keep a transparency line).
            async with SessionLocal() as session:
                profiles = await repo.get_voice_profiles(session, user_id)
                voices = build_voices(profiles, voice)
            try:
                content = await generate_idea_drafts(
                    ideas[0], transcript, voices, client=anthropic_client, settings=settings
                )
            except Exception:
                logger.exception("draft_post_generation_failed", channel=channel)
                await respond("Something went wrong while drafting — check the logs.")
                return
            if not content.drafts:
                await respond(
                    "Nothing here looks postworthy right now — try again after some discussion."
                )
                return
            async with SessionLocal() as session:
                async with session.begin():
                    batch = await repo.create_idea_batch(
                        session,
                        channel_id=channel,
                        user_id=user_id,
                        transcript=transcript,
                        candidate_ideas=[ideas[0].model_dump()],
                    )
                    batch_id = batch.id
                    await repo.replace_drafts(
                        session,
                        batch_id,
                        [
                            repo.DraftSpec(text=d.text, platform=DraftPlatform(d.platform))
                            for d in content.drafts
                        ],
                    )
                    await repo.set_chosen_idea(session, batch_id, 0)
                    await repo.set_batch_status(session, batch_id, BatchStatus.pending)
                    drafted = await repo.get_batch(session, batch_id)
                    assert drafted is not None
                    blocks = build_draft_blocks(drafted, x_char_limit=settings.x_char_limit)
                    blocks.append(
                        {
                            "type": "context",
                            "elements": [
                                {
                                    "type": "mrkdwn",
                                    "text": f"_Read {len(raw)} messages → 1 idea worth posting._",
                                }
                            ],
                        }
                    )
                    text = fallback_text(drafted)
            logger.info("draft_post_persisted", channel=channel, draft_count=len(content.drafts))
        else:
            # Several ideas — show the ranked shortlist and let the user pick one.
            async with SessionLocal() as session:
                async with session.begin():
                    batch = await repo.create_idea_batch(
                        session,
                        channel_id=channel,
                        user_id=user_id,
                        transcript=transcript,
                        candidate_ideas=[idea.model_dump() for idea in ideas],
                    )
                    batch_id = batch.id
                    blocks = build_idea_blocks(batch)
                    text = idea_fallback_text(batch)
            logger.info("draft_post_ideas_posted", channel=channel, idea_count=len(ideas))

        # Post the living card IN-CHANNEL so we capture the message ts.
        try:
            resp = await client.chat_postMessage(channel=channel, blocks=blocks, text=text)
        except SlackApiError as exc:
            error = (exc.response or {}).get("error")
            if error in _NOT_IN_CHANNEL_ERRORS:
                logger.warning("draft_post_not_in_channel", channel=channel, error=error)
                await respond("I couldn't post here — invite me to the channel and try again.")
                return
            logger.exception("draft_post_post_failed", channel=channel, error=error)
            await respond("Something went wrong posting the drafts — check the logs.")
            return

        message_ts = resp["ts"]
        async with SessionLocal() as session:
            async with session.begin():
                await repo.set_batch_message_ts(session, batch_id, channel, message_ts)

        logger.info("draft_post_card_posted", channel=channel, batch_id=str(batch_id))

    @app.command("/post-history")
    async def post_history(ack: Any, respond: Any) -> None:
        # Ack first (3s window). The history read is quick, but the rule holds.
        await ack()

        # Read-only. Build the blocks inside the session so the eager-loaded
        # batch/draft relationships stay attached while rendering.
        async with SessionLocal() as session:
            published = await repo.list_published(session, limit=settings.message_fetch_limit)
            unconfirmed = await repo.list_unconfirmed_approved(session)
            blocks = build_history_blocks(published, unconfirmed)
            text = history_fallback(len(published))

        await respond(blocks=blocks, text=text)
        logger.info("post_history_replied", published=len(published), unconfirmed=len(unconfirmed))
