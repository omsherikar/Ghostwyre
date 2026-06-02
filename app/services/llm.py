"""LLM layer: content-generation steps over Slack transcripts.

Step A is `extract_postworthy`: read a team-chat transcript and return only the
items genuinely worth posting publicly. This is the quality gate that keeps the
pipeline from producing slop — most clones skip it.

Step B is `generate_drafts`: turn those postworthy items into candidate posts
written in the user's voice. Voice is the moat: the drafts must sound like the
user, governed by the `voice` guide passed in as a cached system block.

Two reliability choices are baked in:
- Stable system prompts are sent with `cache_control: ephemeral` so repeated
  calls hit the prompt cache. For step B, system+voice cache together.
- `output_config` structured outputs guarantees schema-valid JSON; we still
  parse defensively and retry once, because a single malformed response should
  not fail the whole `/draft-post` flow.
"""

from __future__ import annotations

from typing import Any

from anthropic import AsyncAnthropic
from anthropic.types import TextBlockParam
from pydantic import BaseModel, ValidationError

from app.config import Settings, get_settings
from app.logging import get_logger
from app.services.json_parse import JSONParseError, loads_lenient
from app.services.schemas import DraftSet, PostworthyItem, PostworthyResult

logger = get_logger(__name__)

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"summary": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["summary", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

EXTRACT_SYSTEM = """\
You read a team-chat transcript and identify what is genuinely worth posting \
publicly on social media.

Return ONLY items that a developer would proudly share in public: a shipped \
feature, a lesson learned, a notable win, or an interesting bug and its fix.

Strict rules:
- Ignore logistics, scheduling, status pings, small talk, and general noise. \
None of that is postworthy.
- If nothing in the transcript is worth posting, return an empty items list. \
Never invent content to fill the list.
- Never include anything confidential: secrets or credentials, customer or \
client names, internal financials, or unreleased plans.
- Keep each `summary` concise (a single clear sentence). Keep each `reason` \
one short line explaining why it is worth posting."""

_MAX_ATTEMPTS = 2


def build_client(settings: Settings | None = None) -> AsyncAnthropic:
    """Construct a reusable async Anthropic client.

    Callers should create one client and pass it into `extract_postworthy`
    (and later `generate_drafts`) so connections are reused across steps.
    """
    settings = settings or get_settings()
    return AsyncAnthropic(api_key=settings.anthropic_api_key)


async def _structured_call[ModelT: BaseModel](
    *,
    client: AsyncAnthropic,
    settings: Settings,
    system: list[TextBlockParam],
    user: str,
    schema: dict[str, Any],
    model_cls: type[ModelT],
) -> ModelT:
    """Call the model with structured output, then parse + validate into *model_cls*.

    The stable *system* blocks (caller-supplied, including any `cache_control`)
    and the *user* message are sent with an `output_config` json_schema. The
    text block is extracted safely; a missing text block, malformed JSON, or a
    schema mismatch raises and triggers exactly one retry. After two failed
    attempts the last error propagates. Never logs message content.
    """
    last_error: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            resp = await client.messages.create(
                model=settings.llm_model,
                max_tokens=1024,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
            text_block = next((b for b in resp.content if b.type == "text"), None)
            if text_block is None:
                raise JSONParseError("No text block in model response")
            data = loads_lenient(text_block.text)
            return model_cls.model_validate(data)
        except (JSONParseError, ValidationError) as exc:
            last_error = exc
            logger.warning(
                "structured_call_parse_failed",
                attempt=attempt,
                max_attempts=_MAX_ATTEMPTS,
            )
    assert last_error is not None  # loop runs at least once
    raise last_error


async def extract_postworthy(
    transcript: str,
    *,
    client: AsyncAnthropic,
    settings: Settings,
) -> PostworthyResult:
    """Extract the postworthy items from a chat *transcript*.

    Retries the call+parse exactly once on a JSON/validation failure; if the
    second attempt also fails, the error propagates. Never logs transcript
    content.
    """
    system: list[TextBlockParam] = [
        {
            "type": "text",
            "text": EXTRACT_SYSTEM,
            "cache_control": {"type": "ephemeral"},
        }
    ]
    result = await _structured_call(
        client=client,
        settings=settings,
        system=system,
        user=transcript,
        schema=EXTRACT_SCHEMA,
        model_cls=PostworthyResult,
    )
    logger.info("postworthy_extract_complete", item_count=len(result.items))
    return result


DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "drafts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["drafts"],
    "additionalProperties": False,
}

DRAFT_SYSTEM = """\
You write candidate social-media posts (for X) in the user's voice.

The user's voice is provided as a separate guide. Treat it as the source of \
truth for tone, style, and what is allowed.

Strict rules:
- Produce 2-3 DISTINCT drafts, each taking a different angle on the material.
- Each draft is a complete, standalone post — not a thread, not a continuation.
- Each draft must be at most 280 characters.
- Strictly follow the voice and tone rules in the provided guide.
- Do NOT add hashtags or emojis unless the voice guide explicitly allows them.
- Output only the posts: no preamble, no commentary, no numbering."""


def _render_items(items: list[PostworthyItem], max_drafts: int) -> str:
    """Render postworthy *items* into a readable prompt with a draft instruction."""
    lines = [f"- {item.summary} (why: {item.reason})" for item in items]
    body = "\n".join(lines)
    return (
        "Here are the postworthy items to draft from:\n"
        f"{body}\n\n"
        f"Write up to {max_drafts} distinct posts in the user's voice."
    )


async def generate_drafts(
    items: list[PostworthyItem],
    voice: str,
    *,
    client: AsyncAnthropic,
    settings: Settings,
) -> DraftSet:
    """Draft candidate posts from postworthy *items*, in the user's *voice*.

    The stable draft instructions and the *voice* guide are sent as system
    blocks; the voice block carries `cache_control` so system+voice cache
    together across calls. Retries the call+parse exactly once on failure.
    Never logs the drafts' text or the transcript.
    """
    system: list[TextBlockParam] = [
        {"type": "text", "text": DRAFT_SYSTEM},
        {"type": "text", "text": voice, "cache_control": {"type": "ephemeral"}},
    ]
    result = await _structured_call(
        client=client,
        settings=settings,
        system=system,
        user=_render_items(items, settings.max_drafts),
        schema=DRAFT_SCHEMA,
        model_cls=DraftSet,
    )
    logger.info("drafts_generate_complete", draft_count=len(result.drafts))
    return result
