"""Onboarding: `/setup` opens a modal where the user pastes their real posts, and
the submission distills a per-platform voice profile that later drives drafting.

Flow: `/setup` -> `client.views_open` a modal with paste fields (X posts, LinkedIn
posts) + a "what do you want to be known for?" field -> on submit, split each
paste into posts, `distill_voice_profile` per platform that has posts, and
`upsert_voice_profile`. The submission acks within Slack's 3s window first, then
does the (slow) distillation, then DMs a confirmation.

Never logs the pasted posts or the distilled card — counts/ids only (PII rule).
"""

from __future__ import annotations

import re
from typing import Any

from slack_bolt.async_app import AsyncApp

from app import repo
from app.config import get_settings
from app.db import SessionLocal
from app.db.models import DraftPlatform
from app.logging import get_logger
from app.services.llm import build_client, distill_voice_profile

logger = get_logger(__name__)

SETUP_CALLBACK_ID = "ghostwyre_setup"

# block_id -> platform for the paste inputs.
_PLATFORM_BLOCKS = {"x_posts": "x", "linkedin_posts": "linkedin"}


def _split_posts(text: str) -> list[str]:
    """Split a paste into individual posts on blank lines; drop empties."""
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def _setup_view() -> dict[str, Any]:
    return {
        "type": "modal",
        "callback_id": SETUP_CALLBACK_ID,
        "title": {"type": "plain_text", "text": "Teach Ghostwyre"},
        "submit": {"type": "plain_text", "text": "Save"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "Paste a few of your real posts so drafts sound like *you*. "
                        "Separate posts with a blank line. Leave a platform blank to skip it."
                    ),
                },
            },
            {
                "type": "input",
                "block_id": "x_posts",
                "optional": True,
                "label": {"type": "plain_text", "text": "Your recent X posts"},
                "element": {"type": "plain_text_input", "action_id": "value", "multiline": True},
            },
            {
                "type": "input",
                "block_id": "linkedin_posts",
                "optional": True,
                "label": {"type": "plain_text", "text": "Your recent LinkedIn posts"},
                "element": {"type": "plain_text_input", "action_id": "value", "multiline": True},
            },
            {
                "type": "input",
                "block_id": "goals",
                "optional": True,
                "label": {"type": "plain_text", "text": "What do you want to be known for?"},
                "element": {"type": "plain_text_input", "action_id": "value", "multiline": True},
            },
        ],
    }


def register(app: AsyncApp) -> None:
    # Build collaborators once at registration time.
    settings = get_settings()
    anthropic_client = build_client(settings)

    @app.command("/setup")
    async def setup_command(ack: Any, body: dict[str, Any], client: Any) -> None:
        await ack()  # FIRST — Slack times out at 3s.
        await client.views_open(trigger_id=body["trigger_id"], view=_setup_view())

    @app.view(SETUP_CALLBACK_ID)
    async def handle_setup(
        ack: Any, body: dict[str, Any], view: dict[str, Any], client: Any
    ) -> None:
        await ack()  # closes the modal; the slow work runs after.
        user_id = body["user"]["id"]
        state = view["state"]["values"]

        def _field(block_id: str) -> str:
            return (state.get(block_id, {}).get("value", {}).get("value") or "").strip()

        goals = _field("goals")
        saved: list[str] = []
        try:
            for block_id, platform in _PLATFORM_BLOCKS.items():
                posts = _split_posts(_field(block_id))
                if not posts:
                    continue
                card = await distill_voice_profile(
                    posts, goals, platform, client=anthropic_client, settings=settings
                )
                async with SessionLocal() as session, session.begin():
                    await repo.upsert_voice_profile(
                        session,
                        slack_user_id=user_id,
                        platform=DraftPlatform(platform),
                        sample_posts=posts,
                        voice_card=card.voice_card,
                        positioning=card.positioning,
                    )
                saved.append(platform)
                logger.info("voice_profile_saved", platform=platform, post_count=len(posts))
        except Exception:
            logger.exception("setup_failed", user_id=user_id)
            await client.chat_postMessage(
                channel=user_id, text="Something went wrong learning your voice — check the logs."
            )
            return

        if saved:
            await client.chat_postMessage(
                channel=user_id,
                text=(
                    f"✅ Learned your voice for: {', '.join(saved)}. "
                    "Run `/draft-post` and your drafts will sound like you."
                ),
            )
        else:
            await client.chat_postMessage(
                channel=user_id,
                text="Nothing to learn — paste at least one post. Run `/setup` to try again.",
            )
