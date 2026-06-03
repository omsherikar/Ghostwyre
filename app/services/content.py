"""Content orchestration: chain extract -> draft with a nothing-postworthy gate.

This module is the thin coordinator over the LLM layer (`app/services/llm.py`).
It runs step A (`extract_postworthy`) and, only if something is worth posting,
step B (`generate_drafts`). When step A finds nothing, the second model call is
skipped entirely — there's nothing to draft, and skipping saves tokens.

Kept deliberately Slack-free so it's pure and unit-testable; Slack wiring lives
in a later unit. Never logs transcript, item, or draft text — counts only.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.config import Settings
from app.logging import get_logger
from app.services.llm import LLMClient, extract_postworthy, generate_drafts
from app.services.schemas import Draft

logger = get_logger(__name__)

_VOICE_PATH = Path(__file__).resolve().parents[2] / "voice.md"


@dataclass(frozen=True)
class ContentResult:
    """Outcome of the content pipeline for one transcript.

    `postworthy` is False when step A found nothing worth posting, in which case
    `drafts` is empty and no drafting call was made.
    """

    postworthy: bool
    drafts: list[Draft]


async def generate_post_drafts(
    transcript: str,
    voice: str,
    *,
    client: LLMClient,
    settings: Settings,
) -> ContentResult:
    """Run the content pipeline: extract postworthy items, then draft posts.

    If extraction yields no items, short-circuit with an empty, not-postworthy
    result and skip the drafting call. Otherwise draft in the user's *voice* and
    return the drafts. Never logs transcript, item, or draft text.
    """
    extracted = await extract_postworthy(transcript, client=client, settings=settings)
    if not extracted.items:
        logger.info("content_nothing_postworthy", item_count=0)
        return ContentResult(postworthy=False, drafts=[])

    draft_set = await generate_drafts(
        extracted.items, voice, transcript=transcript, client=client, settings=settings
    )
    logger.info(
        "content_drafts_ready",
        item_count=len(extracted.items),
        draft_count=len(draft_set.drafts),
    )
    return ContentResult(postworthy=True, drafts=draft_set.drafts)


def load_voice(path: Path | None = None) -> str:
    """Read the voice guide, defaulting to the repo's `voice.md`.

    Returns the file's text, or an empty string if the file is missing —
    drafting still works without voice guidance, so a missing guide must not
    raise.
    """
    target = path if path is not None else _VOICE_PATH
    try:
        return target.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError):
        return ""
