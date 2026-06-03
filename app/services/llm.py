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

import json
from typing import Any, cast

from anthropic import AsyncAnthropic
from anthropic.types import TextBlockParam
from groq import AsyncGroq
from pydantic import BaseModel, ValidationError

from app.config import Settings, get_settings
from app.logging import get_logger
from app.services.json_parse import JSONParseError, loads_lenient
from app.services.schemas import DraftSet, PostworthyItem, PostworthyResult

logger = get_logger(__name__)

# The client is provider-dependent; callers stay agnostic and just pass it through.
LLMClient = AsyncAnthropic | AsyncGroq

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


def build_client(settings: Settings | None = None) -> LLMClient:
    """Construct the reusable async LLM client for the configured provider.

    Callers create one client and pass it into `extract_postworthy` /
    `generate_drafts` so connections are reused across steps. `LLM_PROVIDER=groq`
    returns an `AsyncGroq`; otherwise (default) an `AsyncAnthropic`.
    """
    settings = settings or get_settings()
    if settings.llm_provider == "groq":
        return AsyncGroq(api_key=settings.groq_api_key)
    return AsyncAnthropic(api_key=settings.anthropic_api_key)


async def _raw_completion(
    *,
    client: LLMClient,
    settings: Settings,
    system: list[TextBlockParam],
    user: str,
    schema: dict[str, Any],
    max_tokens: int,
) -> str:
    """Provider-specific call returning the model's raw (expected-JSON) text.

    Anthropic uses structured outputs + prompt caching (the *system* blocks are
    sent as given, `cache_control` included). Groq (OpenAI-compatible) has
    neither, so the JSON schema is described in the prompt and JSON mode forces a
    valid object; the shared lenient-parse + retry handle any drift. Never logs
    message content.
    """
    if settings.llm_provider == "groq":
        system_text = "\n\n".join(block["text"] for block in system)
        system_text += (
            "\n\nReturn ONLY a single JSON object that matches this JSON schema "
            "(no prose, no code fences):\n" + json.dumps(schema)
        )
        groq_client = cast(Any, client)
        groq_resp = await groq_client.chat.completions.create(
            model=settings.groq_model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system_text},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
        )
        content = groq_resp.choices[0].message.content
        if content is None:
            raise JSONParseError("No content in Groq response")
        return str(content)

    anthropic_client = cast(AsyncAnthropic, client)
    resp = await anthropic_client.messages.create(
        model=settings.llm_model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )
    text_block = next((b for b in resp.content if b.type == "text"), None)
    if text_block is None:
        raise JSONParseError("No text block in model response")
    return text_block.text


async def _structured_call[ModelT: BaseModel](
    *,
    client: LLMClient,
    settings: Settings,
    system: list[TextBlockParam],
    user: str,
    schema: dict[str, Any],
    model_cls: type[ModelT],
    max_tokens: int,
) -> ModelT:
    """Call the model (via the provider-specific path), then parse + validate.

    A missing/empty response, malformed JSON, or a schema mismatch raises and
    triggers exactly one retry. After two failed attempts the last error
    propagates. Never logs message content.
    """
    last_error: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            raw = await _raw_completion(
                client=client,
                settings=settings,
                system=system,
                user=user,
                schema=schema,
                max_tokens=max_tokens,
            )
            data = loads_lenient(raw)
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
    client: LLMClient,
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
        max_tokens=settings.extract_max_tokens,
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
                "properties": {
                    "platform": {"type": "string", "enum": ["x", "linkedin"]},
                    "text": {"type": "string"},
                },
                "required": ["platform", "text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["drafts"],
    "additionalProperties": False,
}

DRAFT_SYSTEM = """\
You write social-media posts in the user's voice, grounded in a real team
conversation. You are given: the postworthy insight(s) extracted from the chat,
the conversation transcript itself, and a separate voice guide.

Pick the single strongest insight and write TWO posts about it — one for LinkedIn
and one for X — each developed and specific, drawing on the ACTUAL details of the
conversation (what was built, the numbers, the decision, the lesson), not generic
platitudes.

Per platform:
- linkedin: a developed, multi-paragraph post (roughly 600-1300 characters). Open
  with a hook, develop the insight with the concrete specifics, close with a crisp
  takeaway. Short paragraphs separated by blank lines read well.
- x: a developed long-form post — go past a single line; lead with the sharpest
  point and build the argument. Substantive, not a teaser.

Strict rules:
- Treat the voice guide as the source of truth for tone and style; match it.
- Ground every post in the conversation's real specifics. Never invent facts.
- Never include anything confidential: secrets or credentials, customer or client
  names, internal financials, or unreleased plans.
- Do NOT add hashtags or emojis unless the voice guide explicitly allows them.
- Output exactly one `linkedin` post and one `x` post. No preamble, no commentary."""


def _render_draft_request(items: list[PostworthyItem], transcript: str) -> str:
    """Build the draft prompt: the postworthy insight(s) + the grounding transcript."""
    insights = "\n".join(f"- {item.summary} (why: {item.reason})" for item in items)
    return (
        "Postworthy insight(s) from the conversation:\n"
        f"{insights}\n\n"
        "The conversation transcript (ground the posts in these real specifics):\n"
        f"{transcript}\n\n"
        "Write one LinkedIn post and one X post about the strongest insight, in the "
        "user's voice."
    )


async def generate_drafts(
    items: list[PostworthyItem],
    voice: str,
    *,
    transcript: str,
    client: LLMClient,
    settings: Settings,
) -> DraftSet:
    """Draft a long LinkedIn post + a long X post from postworthy *items*.

    Both are written in the user's *voice* and grounded in the *transcript* (the
    actual conversation). The stable draft instructions and the voice guide are
    sent as system blocks; the voice block carries `cache_control` so system+voice
    cache together. Uses the larger draft token budget. Retries the call+parse
    exactly once on failure. Never logs the transcript or draft text.
    """
    system: list[TextBlockParam] = [
        {"type": "text", "text": DRAFT_SYSTEM},
        {"type": "text", "text": voice, "cache_control": {"type": "ephemeral"}},
    ]
    result = await _structured_call(
        client=client,
        settings=settings,
        system=system,
        user=_render_draft_request(items, transcript),
        schema=DRAFT_SCHEMA,
        model_cls=DraftSet,
        max_tokens=settings.draft_max_tokens,
    )
    logger.info("drafts_generate_complete", draft_count=len(result.drafts))
    return result
