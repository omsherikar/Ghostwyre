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
from app.platforms import PLATFORMS
from app.services.json_parse import JSONParseError, loads_lenient
from app.services.schemas import (
    Draft,
    DraftText,
    PostworthyItem,
    PostworthyResult,
    RankedIdeas,
    VoiceCard,
    VoiceMemoryNotes,
)

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


RANK_SCHEMA = {
    "type": "object",
    "properties": {
        "ideas": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "angle": {"type": "string"},
                    "score": {"type": "integer"},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["summary", "angle", "score", "evidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["ideas"],
    "additionalProperties": False,
}

RANK_SYSTEM = """\
You are given candidate post ideas already extracted from a team-chat transcript,
plus the transcript itself. Score and rank them so the user can pick the one idea
most worth posting publicly.

For EACH idea, score it 0-100 on these criteria together:
- novelty — is the insight fresh, or an obvious truism?
- specificity — is it concrete (real numbers, the actual bug/decision), or vague?
- audience value — would a developer/founder reader actually learn or feel something?
- "would you proudly share this" — is it worth attaching your name to publicly?

Rules:
- MERGE near-duplicate ideas into a single, stronger idea (do not list the same
  insight twice with different wording).
- For each idea, write `angle`: one line on the sharpest take to post about it.
- For each idea, include `evidence`: 1-3 SHORT VERBATIM quotes copied from the
  transcript that prompted it (the real lines — do not paraphrase or invent).
- Order the `ideas` array by `score`, highest first.
- Never include anything confidential (secrets/credentials, customer names,
  internal financials, unreleased plans) in any field."""


def _render_rank_request(items: list[PostworthyItem], transcript: str) -> str:
    """Build the ranking prompt: the candidate ideas + the source transcript."""
    candidates = "\n".join(f"- {item.summary} (why: {item.reason})" for item in items)
    return (
        "Candidate ideas extracted from the conversation:\n"
        f"{candidates}\n\n"
        "The conversation transcript (score against this, and quote from it as evidence):\n"
        f"{transcript}\n\n"
        "Score, dedupe, and rank these ideas."
    )


async def rank_ideas(
    items: list[PostworthyItem],
    transcript: str,
    *,
    client: LLMClient,
    settings: Settings,
) -> RankedIdeas:
    """Score, dedupe, and rank the candidate *items* against the *transcript*.

    Each returned idea carries a 0-100 score, a suggested angle, and verbatim
    transcript quotes as evidence; the list is ordered highest-score first. Reuses
    the structured-output + retry-once machinery. Never logs idea or transcript text.
    """
    system: list[TextBlockParam] = [
        {
            "type": "text",
            "text": RANK_SYSTEM,
            "cache_control": {"type": "ephemeral"},
        }
    ]
    result = await _structured_call(
        client=client,
        settings=settings,
        system=system,
        user=_render_rank_request(items, transcript),
        schema=RANK_SCHEMA,
        model_cls=RankedIdeas,
        max_tokens=settings.rank_max_tokens,
    )
    logger.info("idea_rank_complete", item_count=len(items), ranked_count=len(result.ideas))
    return result


PLATFORM_DRAFT_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}

DRAFT_SYSTEM = """\
You write ONE social-media post for a specific platform, in the user's voice,
grounded in a real team conversation.

You are given: the postworthy insight(s) from the chat, the conversation
transcript, the target platform's strategy, the user's VOICE GUIDE (how they
write), and a few of the user's REAL PAST POSTS as voice examples.

Write a single post about the strongest insight that:
- sounds like the user — match the voice guide and the rhythm/phrasing of their
  real example posts (write something NEW in the same voice; never copy a post);
- follows the platform strategy (length, hook, shape);
- is grounded in the conversation's real specifics — never invent facts;
- never includes anything confidential (secrets or credentials, customer or
  client names, internal financials, unreleased plans);
- adds no hashtags or emojis unless the voice guide allows them.

Output only the post text — no preamble, no commentary, no surrounding quotes."""


def _render_platform_request(
    items: list[PostworthyItem],
    transcript: str,
    positioning: str,
    exemplars: list[str],
) -> str:
    """Build the per-platform draft prompt: positioning + insights + transcript + exemplars."""
    insights = "\n".join(f"- {item.summary} (why: {item.reason})" for item in items)
    pos = f"The user's positioning: {positioning}\n\n" if positioning.strip() else ""
    examples = ""
    if exemplars:
        joined = "\n\n---\n\n".join(exemplars)
        examples = (
            f"The user's real past posts — match this voice, do NOT copy them:\n\n{joined}\n\n"
        )
    return (
        f"{pos}"
        "Postworthy insight(s) from the conversation:\n"
        f"{insights}\n\n"
        "The conversation transcript (ground the post in these real specifics):\n"
        f"{transcript}\n\n"
        f"{examples}"
        "Write one post about the strongest insight, in the user's voice."
    )


async def generate_platform_draft(
    items: list[PostworthyItem],
    platform: str,
    *,
    voice_card: str,
    positioning: str,
    exemplars: list[str],
    transcript: str,
    client: LLMClient,
    settings: Settings,
    memory: list[str] | None = None,
) -> Draft:
    """Draft ONE post for *platform*, in the user's voice, grounded in the transcript.

    System blocks: the draft rules + the platform's strategy + the user's voice
    card (cached) + any learned `memory` rules from past edits (cached). The user's
    positioning, the insights, the transcript, and the exemplar posts go in the user
    message. Retries the call+parse once. Never logs the transcript, exemplars,
    memory, or draft text.
    """
    system: list[TextBlockParam] = [
        {"type": "text", "text": DRAFT_SYSTEM},
        {"type": "text", "text": f"Target platform strategy:\n\n{PLATFORMS[platform].strategy}"},
        {
            "type": "text",
            "text": f"The user's voice guide:\n\n{voice_card}",
            "cache_control": {"type": "ephemeral"},
        },
    ]
    if memory:
        rules = "\n".join(f"- {rule}" for rule in memory)
        system.append(
            {
                "type": "text",
                "text": (
                    "Sticky preferences learned from this user's past edits — "
                    f"follow these exactly:\n{rules}"
                ),
                "cache_control": {"type": "ephemeral"},
            }
        )
    result = await _structured_call(
        client=client,
        settings=settings,
        system=system,
        user=_render_platform_request(items, transcript, positioning, exemplars),
        schema=PLATFORM_DRAFT_SCHEMA,
        model_cls=DraftText,
        max_tokens=settings.draft_max_tokens,
    )
    logger.info("platform_draft_complete", platform=platform)
    return Draft(platform=platform, text=result.text)


VOICE_DISTILL_SCHEMA = {
    "type": "object",
    "properties": {
        "voice_card": {"type": "string"},
        "positioning": {"type": "string"},
    },
    "required": ["voice_card", "positioning"],
    "additionalProperties": False,
}

VOICE_DISTILL_SYSTEM = """\
You analyze a person's real social-media posts for one platform and distill HOW
they write, so another system can draft new posts that sound exactly like them.

Produce two things:
- voice_card: a concrete, prescriptive style guide derived ONLY from the posts —
  their hooks/openers, sentence rhythm and length, vocabulary and signature
  phrases, formatting habits (line breaks, lists, emoji/hashtag usage), recurring
  themes, and the things they clearly never do. Write it as direct rules a writer
  could follow. Be specific and cite patterns you actually observe — never invent.
- positioning: 2-3 sentences on who they are and what they want to be known for,
  combining the themes in their posts with their stated goal (if any).

Do not quote a whole post as a "rule" — describe the underlying pattern."""


def _render_distill_request(posts: list[str], goals: str, platform: str) -> str:
    """Build the distillation prompt from the user's posts + stated goal."""
    numbered = "\n\n".join(f"[Post {i}]\n{post}" for i, post in enumerate(posts, start=1))
    goal_line = f"Their stated goal: {goals}\n\n" if goals.strip() else ""
    return f"Platform: {platform}\n\n{goal_line}Their real posts:\n\n{numbered}"


async def distill_voice_profile(
    posts: list[str],
    goals: str,
    platform: str,
    *,
    client: LLMClient,
    settings: Settings,
) -> VoiceCard:
    """Distill a per-platform voice card + positioning from the user's real *posts*.

    Reuses the structured-output + retry-once machinery. Never logs the posts.
    """
    system: list[TextBlockParam] = [{"type": "text", "text": VOICE_DISTILL_SYSTEM}]
    result = await _structured_call(
        client=client,
        settings=settings,
        system=system,
        user=_render_distill_request(posts, goals, platform),
        schema=VOICE_DISTILL_SCHEMA,
        model_cls=VoiceCard,
        max_tokens=settings.draft_max_tokens,
    )
    logger.info("voice_distill_complete", platform=platform, post_count=len(posts))
    return result


MEMORY_DISTILL_SCHEMA = {
    "type": "object",
    "properties": {"instructions": {"type": "array", "items": {"type": "string"}}},
    "required": ["instructions"],
    "additionalProperties": False,
}

MEMORY_DISTILL_SYSTEM = """\
You compare a social-media draft we generated with the user's edited version and infer
the DURABLE style preferences their edit reveals, so future drafts match without being
told again.

Return up to 3 `instructions`: short, concrete, GENERAL style rules phrased as commands
("Don't open with 'I'.", "Cut hype words like 'unlock'.", "Use shorter paragraphs.",
"No emojis.").

Strict rules:
- Infer ONLY style / voice / formatting changes — NEVER facts specific to this post
  (names, numbers, the topic). Those are not durable rules.
- If the edit is trivial (a typo, a tiny wording tweak, or no real change), return an
  empty list. Never invent rules to fill it.
- Each instruction must generalize to future posts and read as a direct command."""


def _render_memory_request(original: str, edited: str, platform: str) -> str:
    """Build the memory-distillation prompt from the original + edited draft."""
    return (
        f"Platform: {platform}\n\n"
        f"The draft we generated:\n\n{original}\n\n"
        f"The user's edited version:\n\n{edited}\n\n"
        "What durable style rules does this edit reveal?"
    )


async def distill_edit_memory(
    original: str,
    edited: str,
    platform: str,
    *,
    client: LLMClient,
    settings: Settings,
) -> VoiceMemoryNotes:
    """Distill durable style rules from a user's edit (original -> edited draft).

    Returns an empty list for trivial edits. Reuses the structured-output +
    retry-once machinery. Never logs the draft or the distilled rules.
    """
    system: list[TextBlockParam] = [{"type": "text", "text": MEMORY_DISTILL_SYSTEM}]
    result = await _structured_call(
        client=client,
        settings=settings,
        system=system,
        user=_render_memory_request(original, edited, platform),
        schema=MEMORY_DISTILL_SCHEMA,
        model_cls=VoiceMemoryNotes,
        max_tokens=settings.memory_max_tokens,
    )
    logger.info("memory_distill_complete", platform=platform, rule_count=len(result.instructions))
    return result
