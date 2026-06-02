"""Pure tests for the Slack approval-card builder (no DB, no I/O).

ORM instances are constructed in-memory: we instantiate the mapped classes and
set attributes directly (id, status, .drafts list) without a session. The
builder must never touch the network or a DB — it only reads attributes.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from app.db.models import BatchStatus, Draft, DraftBatch, DraftStatus
from app.slack.blocks import build_draft_blocks, fallback_text


def _draft(slot: int, text: str, status: DraftStatus = DraftStatus.pending) -> Draft:
    return Draft(id=uuid.uuid4(), slot_index=slot, text=text, status=status)


def _batch(
    drafts: list[Draft],
    status: BatchStatus = BatchStatus.pending,
    channel: str = "C12345",
) -> DraftBatch:
    return DraftBatch(
        id=uuid.uuid4(),
        slack_channel_id=channel,
        slack_user_id="U999",
        transcript="confidential transcript — never rendered",
        status=status,
        drafts=drafts,
    )


def _actions_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [b for b in blocks if b.get("type") == "actions"]


def _button_action_ids(actions_block: dict[str, Any]) -> list[str]:
    return [el["action_id"] for el in actions_block["elements"]]


def _rendered_text(blocks: list[dict[str, Any]]) -> str:
    return json.dumps(blocks)


def test_returns_list_of_dicts_for_pending_2_draft_batch() -> None:
    batch = _batch([_draft(0, "alpha"), _draft(1, "beta")])
    blocks = build_draft_blocks(batch)
    assert isinstance(blocks, list)
    assert len(blocks) > 0
    assert all(isinstance(b, dict) for b in blocks)


def test_each_draft_actions_block_has_three_buttons() -> None:
    batch = _batch([_draft(0, "alpha"), _draft(1, "beta")])
    blocks = build_draft_blocks(batch)
    actions = _actions_blocks(blocks)
    assert len(actions) == 2
    for ab in actions:
        ids = _button_action_ids(ab)
        assert ids == ["approve_draft", "regenerate_draft", "cancel_batch"]


def test_approve_and_regenerate_values_carry_draft_and_batch_ids() -> None:
    d0 = _draft(0, "alpha")
    d1 = _draft(1, "beta")
    batch = _batch([d0, d1])
    blocks = build_draft_blocks(batch)
    actions = _actions_blocks(blocks)
    first = actions[0]
    by_id = {el["action_id"]: el for el in first["elements"]}

    approve_val = json.loads(by_id["approve_draft"]["value"])
    assert approve_val["b"] == str(batch.id)
    assert approve_val["d"] == str(d0.id)

    regen_val = json.loads(by_id["regenerate_draft"]["value"])
    assert regen_val["b"] == str(batch.id)
    assert regen_val["d"] == str(d0.id)

    cancel_val = json.loads(by_id["cancel_batch"]["value"])
    assert cancel_val["b"] == str(batch.id)
    assert "d" not in cancel_val


def test_draft_text_appears_in_blocks() -> None:
    batch = _batch([_draft(0, "this-is-draft-zero"), _draft(1, "beta")])
    blocks = build_draft_blocks(batch)
    assert "this-is-draft-zero" in _rendered_text(blocks)


def test_slot_label_is_one_based_and_ordered() -> None:
    batch = _batch([_draft(0, "alpha"), _draft(1, "beta")])
    blocks = build_draft_blocks(batch)
    rendered = _rendered_text(blocks)
    assert "*Draft 1*" in rendered
    assert "*Draft 2*" in rendered
    assert rendered.index("*Draft 1*") < rendered.index("*Draft 2*")


def test_over_limit_draft_drops_approve_keeps_regen_and_cancel() -> None:
    long_text = "x" * 281
    batch = _batch([_draft(0, long_text)])
    blocks = build_draft_blocks(batch)
    actions = _actions_blocks(blocks)
    assert len(actions) == 1
    ids = _button_action_ids(actions[0])
    assert "approve_draft" not in ids
    assert ids == ["regenerate_draft", "cancel_batch"]
    assert "too long to publish" in _rendered_text(blocks)


def test_at_limit_draft_keeps_approve() -> None:
    batch = _batch([_draft(0, "y" * 280)])
    blocks = build_draft_blocks(batch)
    ids = _button_action_ids(_actions_blocks(blocks)[0])
    assert "approve_draft" in ids


def test_cancelled_batch_single_section_no_actions() -> None:
    batch = _batch([_draft(0, "alpha")], status=BatchStatus.cancelled)
    blocks = build_draft_blocks(batch)
    assert _actions_blocks(blocks) == []
    sections = [b for b in blocks if b.get("type") == "section"]
    assert len(sections) == 1
    assert "cancelled" in _rendered_text(blocks).lower()


def test_expired_batch_is_terminal_tombstone_no_actions() -> None:
    batch = _batch([_draft(0, "alpha")], status=BatchStatus.expired)
    blocks = build_draft_blocks(batch)
    assert _actions_blocks(blocks) == []
    sections = [b for b in blocks if b.get("type") == "section"]
    assert len(sections) == 1


def test_approved_batch_shows_approved_text_no_actions() -> None:
    approved = _draft(0, "the-chosen-one", status=DraftStatus.approved)
    other = _draft(1, "not-chosen", status=DraftStatus.pending)
    batch = _batch([approved, other], status=BatchStatus.approved)
    blocks = build_draft_blocks(batch)
    assert _actions_blocks(blocks) == []
    rendered = _rendered_text(blocks)
    assert "the-chosen-one" in rendered
    assert "Published (dry-run)" in rendered


def test_regenerating_draft_renders_placeholder_no_buttons() -> None:
    regenerating = _draft(0, "old-text", status=DraftStatus.regenerating)
    normal = _draft(1, "beta")
    batch = _batch([regenerating, normal])
    blocks = build_draft_blocks(batch)
    rendered = _rendered_text(blocks)
    assert "Regenerating" in rendered
    # The old text must not be shown for a regenerating slot.
    assert "old-text" not in rendered
    # Slot label still shown.
    assert "*Draft 1*" in rendered
    # Only the normal draft (slot 1) keeps an actions block.
    assert len(_actions_blocks(blocks)) == 1


def test_fallback_text_is_nonempty_string_with_count() -> None:
    batch = _batch([_draft(0, "alpha"), _draft(1, "beta")])
    text = fallback_text(batch)
    assert isinstance(text, str)
    assert text.strip()
    assert "2" in text
