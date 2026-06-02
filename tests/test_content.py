"""Tests for content orchestration (mocked, no network)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.services.content import ContentResult, generate_post_drafts, load_voice


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


@pytest.mark.asyncio
async def test_nothing_postworthy_skips_drafting() -> None:
    client = FakeClient([json.dumps({"items": []})])
    result = await generate_post_drafts(
        "just some standup logistics and small talk",
        "Casual voice.",
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert result == ContentResult(postworthy=False, drafts=[])
    # generate_drafts must be skipped: only the extract call happened.
    assert len(client.messages.calls) == 1


@pytest.mark.asyncio
async def test_postworthy_produces_drafts() -> None:
    extract = json.dumps(
        {
            "items": [
                {"summary": "Shipped dark mode", "reason": "A feature launch."},
                {"summary": "Fixed a race condition", "reason": "Interesting bug fix."},
            ]
        }
    )
    drafts = json.dumps(
        {
            "drafts": [
                {"text": "Just shipped dark mode."},
                {"text": "Squashed a nasty race condition today."},
            ]
        }
    )
    client = FakeClient([extract, drafts])
    result = await generate_post_drafts(
        "we shipped dark mode and fixed a race condition",
        "Casual voice.",
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert result.postworthy is True
    assert [d.text for d in result.drafts] == [
        "Just shipped dark mode.",
        "Squashed a nasty race condition today.",
    ]
    # Both extract and draft calls happened.
    assert len(client.messages.calls) == 2


def test_load_voice_reads_file(tmp_path: Path) -> None:
    guide = tmp_path / "voice.md"
    guide.write_text("My distinctive voice guide.", encoding="utf-8")
    assert load_voice(guide) == "My distinctive voice guide."


def test_load_voice_missing_returns_empty(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.md"
    assert load_voice(missing) == ""
