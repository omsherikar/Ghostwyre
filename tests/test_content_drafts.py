"""Tests for LLM step B — drafting in the user's voice (mocked, no network)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.config import Settings
from app.services.llm import generate_drafts
from app.services.schemas import DraftSet, PostworthyItem


class FakeTextBlock:
    """A response content block that mimics an Anthropic text block."""

    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class FakeResponse:
    def __init__(self, text: str) -> None:
        self.content = [FakeTextBlock(text)]


class FakeMessages:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        text = self._responses.pop(0)
        return FakeResponse(text)


class FakeClient:
    def __init__(self, responses: list[str]) -> None:
        self.messages = FakeMessages(responses)


def _settings() -> Settings:
    return Settings(llm_provider="anthropic", anthropic_api_key="test-key")


def _items() -> list[PostworthyItem]:
    return [
        PostworthyItem(summary="Shipped dark mode", reason="A user-visible feature launch."),
        PostworthyItem(summary="Fixed a race condition", reason="Interesting bug fix."),
    ]


@pytest.mark.asyncio
async def test_generate_returns_parsed_drafts_per_platform() -> None:
    payload = {
        "drafts": [
            {"platform": "linkedin", "text": "Just shipped dark mode. Here's what I learned…"},
            {"platform": "x", "text": "Spent the day hunting a race condition. The race is over."},
        ]
    }
    client = FakeClient([json.dumps(payload)])
    result = await generate_drafts(
        _items(),
        "Casual, witty, lowercase-friendly. No hashtags.",
        transcript="alice: shipped dark mode today",
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert isinstance(result, DraftSet)
    assert len(result.drafts) == 2
    assert {d.platform for d in result.drafts} == {"linkedin", "x"}
    assert result.drafts[0].platform == "linkedin"
    assert len(client.messages.calls) == 1


@pytest.mark.asyncio
async def test_generate_retries_once_on_invalid_then_succeeds() -> None:
    valid = json.dumps(
        {"drafts": [{"platform": "x", "text": "We landed the migration with zero downtime."}]}
    )
    client = FakeClient(["this is not json {", valid])
    result = await generate_drafts(
        _items(),
        "Direct and confident.",
        transcript="carol: we landed the migration",
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert len(result.drafts) == 1
    assert result.drafts[0].text == "We landed the migration with zero downtime."
    assert result.drafts[0].platform == "x"
    assert len(client.messages.calls) == 2


@pytest.mark.asyncio
async def test_generate_passes_structured_output_cached_voice_and_grounding() -> None:
    transcript = "alice: shipped X and signups rose 12 percent overnight"
    client = FakeClient([json.dumps({"drafts": [{"platform": "x", "text": "A post."}]})])
    settings = _settings()
    await generate_drafts(
        _items(),
        "My distinctive voice guide.",
        transcript=transcript,
        client=client,  # type: ignore[arg-type]
        settings=settings,
    )
    kwargs = client.messages.calls[0]
    assert kwargs["output_config"]["format"]["type"] == "json_schema"
    assert "schema" in kwargs["output_config"]["format"]
    assert kwargs["model"] == settings.llm_model
    # Drafting uses the larger token budget for long-form output.
    assert kwargs["max_tokens"] == settings.draft_max_tokens
    # The transcript is fed into the draft prompt so drafts are grounded.
    assert transcript in kwargs["messages"][0]["content"]
    system = kwargs["system"]
    voice_block = system[1]
    assert voice_block["text"] == "My distinctive voice guide."
    assert voice_block["cache_control"] == {"type": "ephemeral"}
