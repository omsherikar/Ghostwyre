"""Slack interactive-action handlers for the approval card.

Three buttons drive the human-in-the-loop gate on the one living card per batch:
Approve (the ONLY publish path), Regenerate (re-runs the full content chain),
and Cancel. Every handler:

1. `ack()`s first (Slack's 3-second window).
2. resolves everything BY ID from the button payload, loading the batch inside a
   single `session.begin()` transaction (the caller owns the transaction; repo
   functions only flush).
3. is idempotent: if the batch is gone or no longer `pending`, it just re-renders
   the terminal card and returns — no publish, regen, or cancel. This makes Slack
   retries and double-clicks safe (publish fires exactly once).
4. edits the SAME message in place via `client.chat_update(channel, ts, …)`,
   re-sending the full blocks list from `build_draft_blocks` on the post-mutation
   batch state.

Logging is ids/counts/flags only — never the transcript or draft text
(CONFIDENTIAL at rest).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from slack_bolt.async_app import AsyncApp

from app import repo
from app.config import get_settings
from app.db import SessionLocal
from app.db.models import ApprovalAction, BatchStatus, DraftBatch, DraftStatus
from app.logging import get_logger
from app.services.content import generate_post_drafts, load_voice
from app.services.llm import build_client
from app.services.publisher import (
    PublisherClient,
    PublishError,
    PublishUnknownError,
    get_publisher,
)
from app.slack.blocks import build_draft_blocks, fallback_text

logger = get_logger(__name__)

# The publisher is built once in register(). It lives in a module-level slot so
# tests can swap in a fake via _set_publisher() without monkeypatching a closure.
_publisher: PublisherClient | None = None


def _set_publisher(publisher: PublisherClient) -> None:
    """Override the active publisher (used by tests)."""
    global _publisher
    _publisher = publisher


def parse_action_value(value: str | None) -> tuple[str, str | None]:
    """Decode a button value to (batch_id, draft_id?) — draft_id is None for cancel."""
    data = json.loads(value or "{}")
    return data["b"], data.get("d")


def _recover_batch_id(action: dict[str, Any]) -> str | None:
    """Best-effort batch id from the block_id backstop `draft_actions:<batch>:<slot>`."""
    block_id = action.get("block_id")
    if isinstance(block_id, str) and block_id.startswith("draft_actions:"):
        parts = block_id.split(":")
        if len(parts) >= 2 and parts[1]:
            return parts[1]
    return None


def _notice_blocks(mrkdwn: str) -> list[dict[str, Any]]:
    """A single-section, button-free notice card."""
    return [{"type": "section", "text": {"type": "mrkdwn", "text": mrkdwn}}]


def _context_line(mrkdwn: str) -> dict[str, Any]:
    """A context block appended to a rebuilt card to show a status line."""
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": mrkdwn}]}


def _regenerating_blocks(batch: DraftBatch) -> list[dict[str, Any]]:
    """Lightweight interim card shown while regeneration runs (no buttons)."""
    n = len(batch.drafts)
    return [
        {"type": "header", "text": {"type": "plain_text", "text": "Regenerating…"}},
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"Re-drafting {n} posts 🔄"}],
        },
    ]


async def _update_card(
    client: Any,
    batch: DraftBatch,
    blocks: list[dict[str, Any]],
    text: str,
) -> None:
    """Edit the batch's living card in place."""
    await client.chat_update(
        channel=batch.slack_channel_id,
        ts=batch.slack_message_ts,
        blocks=blocks,
        text=text,
    )


