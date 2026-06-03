"""Tests for the ranking orchestration (rank_channel_ideas + generate_idea_drafts).

Mocked, no network — a fake Anthropic-shaped client returns canned JSON per call.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.config import Settings
from app.services.content import (
    RankResult,
    generate_idea_drafts,
    rank_channel_ideas,
    seed_voices,
)
from app.services.schemas import RankedIdea


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


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"llm_provider": "anthropic", "anthropic_api_key": "test-key"}
    base.update(overrides)
    return Settings(**base)


def _idea(summary: str = "Shipped dark mode", angle: str = "Ship ugly.") -> RankedIdea:
    return RankedIdea(summary=summary, angle=angle, score=80, evidence=["alice: shipped"])


@pytest.mark.asyncio
async def test_rank_channel_ideas_nothing_postworthy_skips_ranking() -> None:
    # Empty extraction short-circuits: no ranking call is made.
    client = FakeClient([json.dumps({"items": []})])
    result = await rank_channel_ideas(
        "just standup logistics",
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert result == RankResult(postworthy=False, ideas=[])
    assert len(client.messages.calls) == 1  # extract only, no rank


@pytest.mark.asyncio
async def test_rank_channel_ideas_returns_top_k() -> None:
    extract = json.dumps(
        {"items": [{"summary": "a", "reason": "r"}, {"summary": "b", "reason": "r"}]}
    )
    rank = json.dumps(
        {
            "ideas": [
                {"summary": "a", "angle": "x", "score": 90, "evidence": ["q1"]},
                {"summary": "b", "angle": "y", "score": 70, "evidence": ["q2"]},
                {"summary": "c", "angle": "z", "score": 50, "evidence": ["q3"]},
            ]
        }
    )
    client = FakeClient([extract, rank])
    result = await rank_channel_ideas(
        "we shipped a and b",
        client=client,  # type: ignore[arg-type]
        settings=_settings(idea_shortlist_size=2),  # keep only the top 2
    )
    assert result.postworthy is True
    assert [i.summary for i in result.ideas] == ["a", "b"]  # sliced + order preserved
    assert result.ideas[0].score == 90
    assert len(client.messages.calls) == 2  # extract + rank


@pytest.mark.asyncio
async def test_generate_idea_drafts_one_per_platform() -> None:
    # seed_voices covers x + linkedin -> two draft calls, one per platform.
    draft_a = json.dumps({"text": "x take"})
    draft_b = json.dumps({"text": "linkedin take"})
    client = FakeClient([draft_a, draft_b])
    result = await generate_idea_drafts(
        _idea(),
        "alice: shipped dark mode today",
        seed_voices("Casual voice."),
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert result.postworthy is True
    assert {d.platform for d in result.drafts} == {"x", "linkedin"}
    assert len(client.messages.calls) == 2  # no extract/rank — idea is already chosen


@pytest.mark.asyncio
async def test_generate_idea_drafts_feeds_idea_into_prompt() -> None:
    client = FakeClient([json.dumps({"text": "t"}), json.dumps({"text": "t"})])
    await generate_idea_drafts(
        _idea(summary="Killed a feature", angle="Letting go is the job."),
        "carol: killed the feature nobody used",
        seed_voices("Casual."),
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    user = client.messages.calls[0]["messages"][0]["content"]
    assert "Killed a feature" in user
    assert "Letting go is the job." in user
