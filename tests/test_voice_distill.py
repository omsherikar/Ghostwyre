"""Tests for voice-card distillation (mocked, no network)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.config import Settings
from app.services.json_parse import JSONParseError
from app.services.llm import distill_voice_profile
from app.services.schemas import VoiceCard


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


async def test_distill_returns_voice_card_and_grounds_in_posts() -> None:
    payload = {
        "voice_card": "Short sentences. No hashtags. Lead with the lesson.",
        "positioning": "A builder who ships and shares the messy middle.",
    }
    client = FakeClient([json.dumps(payload)])
    result = await distill_voice_profile(
        ["shipped onboarding v2 today", "the bug was a trailing slash"],
        "be known for shipping fast",
        "x",
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert isinstance(result, VoiceCard)
    assert "hashtags" in result.voice_card.lower()
    assert result.positioning.startswith("A builder")

    # The user's real posts + platform + goal reach the prompt.
    user_msg = client.messages.calls[0]["messages"][0]["content"]
    assert "shipped onboarding v2 today" in user_msg
    assert "shipping fast" in user_msg
    assert "x" in user_msg.lower()


async def test_distill_retries_once_on_invalid_then_succeeds() -> None:
    valid = json.dumps({"voice_card": "vc", "positioning": "pos"})
    client = FakeClient(["this is not json {", valid])
    result = await distill_voice_profile(
        ["a post"],
        "",
        "linkedin",
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert result.voice_card == "vc"
    assert len(client.messages.calls) == 2


async def test_distill_two_invalid_responses_raise() -> None:
    client = FakeClient(["nope", "still nope"])
    with pytest.raises(JSONParseError):
        await distill_voice_profile(
            ["a post"],
            "",
            "x",
            client=client,  # type: ignore[arg-type]
            settings=_settings(),
        )
    assert len(client.messages.calls) == 2
