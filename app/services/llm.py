"""LLM layer: content-generation steps over Slack transcripts.

Step A (this unit) is `extract_postworthy`: read a team-chat transcript and
return only the items genuinely worth posting publicly. This is the quality
gate that keeps the pipeline from producing slop — most clones skip it.

Two reliability choices are baked in:
- The stable system prompt is sent as a `cache_control: ephemeral` block so
  repeated calls hit the prompt cache.
- `output_config` structured outputs guarantees schema-valid JSON; we still
  parse defensively and retry once, because a single malformed response should
  not fail the whole `/draft-post` flow.
"""

from __future__ import annotations

from anthropic import AsyncAnthropic
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.logging import get_logger
from app.services.json_parse import JSONParseError, loads_lenient
from app.services.schemas import PostworthyResult

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
    last_error: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return await _call_and_parse(transcript, client=client, settings=settings)
        except (JSONParseError, ValidationError) as exc:
            last_error = exc
            logger.warning(
                "postworthy_extract_parse_failed",
                attempt=attempt,
                max_attempts=_MAX_ATTEMPTS,
            )
    assert last_error is not None  # loop runs at least once
    raise last_error


async def _call_and_parse(
    transcript: str,
    *,
    client: AsyncAnthropic,
    settings: Settings,
) -> PostworthyResult:
    resp = await client.messages.create(
        model=settings.llm_model,
        max_tokens=1024,
        system=[
            {
                "type": "text",
                "text": EXTRACT_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": transcript}],
        output_config={"format": {"type": "json_schema", "schema": EXTRACT_SCHEMA}},
    )
    text_block = next((b for b in resp.content if b.type == "text"), None)
    if text_block is None:
        raise JSONParseError("No text block in model response")
    text = text_block.text
    data = loads_lenient(text)
    result = PostworthyResult.model_validate(data)
    logger.info("postworthy_extract_complete", item_count=len(result.items))
    return result
