"""Content orchestration: extract the postworthy ideas, then draft one post PER
platform — each in that platform's voice, grounded in the conversation.

Thin coordinator over the LLM layer (`app/services/llm.py`): runs `extract_postworthy`
and, only if something is worth posting, calls `generate_platform_draft` once per
platform in the supplied `voices` map (fed that platform's voice card, positioning,
and a few of the user's real posts as exemplars). When extraction finds nothing, no
drafting calls are made.

Kept deliberately Slack-free so it's pure and unit-testable; Slack wiring lives in
the handlers. Never logs transcript, item, exemplar, or draft text — counts only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import Settings
from app.logging import get_logger
from app.platforms import PLATFORMS
from app.services.llm import LLMClient, extract_postworthy, generate_platform_draft
from app.services.schemas import Draft, PostworthyItem

logger = get_logger(__name__)

_VOICE_PATH = Path(__file__).resolve().parents[2] / "voice.md"


@dataclass(frozen=True)
class VoiceContext:
    """The voice inputs for ONE platform: the distilled voice card, the user's
    positioning, and their real sample posts (exemplars are picked from these)."""

    voice_card: str
    positioning: str
    sample_posts: list[str]


@dataclass(frozen=True)
class ContentResult:
    """Outcome of the content pipeline for one transcript.

    `postworthy` is False when extraction found nothing worth posting, in which
    case `drafts` is empty and no drafting call was made.
    """

    postworthy: bool
    drafts: list[Draft]


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2}


def select_exemplars(sample_posts: list[str], items: list[PostworthyItem], n: int) -> list[str]:
    """Pick up to *n* of the user's real posts as voice exemplars.

    Lightweight relevance: rank by word overlap with the postworthy insight(s),
    falling back to original order. No embeddings (corpora are small).
    """
    if len(sample_posts) <= n:
        return list(sample_posts)
    insight_tokens = _tokens(" ".join(f"{i.summary} {i.reason}" for i in items))
    ranked = sorted(sample_posts, key=lambda p: len(_tokens(p) & insight_tokens), reverse=True)
    return ranked[:n]


async def generate_post_drafts(
    transcript: str,
    voices: dict[str, VoiceContext],
    *,
    client: LLMClient,
    settings: Settings,
) -> ContentResult:
    """Extract postworthy items, then draft one post per platform in *voices*.

    If extraction yields no items, short-circuit with an empty, not-postworthy
    result and make no drafting calls. Otherwise generate one draft per platform,
    each with that platform's voice card + exemplars + strategy. Never logs
    transcript, item, exemplar, or draft text.
    """
    extracted = await extract_postworthy(transcript, client=client, settings=settings)
    if not extracted.items:
        logger.info("content_nothing_postworthy", item_count=0)
        return ContentResult(postworthy=False, drafts=[])

    drafts: list[Draft] = []
    for platform, ctx in voices.items():
        exemplars = select_exemplars(
            ctx.sample_posts, extracted.items, settings.voice_exemplar_count
        )
        draft = await generate_platform_draft(
            extracted.items,
            platform,
            voice_card=ctx.voice_card,
            positioning=ctx.positioning,
            exemplars=exemplars,
            transcript=transcript,
            client=client,
            settings=settings,
        )
        drafts.append(draft)

    logger.info("content_drafts_ready", item_count=len(extracted.items), draft_count=len(drafts))
    return ContentResult(postworthy=True, drafts=drafts)


def seed_voices(voice_md: str) -> dict[str, VoiceContext]:
    """Fallback `voices` map for users without a profile: the static `voice.md`
    as every platform's voice card, no positioning, no exemplars."""
    return {
        platform: VoiceContext(voice_card=voice_md, positioning="", sample_posts=[])
        for platform in PLATFORMS
    }


def build_voices(profiles: dict[str, Any], voice_md: str) -> dict[str, VoiceContext]:
    """Build the per-platform `voices` map: each platform uses the user's stored
    profile (voice card + positioning + sample posts) when present, else falls back
    to the `voice.md` seed. *profiles* is a platform→VoiceProfile map from the repo;
    call this while its rows are still attached to a session (it reads their fields).
    """
    voices: dict[str, VoiceContext] = {}
    for platform in PLATFORMS:
        profile = profiles.get(platform)
        if profile is None:
            voices[platform] = VoiceContext(voice_card=voice_md, positioning="", sample_posts=[])
        else:
            voices[platform] = VoiceContext(
                voice_card=profile.voice_card,
                positioning=profile.positioning,
                sample_posts=list(profile.sample_posts),
            )
    return voices


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
