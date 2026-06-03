"""Tests for per-platform drafting in the user's voice (mocked, no network)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.config import Settings
from app.services.llm import generate_platform_draft
from app.services.schemas import Draft, PostworthyItem


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
async def test_generate_platform_draft_returns_one_draft() -> None:
    client = FakeClient([json.dumps({"text": "Just shipped dark mode. Here's what I learned…"})])
    result = await generate_platform_draft(
        _items(),
        "linkedin",
        voice_card="Casual, witty. No hashtags.",
        positioning="A builder who ships.",
        exemplars=["an old post about shipping"],
        transcript="alice: shipped dark mode today",
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert isinstance(result, Draft)
    assert result.platform == "linkedin"
    assert result.text.startswith("Just shipped")
    assert len(client.messages.calls) == 1


@pytest.mark.asyncio
async def test_generate_platform_draft_retries_once_on_invalid() -> None:
    valid = json.dumps({"text": "We landed the migration with zero downtime."})
    client = FakeClient(["this is not json {", valid])
    result = await generate_platform_draft(
        _items(),
        "x",
        voice_card="Direct and confident.",
        positioning="",
        exemplars=[],
        transcript="carol: we landed the migration",
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert result.text == "We landed the migration with zero downtime."
    assert result.platform == "x"
    assert len(client.messages.calls) == 2


@pytest.mark.asyncio
async def test_generate_platform_draft_prompt_has_voice_strategy_exemplars_grounding() -> None:
    transcript = "alice: shipped X and signups rose 12 percent overnight"
    client = FakeClient([json.dumps({"text": "A post."})])
    settings = _settings()
    await generate_platform_draft(
        _items(),
        "linkedin",
        voice_card="My distinctive voice guide.",
        positioning="Known for shipping.",
        exemplars=["EXEMPLAR-POST-ALPHA"],
        transcript=transcript,
        client=client,  # type: ignore[arg-type]
        settings=settings,
    )
    kwargs = client.messages.calls[0]
    assert kwargs["output_config"]["format"]["type"] == "json_schema"
    assert kwargs["model"] == settings.llm_model
    assert kwargs["max_tokens"] == settings.draft_max_tokens

    system_texts = [b["text"] for b in kwargs["system"]]
    assert any("My distinctive voice guide." in t for t in system_texts)  # voice card
    assert any("LinkedIn" in t for t in system_texts)  # platform strategy
    assert any(b.get("cache_control") == {"type": "ephemeral"} for b in kwargs["system"])

    user_msg = kwargs["messages"][0]["content"]
    assert transcript in user_msg  # grounded in the conversation
    assert "EXEMPLAR-POST-ALPHA" in user_msg  # the user's real post as an exemplar
    assert "Known for shipping." in user_msg  # positioning
