"""Tests for LLM step A — postworthy extraction (mocked, no network)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.config import Settings
from app.services.json_parse import JSONParseError
from app.services.llm import extract_postworthy
from app.services.schemas import PostworthyResult


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


@pytest.mark.asyncio
async def test_extract_returns_parsed_items() -> None:
    payload = {
        "items": [
            {"summary": "Shipped dark mode", "reason": "A user-visible feature launch."},
            {"summary": "Fixed a nasty race condition", "reason": "Interesting bug fix."},
        ]
    }
    client = FakeClient([json.dumps(payload)])
    result = await extract_postworthy(
        "alice: shipped dark mode today",
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert isinstance(result, PostworthyResult)
    assert len(result.items) == 2
    assert result.items[0].summary == "Shipped dark mode"
    assert result.items[1].reason == "Interesting bug fix."
    assert len(client.messages.calls) == 1


@pytest.mark.asyncio
async def test_extract_nothing_postworthy() -> None:
    client = FakeClient([json.dumps({"items": []})])
    result = await extract_postworthy(
        "bob: lunch at noon?",
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert result == PostworthyResult(items=[])
    assert len(client.messages.calls) == 1


@pytest.mark.asyncio
async def test_extract_retries_once_on_invalid_then_succeeds() -> None:
    valid = json.dumps({"items": [{"summary": "A win", "reason": "It's a win."}]})
    client = FakeClient(["this is not json {", valid])
    result = await extract_postworthy(
        "carol: we landed the migration",
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert len(result.items) == 1
    assert result.items[0].summary == "A win"
    assert len(client.messages.calls) == 2


@pytest.mark.asyncio
async def test_extract_two_invalid_responses_raise() -> None:
    client = FakeClient(["not json", "still not json"])
    with pytest.raises(JSONParseError):
        await extract_postworthy(
            "dave: ???",
            client=client,  # type: ignore[arg-type]
            settings=_settings(),
        )
    assert len(client.messages.calls) == 2


@pytest.mark.asyncio
async def test_extract_passes_structured_output_and_cache_control() -> None:
    client = FakeClient([json.dumps({"items": []})])
    await extract_postworthy(
        "erin: standup at 10",
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    kwargs = client.messages.calls[0]
    assert kwargs["output_config"]["format"]["type"] == "json_schema"
    assert "schema" in kwargs["output_config"]["format"]
    system_block = kwargs["system"][0]
    assert system_block["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_extract_retries_once_on_validation_error_then_succeeds() -> None:
    # First response: structurally valid JSON but wrong types (summary is int, reason is null)
    invalid_types = json.dumps({"items": [{"summary": 123, "reason": None}]})
    valid = json.dumps({"items": [{"summary": "Shipped feature X", "reason": "Visible win."}]})
    client = FakeClient([invalid_types, valid])
    result = await extract_postworthy(
        "frank: shipped feature X today",
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert len(result.items) == 1
    assert result.items[0].summary == "Shipped feature X"
    assert len(client.messages.calls) == 2


@pytest.mark.asyncio
async def test_extract_retries_once_on_missing_text_block_then_succeeds() -> None:
    """A response with no text block should trigger a retry (Fix 1 / PEP 479)."""

    class FakeEmptyResponse:
        content: list[Any] = []

    class FakeMixedMessages:
        def __init__(self, valid_text: str) -> None:
            self._valid_text = valid_text
            self.calls: list[dict[str, Any]] = []
            self._call_count = 0

        async def create(self, **kwargs: Any) -> Any:
            self.calls.append(kwargs)
            self._call_count += 1
            if self._call_count == 1:
                return FakeEmptyResponse()
            return FakeResponse(self._valid_text)

    class FakeMixedClient:
        def __init__(self, valid_text: str) -> None:
            self.messages = FakeMixedMessages(valid_text)

    valid = json.dumps({"items": [{"summary": "Big win", "reason": "Worth sharing."}]})
    client = FakeMixedClient(valid)
    result = await extract_postworthy(
        "grace: big release today",
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert len(result.items) == 1
    assert result.items[0].summary == "Big win"
    assert len(client.messages.calls) == 2
