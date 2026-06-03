"""Tests for distill_edit_memory (mocked, no network).

Given the draft we wrote and the user's edited version, the LLM returns durable
style rules (or [] for a trivial edit). Mirrors the other structured-output tests.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.config import Settings
from app.services.json_parse import JSONParseError
from app.services.llm import distill_edit_memory
from app.services.schemas import VoiceMemoryNotes


class FakeTextBlock:
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
        return FakeResponse(self._responses.pop(0))


class FakeClient:
    def __init__(self, responses: list[str]) -> None:
        self.messages = FakeMessages(responses)


def _settings() -> Settings:
    return Settings(llm_provider="anthropic", anthropic_api_key="test-key")


@pytest.mark.asyncio
async def test_distill_returns_rules() -> None:
    payload = {"instructions": ["Don't open with 'I'.", "No emojis."]}
    client = FakeClient([json.dumps(payload)])
    result = await distill_edit_memory(
        "I shipped dark mode 🎉",
        "Shipped dark mode.",
        "x",
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert isinstance(result, VoiceMemoryNotes)
    assert result.instructions == ["Don't open with 'I'.", "No emojis."]
    assert len(client.messages.calls) == 1


@pytest.mark.asyncio
async def test_distill_trivial_edit_returns_empty() -> None:
    client = FakeClient([json.dumps({"instructions": []})])
    result = await distill_edit_memory(
        "Shipped dark mode.",
        "Shipped dark mode!",
        "x",
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert result.instructions == []


@pytest.mark.asyncio
async def test_distill_retries_once_then_succeeds() -> None:
    valid = json.dumps({"instructions": ["Shorter paragraphs."]})
    client = FakeClient(["not json {", valid])
    result = await distill_edit_memory(
        "orig",
        "edited",
        "linkedin",
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert result.instructions == ["Shorter paragraphs."]
    assert len(client.messages.calls) == 2


@pytest.mark.asyncio
async def test_distill_two_invalid_responses_raise() -> None:
    client = FakeClient(["nope", "still nope"])
    with pytest.raises(JSONParseError):
        await distill_edit_memory(
            "orig",
            "edited",
            "x",
            client=client,  # type: ignore[arg-type]
            settings=_settings(),
        )
    assert len(client.messages.calls) == 2


@pytest.mark.asyncio
async def test_distill_prompt_carries_both_versions() -> None:
    client = FakeClient([json.dumps({"instructions": []})])
    await distill_edit_memory(
        "ORIGINAL DRAFT TEXT",
        "EDITED DRAFT TEXT",
        "linkedin",
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    user = client.messages.calls[0]["messages"][0]["content"]
    assert "ORIGINAL DRAFT TEXT" in user
    assert "EDITED DRAFT TEXT" in user
    assert "linkedin" in user