def register(app: AsyncApp) -> None:
    # Build collaborators once at registration time, not per invocation.
    settings = get_settings()
    anthropic_client = build_client(settings)
    voice = load_voice()
    _set_publisher(get_publisher(settings))

    @app.action("approve_draft")
    async def approve_draft(
        ack: Any, body: dict[str, Any], action: dict[str, Any], client: Any, logger: Any
    ) -> None:
        await ack()  # FIRST — Slack times out at 3s.

        batch_id_s: str | None
        try:
            batch_id_s, draft_id_s = parse_action_value(action.get("value"))
        except (json.JSONDecodeError, KeyError):
            batch_id_s, draft_id_s = _recover_batch_id(action), None
        try:
            b = uuid.UUID(batch_id_s) if batch_id_s else None
            d = uuid.UUID(draft_id_s) if draft_id_s else None
        except (ValueError, TypeError):
            b = d = None

        if b is None or d is None:
            await client.chat_update(
                channel=body.get("channel", {}).get("id", ""),
                ts=body.get("message", {}).get("ts", ""),
                blocks=_notice_blocks(
                    "Sorry — I couldn't read that button. Run `/draft-post` again."
                ),
                text="Ghostwyre: couldn't read that button.",
            )
            return

        user_id = body["user"]["id"]

        # Txn 1: validate + ATOMICALLY claim the batch (status -> approved) BEFORE
        # any network call. Capture the draft text as a primitive — never re-render
        # off these ORM objects once the session closes.
        async with SessionLocal() as session:
            async with session.begin():
                batch = await repo.get_batch(session, b)
                if batch is None or batch.status != BatchStatus.pending:
                    if batch is not None:
                        await _update_card(
                            client, batch, build_draft_blocks(batch), fallback_text(batch)
                        )
                    return

                draft = next((dr for dr in batch.drafts if dr.id == d), None)
                if draft is None:
                    await _update_card(
                        client,
                        batch,
                        _notice_blocks("Sorry — that draft is gone. Run `/draft-post` again."),
                        "Ghostwyre: draft not found.",
                    )
                    return

                draft_text = draft.text
                claimed = await repo.claim_for_publish(session, b)

        if not claimed:
            # Lost the claim to a concurrent click / Slack retry — re-render only.
            async with SessionLocal() as session:
                batch = await repo.get_batch(session, b)
            if batch is not None:
                await _update_card(client, batch, build_draft_blocks(batch), fallback_text(batch))
            return

        # Publish OUTSIDE any transaction. The batch is already 'approved', so a
        # failed chat_update below can no longer roll the status back: a re-click
        # re-renders instead of republishing (the at-most-once gate).
        publisher = _publisher
        assert publisher is not None  # set in register()
        try:
            result = await publisher.publish(draft_text)
        except PublishError as exc:
            # Definite failure — nothing was posted. Revert the claim so the card
            # re-arms and the user can retry or Regenerate.
            async with SessionLocal() as session:
                async with session.begin():
                    await repo.set_batch_status(session, b, BatchStatus.pending)
                    await repo.set_draft_status(session, d, DraftStatus.pending)
                batch = await repo.get_batch(session, b)
            if batch is not None:
                blocks = build_draft_blocks(batch)
                blocks.append(_context_line(f"⚠️ Couldn't publish: {exc}. Try Regenerate."))
                await _update_card(client, batch, blocks, "Ghostwyre: couldn't publish.")
            logger.info("approve_publish_failed", batch_id=str(b))
            return
        except PublishUnknownError as exc:
            # Ambiguous — the post may exist. Leave the batch 'approved' (do NOT
            # re-arm; that risks a double-post) and record an audit row with no url.
            async with SessionLocal() as session:
                async with session.begin():
                    await repo.set_draft_status(session, d, DraftStatus.approved)
                    await repo.record_event(
                        session,
                        batch_id=b,
                        draft_id=d,
                        action=ApprovalAction.approve,
                        slack_user_id=user_id,
                        publish_url=None,
                    )
                batch = await repo.get_batch(session, b)
            if batch is not None:
                blocks = build_draft_blocks(batch)
                blocks.append(_context_line(f"⚠️ Publish status unknown. {exc}"))
                await _update_card(client, batch, blocks, "Ghostwyre: publish status unknown.")
            logger.warning("approve_publish_unknown", batch_id=str(b))
            return

        # Success — record the event and render the terminal card with the link.
        async with SessionLocal() as session:
            async with session.begin():
                await repo.set_draft_status(session, d, DraftStatus.approved)
                await repo.record_event(
                    session,
                    batch_id=b,
                    draft_id=d,
                    action=ApprovalAction.approve,
                    slack_user_id=user_id,
                    publish_url=result.url,
                )
            batch = await repo.get_batch(session, b)
        assert batch is not None
        blocks = build_draft_blocks(batch)
        run_label = "dry-run" if result.dry_run else "live"
        blocks.append(_context_line(f"Published ({run_label}): {result.url}"))
        await _update_card(client, batch, blocks, fallback_text(batch))
        logger.info("approve_published", batch_id=str(b), dry_run=result.dry_run)

    @app.action("regenerate_draft")
    async def regenerate_draft(
        ack: Any, body: dict[str, Any], action: dict[str, Any], client: Any, logger: Any
    ) -> None:
        await ack()  # FIRST.

        batch_id_s: str | None
        try:
            batch_id_s, _ = parse_action_value(action.get("value"))
        except (json.JSONDecodeError, KeyError):
            batch_id_s = _recover_batch_id(action)
        try:
            b = uuid.UUID(batch_id_s) if batch_id_s else None
        except (ValueError, TypeError):
            b = None

        if b is None:
            await client.chat_update(
                channel=body.get("channel", {}).get("id", ""),
                ts=body.get("message", {}).get("ts", ""),
                blocks=_notice_blocks(
                    "Sorry — I couldn't read that button. Run `/draft-post` again."
                ),
                text="Ghostwyre: couldn't read that button.",
            )
            return

        # Read + guard (no writes). Re-render of a terminal batch happens outside.
        async with SessionLocal() as session:
            batch = await repo.get_batch(session, b)
        if batch is None or batch.status != BatchStatus.pending:
            if batch is not None:
                await _update_card(client, batch, build_draft_blocks(batch), fallback_text(batch))
            return
        transcript = batch.transcript  # CONFIDENTIAL — never logged.

        # Interim feedback (no transaction held) so the user can't double-fire.
        await _update_card(
            client, batch, _regenerating_blocks(batch), "Ghostwyre: regenerating drafts…"
        )

        # The LLM call runs with no DB transaction open.
        try:
            result = await generate_post_drafts(
                transcript, voice, client=anthropic_client, settings=settings
            )
        except Exception:
            logger.exception("regenerate_failed", batch_id=str(b))
            async with SessionLocal() as session:
                batch = await repo.get_batch(session, b)
            if batch is not None:
                blocks = build_draft_blocks(batch)
                blocks.append(_context_line("⚠️ Regenerate failed — original drafts kept."))
                await _update_card(client, batch, blocks, "Ghostwyre: regenerate failed.")
            return

        if not result.postworthy:
            async with SessionLocal() as session:
                batch = await repo.get_batch(session, b)
            if batch is not None:
                await _update_card(
                    client,
                    batch,
                    _notice_blocks(
                        "Nothing here looks postworthy on a re-read — "
                        "the original drafts are kept. Run `/draft-post` after more discussion."
                    ),
                    "Ghostwyre: nothing postworthy.",
                )
            return

        # Swap the drafts + record the event in one short transaction, THEN render.
        async with SessionLocal() as session:
            async with session.begin():
                await repo.replace_drafts(session, b, [d.text for d in result.drafts])
                await repo.record_event(
                    session,
                    batch_id=b,
                    draft_id=None,
                    action=ApprovalAction.regenerate,
                    slack_user_id=body["user"]["id"],
                )
            batch = await repo.get_batch(session, b)

        if batch is not None:
            await _update_card(client, batch, build_draft_blocks(batch), fallback_text(batch))
        logger.info("regenerate_done", batch_id=str(b), draft_count=len(result.drafts))

    @app.action("cancel_batch")
    async def cancel_batch(
        ack: Any, body: dict[str, Any], action: dict[str, Any], client: Any, logger: Any
    ) -> None:
        await ack()  # FIRST.

        batch_id_s: str | None
        try:
            batch_id_s, _ = parse_action_value(action.get("value"))
        except (json.JSONDecodeError, KeyError):
            batch_id_s = _recover_batch_id(action)
        try:
            b = uuid.UUID(batch_id_s) if batch_id_s else None
        except (ValueError, TypeError):
            b = None

        if b is None:
            await client.chat_update(
                channel=body.get("channel", {}).get("id", ""),
                ts=body.get("message", {}).get("ts", ""),
                blocks=_notice_blocks(
                    "Sorry — I couldn't read that button. Run `/draft-post` again."
                ),
                text="Ghostwyre: couldn't read that button.",
            )
            return

        # Mark cancelled in one short transaction, THEN render outside it (a failed
        # chat_update can't roll the cancel back). A non-pending batch just re-renders.
        async with SessionLocal() as session:
            async with session.begin():
                batch = await repo.get_batch(session, b)
                if batch is None:
                    return
                if batch.status == BatchStatus.pending:
                    await repo.set_batch_status(session, b, BatchStatus.cancelled)
                    await repo.record_event(
                        session,
                        batch_id=b,
                        draft_id=None,
                        action=ApprovalAction.cancel,
                        slack_user_id=body["user"]["id"],
                    )
            batch = await repo.get_batch(session, b)

        if batch is not None:
            await _update_card(client, batch, build_draft_blocks(batch), fallback_text(batch))
        logger.info("batch_cancelled", batch_id=str(b))
