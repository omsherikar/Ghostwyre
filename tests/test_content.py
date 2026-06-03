"""Tests for content helpers that don't need a mocked LLM.

The ranking orchestration (rank_channel_ideas / generate_idea_drafts) is covered
in test_content_ranking.py; this file covers the voice-guide loader.
"""

from __future__ import annotations

from pathlib import Path

from app.services.content import load_voice


def test_load_voice_reads_file(tmp_path: Path) -> None:
    guide = tmp_path / "voice.md"
    guide.write_text("My distinctive voice guide.", encoding="utf-8")
    assert load_voice(guide) == "My distinctive voice guide."


def test_load_voice_missing_returns_empty(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.md"
    assert load_voice(missing) == ""
