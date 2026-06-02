"""Tests for the pure draft-formatting helper (no I/O)."""

from __future__ import annotations

from app.services.schemas import Draft
from app.slack.commands import format_drafts


def test_format_numbers_and_includes_each_text() -> None:
    drafts = [Draft(text="first post"), Draft(text="second post"), Draft(text="third post")]
    out = format_drafts(drafts)
    assert "Here are 3 drafts" in out
    assert "*1.*" in out
    assert "*2.*" in out
    assert "*3.*" in out
    assert "first post" in out
    assert "second post" in out
    assert "third post" in out
    # Numbering is ordered.
    assert out.index("*1.*") < out.index("*2.*") < out.index("*3.*")


def test_format_single_draft() -> None:
    out = format_drafts([Draft(text="solo")])
    assert "Here are 1 drafts" in out
    assert "*1.*" in out
    assert "*2.*" not in out
    assert "solo" in out


def test_format_empty_list() -> None:
    out = format_drafts([])
    assert "Here are 0 drafts" in out
    assert "*1.*" not in out
