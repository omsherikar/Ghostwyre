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
