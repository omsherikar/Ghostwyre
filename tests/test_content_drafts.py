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
    return Settings(anthropic_api_key="test-key")


def _items() -> list[PostworthyItem]:
    return [
        PostworthyItem(summary="Shipped dark mode", reason="A user-visible feature launch."),
        PostworthyItem(summary="Fixed a race condition", reason="Interesting bug fix."),
    ]


@pytest.mark.asyncio
async def test_generate_returns_parsed_drafts() -> None:
    payload = {
        "drafts": [
            {"text": "Just shipped dark mode. Your retinas can thank me later."},
            {"text": "Spent the day hunting a race condition. The race is over. We won."},
        ]
    }
    client = FakeClient([json.dumps(payload)])
    result = await generate_drafts(
        _items(),
        "Casual, witty, lowercase-friendly. No hashtags.",
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert isinstance(result, DraftSet)
    assert len(result.drafts) == 2
    assert result.drafts[0].text.startswith("Just shipped dark mode")
    assert "race is over" in result.drafts[1].text
    assert len(client.messages.calls) == 1


@pytest.mark.asyncio
async def test_generate_retries_once_on_invalid_then_succeeds() -> None:
    valid = json.dumps({"drafts": [{"text": "We landed the migration with zero downtime."}]})
    client = FakeClient(["this is not json {", valid])
    result = await generate_drafts(
        _items(),
        "Direct and confident.",
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert len(result.drafts) == 1
    assert result.drafts[0].text == "We landed the migration with zero downtime."
    assert len(client.messages.calls) == 2


@pytest.mark.asyncio
async def test_generate_passes_structured_output_model_and_cached_voice() -> None:
    client = FakeClient([json.dumps({"drafts": [{"text": "A post."}]})])
    settings = _settings()
    await generate_drafts(
        _items(),
        "My distinctive voice guide.",
        client=client,  # type: ignore[arg-type]
        settings=settings,
    )
    kwargs = client.messages.calls[0]
    assert kwargs["output_config"]["format"]["type"] == "json_schema"
    assert "schema" in kwargs["output_config"]["format"]
    assert kwargs["model"] == settings.llm_model
    system = kwargs["system"]
    voice_block = system[1]
    assert voice_block["text"] == "My distinctive voice guide."
    assert voice_block["cache_control"] == {"type": "ephemeral"}
