"""Edit a draft before approving — and learn from the change (Pillar D).

The card's **Edit** button opens a modal pre-filled with the draft. On submit we
save the edited text (so Approve publishes *that*) and distill durable style rules
from the (original → edited) diff into the user's voice memory, then re-render the
living card. Memory steers FUTURE drafts; the edit fixes the current one.

Carries ids + the card's (channel, ts) through `private_metadata` so the view
submission can update the right message. Never logs draft or memory text — ids and
counts only (PII / confidential rule).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from slack_bolt.async_app import AsyncApp

from app import repo
from app.config import get_settings
from app.db import SessionLocal
from app.db.models import BatchStatus, DraftPlatform
from app.logging import get_logger
from app.services.llm import build_client, distill_edit_memory
from app.slack.blocks import build_draft_blocks, build_voice_blocks, fallback_text, voice_fallback

logger = get_logger(__name__)

EDIT_CALLBACK_ID = "ghostwyre_edit"


def _edit_view(*, draft_text: str, private_metadata: str) -> dict[str, Any]:
    return {
        "type": "modal",
        "callback_id": EDIT_CALLBACK_ID,
        "private_metadata": private_metadata,
        "title": {"type": "plain_text", "text": "Edit draft"},
        "submit": {"type": "plain_text", "text": "Save"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "draft",
                "label": {"type": "plain_text", "text": "Your post"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "value",
                    "multiline": True,
                    "initial_value": draft_text,
                },
            }
        ],
    }


def register(app: AsyncApp) -> None:
    settings = get_settings()
    anthropic_client = build_client(settings)

    @app.action("edit_draft")
    async def edit_draft(
        ack: Any, body: dict[str, Any], action: dict[str, Any], client: Any
    ) -> None:
        await ack()  # FIRST — Slack times out at 3s.
        try:
            data = json.loads(action.get("value") or "{}")
            b = uuid.UUID(data["b"])
            d = uuid.UUID(data["d"])
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            return  # malformed button — nothing to open

        async with SessionLocal() as session:
            draft = await repo.get_draft(session, d)
            text = draft.text if draft is not None else None
        if text is None:
            return

        # Carry only ids — the card's (channel, ts) are read from the batch record on
        # submit (the authoritative source, like the other handlers).
        meta = json.dumps({"b": str(b), "d": str(d)})
        await client.views_open(
            trigger_id=body["trigger_id"],
            view=_edit_view(draft_text=text, private_metadata=meta),
        )

    @app.view(EDIT_CALLBACK_ID)
    async def handle_edit(
        ack: Any, body: dict[str, Any], view: dict[str, Any], client: Any
    ) -> None:
        await ack()  # closes the modal; the slow work runs after.
        try:
            meta = json.loads(view.get("private_metadata") or "{}")
            d = uuid.UUID(meta["d"])
            b = uuid.UUID(meta["b"])
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            return
        edited = (
            view.get("state", {}).get("values", {}).get("draft", {}).get("value", {}).get("value")
            or ""
        ).strip()

        # Load the original (DB still holds it) + cheap pre-guard. Capture primitives.
        async with SessionLocal() as session:
            draft = await repo.get_draft(session, d)
            if draft is None:
                return
            original = draft.text
            platform = str(draft.platform)
            user_id = draft.batch.slack_user_id
            status = draft.batch.status
        # No-op if the batch can't be edited, the field was cleared, or nothing changed.
        if status != BatchStatus.pending or not edited or edited == original:
            return

        # Save the edit + re-render, re-checking `pending` INSIDE the txn so a concurrent
        # Approve/Cancel landing between the read above and this write can't be clobbered
        # (and we never mutate an already-published draft or learn from it).
        async with SessionLocal() as session:
            async with session.begin():
                batch = await repo.get_batch(session, b)
                if batch is None or batch.status != BatchStatus.pending:
                    return  # resolved meanwhile — leave the terminal card alone
                await repo.set_draft_text(session, d, edited)
            batch = await repo.get_batch(session, b)
        if batch is None:
            return
        # Use the card's authoritative coordinates from the batch record.
        await client.chat_update(
            channel=batch.slack_channel_id,
            ts=batch.slack_message_ts,
            blocks=build_draft_blocks(batch, x_char_limit=settings.x_char_limit),
            text=fallback_text(batch),
        )

        # Learn from the edit (slow LLM call, after the card already updated).
        try:
            notes = await distill_edit_memory(
                original, edited, platform, client=anthropic_client, settings=settings
            )
        except Exception:
            logger.exception("edit_distill_failed", draft_id=str(d))
            return
        if notes.instructions:
            async with SessionLocal() as session:
                async with session.begin():
                    await repo.add_voice_memory(
                        session,
                        slack_user_id=user_id,
                        platform=DraftPlatform(platform),
                        instructions=notes.instructions,
                    )
            logger.info("edit_learned", draft_id=str(d), rule_count=len(notes.instructions))

    @app.command("/voice")
    async def voice_command(ack: Any, command: dict[str, Any], respond: Any) -> None:
        await ack()
        user_id = command["user_id"]
        async with SessionLocal() as session:
            rows = await repo.list_voice_memory(session, user_id)
            blocks = build_voice_blocks(rows)
            text = voice_fallback(len(rows))
        await respond(blocks=blocks, text=text)
        logger.info("voice_listed", rule_count=len(rows))

    @app.action("forget_memory")
    async def forget_memory(
        ack: Any, body: dict[str, Any], action: dict[str, Any], respond: Any
    ) -> None:
        await ack()
        try:
            data = json.loads(action.get("value") or "{}")
            m = uuid.UUID(data["m"])
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            return
        user_id = body["user"]["id"]
        async with SessionLocal() as session:
            async with session.begin():
                await repo.delete_voice_memory(session, m, slack_user_id=user_id)
            rows = await repo.list_voice_memory(session, user_id)
            blocks = build_voice_blocks(rows)
            text = voice_fallback(len(rows))
        await respond(replace_original=True, blocks=blocks, text=text)
        logger.info("voice_forgot", rule_count=len(rows))
