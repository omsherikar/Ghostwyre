"""Tests for the idea-ranking LLM step (rank_ideas) — mocked, no network."""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.config import Settings
from app.services.json_parse import JSONParseError
from app.services.llm import rank_ideas
from app.services.schemas import PostworthyItem, RankedIdeas


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


def _items() -> list[PostworthyItem]:
    return [
        PostworthyItem(summary="Shipped dark mode", reason="A launch."),
        PostworthyItem(summary="Fixed a race condition", reason="A bug fix."),
    ]


@pytest.mark.asyncio
async def test_rank_returns_parsed_ranked_ideas() -> None:
    payload = {
        "ideas": [
            {
                "summary": "Shipped dark mode",
                "angle": "Ship ugly, polish later.",
                "score": 82,
                "evidence": ["alice: shipped dark mode today"],
            },
            {
                "summary": "Fixed a race condition",
                "angle": "The bug is never where you look.",
                "score": 64,
                "evidence": ["bob: it was a trailing slash"],
            },
        ]
    }
    client = FakeClient([json.dumps(payload)])
    result = await rank_ideas(
        _items(),
        "alice: shipped dark mode today\nbob: it was a trailing slash",
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert isinstance(result, RankedIdeas)
    assert len(result.ideas) == 2
    # Returned in the order the model gave (highest score first).
    assert result.ideas[0].score == 82
    assert result.ideas[0].angle == "Ship ugly, polish later."
    assert result.ideas[0].evidence == ["alice: shipped dark mode today"]
    assert len(client.messages.calls) == 1


@pytest.mark.asyncio
async def test_rank_retries_once_on_invalid_then_succeeds() -> None:
    valid = json.dumps(
        {"ideas": [{"summary": "A win", "angle": "Share it.", "score": 70, "evidence": ["x: win"]}]}
    )
    client = FakeClient(["not json {", valid])
    result = await rank_ideas(
        _items(),
        "x: win",
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert len(result.ideas) == 1
    assert result.ideas[0].summary == "A win"
    assert len(client.messages.calls) == 2


@pytest.mark.asyncio
async def test_rank_two_invalid_responses_raise() -> None:
    client = FakeClient(["nope", "still nope"])
    with pytest.raises(JSONParseError):
        await rank_ideas(
            _items(),
            "transcript",
            client=client,  # type: ignore[arg-type]
            settings=_settings(),
        )
    assert len(client.messages.calls) == 2


@pytest.mark.asyncio
async def test_rank_prompt_carries_candidates_transcript_and_cache_control() -> None:
    client = FakeClient([json.dumps({"ideas": []})])
    await rank_ideas(
        _items(),
        "alice: shipped dark mode today",
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    kwargs = client.messages.calls[0]
    # Structured output + cached system block, like the other steps.
    assert kwargs["output_config"]["format"]["type"] == "json_schema"
    assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}
    # The candidate ideas and the transcript both reach the user prompt.
    user = kwargs["messages"][0]["content"]
    assert "Shipped dark mode" in user
    assert "alice: shipped dark mode today" in user
