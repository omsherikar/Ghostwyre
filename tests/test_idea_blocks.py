"""Pure tests for the ranked-idea card builder (no DB, no I/O).

A `DraftBatch` is built in-memory with `candidate_ideas` set directly; the builder
only reads attributes.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from app.db.models import BatchStatus, DraftBatch
from app.slack.blocks import (
    PICK_ACTION_ID,
    build_draft_blocks,
    build_idea_blocks,
    fallback_text,
    idea_fallback_text,
)

IDEAS: list[dict[str, Any]] = [
    {
        "summary": "Shipped dark mode",
        "angle": "Ship ugly, polish later.",
        "score": 88,
        "evidence": ["alice: shipped dark mode today", "bob: users love it"],
    },
    {
        "summary": "Killed a feature",
        "angle": "Saying no is the job.",
        "score": 71,
        "evidence": ["carol: nobody used the export"],
    },
]


def _batch(ideas: list[dict[str, Any]], channel: str = "C12345") -> DraftBatch:
    return DraftBatch(
        id=uuid.uuid4(),
        slack_channel_id=channel,
        slack_user_id="U999",
        transcript="confidential transcript — never rendered",
        status=BatchStatus.selecting,
        candidate_ideas=ideas,
        drafts=[],
    )


def _actions(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [b for b in blocks if b.get("type") == "actions"]


def test_one_pick_button_per_idea_plus_cancel() -> None:
    batch = _batch(IDEAS)
    blocks = build_idea_blocks(batch)
    pick_buttons = [
        e for b in _actions(blocks) for e in b["elements"] if e.get("action_id") == PICK_ACTION_ID
    ]
    assert len(pick_buttons) == 2  # one per idea
    cancel = [
        e for b in _actions(blocks) for e in b["elements"] if e.get("action_id") == "cancel_batch"
    ]
    assert len(cancel) == 1


def test_pick_button_values_are_ids_and_index_only() -> None:
    batch = _batch(IDEAS)
    blocks = build_idea_blocks(batch)
    pick_buttons = [
        e for b in _actions(blocks) for e in b["elements"] if e.get("action_id") == PICK_ACTION_ID
    ]
    for i, button in enumerate(pick_buttons):
        decoded = json.loads(button["value"])
        assert decoded == {"b": str(batch.id), "i": str(i)}
    # The idea text must never appear in a button value (ids-only rule).
    all_values = " ".join(e["value"] for b in _actions(blocks) for e in b["elements"])
    assert "Shipped dark mode" not in all_values


def test_evidence_quotes_render_in_card() -> None:
    batch = _batch(IDEAS)
    rendered = json.dumps(build_idea_blocks(batch))
    assert "alice: shipped dark mode today" in rendered
    assert "carol: nobody used the export" in rendered
    # The score and angle surface too.
    assert "88" in rendered
    assert "Ship ugly, polish later." in rendered


def test_idea_fallback_text_counts() -> None:
    assert "2 ideas" in idea_fallback_text(_batch(IDEAS))
    assert "1 idea" in idea_fallback_text(_batch(IDEAS[:1]))


def test_build_draft_blocks_renders_idea_card_for_selecting() -> None:
    # A `selecting` batch has no drafts; build_draft_blocks must route to the idea
    # picker, never a degenerate buttonless "drafts" card (regression guard).
    batch = _batch(IDEAS)  # status=selecting
    rendered = json.dumps(build_draft_blocks(batch))
    assert PICK_ACTION_ID in rendered
    assert "Shipped dark mode" in rendered


def test_fallback_text_for_selecting_is_idea_text() -> None:
    assert "ideas worth posting" in fallback_text(_batch(IDEAS))
