"""Pydantic v2 models for the LLM layer's structured outputs.

These types are the typed results shared across the content-generation pipeline:
extract_postworthy and generate_drafts both produce / consume these models.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class PostworthyItem(BaseModel):
    summary: str
    reason: str


class PostworthyResult(BaseModel):
    items: list[PostworthyItem]


class RankedIdea(BaseModel):
    """A scored candidate idea: the idea, the suggested angle, a 0-100 score, and
    1-3 verbatim transcript quotes as evidence for why it surfaced."""

    summary: str
    angle: str
    score: int
    evidence: list[str]


class RankedIdeas(BaseModel):
    ideas: list[RankedIdea]


class Draft(BaseModel):
    platform: Literal["x", "linkedin"] = "x"
    text: str


class DraftSet(BaseModel):
    drafts: list[Draft]


class DraftText(BaseModel):
    """One platform's generated post text (platform is known by the caller)."""

    text: str


class VoiceCard(BaseModel):
    """A per-platform voice profile distilled from a user's real posts."""

    voice_card: str
    positioning: str


class VoiceMemoryNotes(BaseModel):
    """Durable style rules distilled from how a user edited a draft (may be empty)."""

    instructions: list[str]
