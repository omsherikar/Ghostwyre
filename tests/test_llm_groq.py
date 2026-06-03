"""Tests for the Groq provider path in services/llm.py (mocked, no network).

A hand-rolled fake mimics the Groq (OpenAI-compatible) client shape
(`client.chat.completions.create(...).choices[0].message.content`). The Anthropic
path is covered separately in test_content_extract.py / test_content_drafts.py.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.config import Settings
from app.services.llm import extract_postworthy, generate_drafts
from app.services.schemas import PostworthyItem


class FakeMessage:
    def __init__(self, content: str | None) -> None:
        self.content = content


class FakeChoice:
    def __init__(self, content: str | None) -> None:
        self.message = FakeMessage(content)


class FakeCompletion:
    def __init__(self, content: str | None) -> None:
        self.choices = [FakeChoice(content)]


class FakeCompletions:
    def __init__(self, responses: list[str | None]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> FakeCompletion:
        self.calls.append(kwargs)
        return FakeCompletion(self._responses.pop(0))


class FakeChat:
    def __init__(self, responses: list[str | None]) -> None:
        self.completions = FakeCompletions(responses)


class FakeGroqClient:
    def __init__(self, responses: list[str | None]) -> None:
        self.chat = FakeChat(responses)


def _settings() -> Settings:
    return Settings(llm_provider="groq", groq_api_key="test-key", groq_model="test-model")


@pytest.mark.asyncio
async def test_groq_extract_parses_items() -> None:
    payload = {"items": [{"summary": "Shipped dark mode", "reason": "Feature launch."}]}
    client = FakeGroqClient([json.dumps(payload)])
    result = await extract_postworthy(
        "alice: shipped dark mode today",
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert len(result.items) == 1
    assert result.items[0].summary == "Shipped dark mode"

    # Groq-specific request shape: JSON mode + the configured model, and the system
    # prompt mentions JSON (Groq requires the word for json_object mode).
    kwargs = client.chat.completions.calls[0]
    assert kwargs["model"] == "test-model"
    assert kwargs["response_format"] == {"type": "json_object"}
    system_text = kwargs["messages"][0]["content"]
    assert "json" in system_text.lower()


@pytest.mark.asyncio
async def test_groq_extract_retries_once_then_succeeds() -> None:
    valid = json.dumps({"items": []})
    client = FakeGroqClient(["this is not json {", valid])
    result = await extract_postworthy(
        "bob: lunch at noon?",
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert result.items == []
    assert len(client.chat.completions.calls) == 2


@pytest.mark.asyncio
async def test_groq_none_content_retries_then_raises() -> None:
    from app.services.json_parse import JSONParseError

    client = FakeGroqClient([None, None])
    with pytest.raises(JSONParseError):
        await extract_postworthy(
            "carol: ???",
            client=client,  # type: ignore[arg-type]
            settings=_settings(),
        )
    assert len(client.chat.completions.calls) == 2


@pytest.mark.asyncio
async def test_groq_generate_drafts() -> None:
    payload = {
        "drafts": [
            {"platform": "linkedin", "text": "draft one"},
            {"platform": "x", "text": "draft two"},
        ]
    }
    client = FakeGroqClient([json.dumps(payload)])
    result = await generate_drafts(
        [PostworthyItem(summary="Shipped X", reason="A win.")],
        "Write casually. No hashtags.",
        transcript="alice: shipped X today",
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert [d.text for d in result.drafts] == ["draft one", "draft two"]
    assert [d.platform for d in result.drafts] == ["linkedin", "x"]
