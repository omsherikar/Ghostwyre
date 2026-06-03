"""Pure tests for the Slack approval-card builder (no DB, no I/O).

ORM instances are constructed in-memory: we instantiate the mapped classes and
set attributes directly (id, status, .drafts list) without a session. The
builder must never touch the network or a DB — it only reads attributes.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from app.db.models import (
    ApprovalAction,
    ApprovalEvent,
    BatchStatus,
    Draft,
    DraftBatch,
    DraftPlatform,
    DraftStatus,
)
from app.slack.blocks import (
    build_draft_blocks,
    build_history_blocks,
    fallback_text,
    history_fallback,
)


def _draft(
    slot: int,
    text: str,
    status: DraftStatus = DraftStatus.pending,
    platform: DraftPlatform = DraftPlatform.x,
) -> Draft:
    return Draft(id=uuid.uuid4(), slot_index=slot, text=text, status=status, platform=platform)


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


def test_each_draft_actions_block_has_four_buttons() -> None:
    batch = _batch([_draft(0, "alpha"), _draft(1, "beta")])
    blocks = build_draft_blocks(batch)
    actions = _actions_blocks(blocks)
    assert len(actions) == 2
    for ab in actions:
        ids = _button_action_ids(ab)
        assert ids == ["approve_draft", "edit_draft", "regenerate_draft", "cancel_batch"]


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


def test_platform_labels_and_order() -> None:
    batch = _batch(
        [
            _draft(0, "a developed post", platform=DraftPlatform.linkedin),
            _draft(1, "a punchy take", platform=DraftPlatform.x),
        ]
    )
    rendered = _rendered_text(build_draft_blocks(batch))
    assert "*LinkedIn draft*" in rendered
    assert "*X draft*" in rendered
    assert rendered.index("*LinkedIn draft*") < rendered.index("*X draft*")


def test_linkedin_draft_is_copy_only_no_approve() -> None:
    batch = _batch([_draft(0, "a developed linkedin post", platform=DraftPlatform.linkedin)])
    blocks = build_draft_blocks(batch)
    ids = _button_action_ids(_actions_blocks(blocks)[0])
    assert "approve_draft" not in ids  # no LinkedIn publisher — copy-only
    assert ids == ["edit_draft", "regenerate_draft", "cancel_batch"]
    assert "copy & paste into linkedin" in _rendered_text(blocks).lower()


def test_over_limit_x_draft_drops_approve_keeps_regen_and_cancel() -> None:
    batch = _batch([_draft(0, "x" * 281, platform=DraftPlatform.x)])
    blocks = build_draft_blocks(batch, x_char_limit=280)
    actions = _actions_blocks(blocks)
    assert len(actions) == 1
    ids = _button_action_ids(actions[0])
    assert "approve_draft" not in ids
    # Still editable (well under the modal limit), so Edit stays alongside regen/cancel.
    assert ids == ["edit_draft", "regenerate_draft", "cancel_batch"]
    assert "too long to auto-publish" in _rendered_text(blocks).lower()


def test_at_limit_x_draft_keeps_approve() -> None:
    batch = _batch([_draft(0, "y" * 280, platform=DraftPlatform.x)])
    blocks = build_draft_blocks(batch, x_char_limit=280)
    ids = _button_action_ids(_actions_blocks(blocks)[0])
    assert "approve_draft" in ids


def test_long_x_draft_publishable_when_limit_raised() -> None:
    # With a raised X limit (Premium), a long X draft keeps its Approve button.
    batch = _batch([_draft(0, "z" * 1000, platform=DraftPlatform.x)])
    blocks = build_draft_blocks(batch, x_char_limit=25000)
    ids = _button_action_ids(_actions_blocks(blocks)[0])
    assert "approve_draft" in ids
    assert "characters" in _rendered_text(blocks)


def test_edit_button_carries_batch_and_draft_ids() -> None:
    d0 = _draft(0, "alpha")
    batch = _batch([d0])
    by_id = {
        el["action_id"]: el for el in _actions_blocks(build_draft_blocks(batch))[0]["elements"]
    }
    assert json.loads(by_id["edit_draft"]["value"]) == {"b": str(batch.id), "d": str(d0.id)}


def test_very_long_draft_drops_edit_button() -> None:
    from app.slack.blocks import EDIT_MAX_LEN

    batch = _batch([_draft(0, "z" * (EDIT_MAX_LEN + 1), platform=DraftPlatform.x)])
    blocks = build_draft_blocks(batch, x_char_limit=25000)
    ids = _button_action_ids(_actions_blocks(blocks)[0])
    assert "edit_draft" not in ids  # too long for Slack's modal input
    assert "too long to edit in slack" in _rendered_text(blocks).lower()


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


def test_approved_batch_keeps_all_drafts_no_actions() -> None:
    # Approval is terminal (no buttons) but must NOT discard the other drafts: the
    # published X draft is marked published; the LinkedIn draft stays copy-paste.
    approved = _draft(0, "the-chosen-one", status=DraftStatus.approved, platform=DraftPlatform.x)
    other = _draft(1, "still-copyable", status=DraftStatus.pending, platform=DraftPlatform.linkedin)
    batch = _batch([approved, other], status=BatchStatus.approved)
    blocks = build_draft_blocks(batch)
    assert _actions_blocks(blocks) == []  # terminal, no buttons
    rendered = _rendered_text(blocks)
    assert "the-chosen-one" in rendered  # published draft still shown
    assert "Published" in rendered  # …and marked published
    assert "still-copyable" in rendered  # the LinkedIn draft is NOT lost
    assert "Copy & paste" in rendered  # …offered as copy-paste


def test_regenerating_draft_renders_placeholder_no_buttons() -> None:
    regenerating = _draft(0, "old-text", status=DraftStatus.regenerating)
    normal = _draft(1, "beta")
    batch = _batch([regenerating, normal])
    blocks = build_draft_blocks(batch)
    rendered = _rendered_text(blocks)
    assert "Regenerating" in rendered
    # The old text must not be shown for a regenerating slot.
    assert "old-text" not in rendered
    # Platform label still shown (both default to X here).
    assert "*X draft*" in rendered
    # Only the normal draft (slot 1) keeps an actions block.
    assert len(_actions_blocks(blocks)) == 1


def test_fallback_text_is_nonempty_string_with_count() -> None:
    batch = _batch([_draft(0, "alpha"), _draft(1, "beta")])
    text = fallback_text(batch)
    assert isinstance(text, str)
    assert text.strip()
    assert "2" in text


# --- history renderer ------------------------------------------------------- #


def _event(
    text: str | None,
    *,
    url: str | None = "https://x.test/1",
    channel: str = "C12345",
    when: datetime | None = None,
) -> ApprovalEvent:
    draft = _draft(0, text) if text is not None else None
    return ApprovalEvent(
        id=uuid.uuid4(),
        action=ApprovalAction.approve,
        slack_user_id="U999",
        publish_url=url,
        created_at=when or datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        draft=draft,
        batch=_batch([], channel=channel),
    )


def test_history_renders_posts_newest_first_with_links() -> None:
    e_new = _event("second post", url="https://x.test/2")
    e_old = _event("first post", url="https://x.test/1")
    rendered = _rendered_text(build_history_blocks([e_new, e_old], []))
    assert "second post" in rendered and "first post" in rendered
    assert "https://x.test/2" in rendered and "https://x.test/1" in rendered
    # order is preserved as given (caller passes newest-first)
    assert rendered.index("second post") < rendered.index("first post")


def test_history_warns_about_unconfirmed_only_when_present() -> None:
    published = [_event("a post")]
    with_warning = _rendered_text(build_history_blocks(published, [_batch([])]))
    assert "couldn't be confirmed" in with_warning

    without_warning = _rendered_text(build_history_blocks(published, []))
    assert "couldn't be confirmed" not in without_warning


def test_history_empty_state() -> None:
    rendered = _rendered_text(build_history_blocks([], []))
    assert "No published posts yet" in rendered


def test_history_missing_draft_renders_placeholder() -> None:
    rendered = _rendered_text(build_history_blocks([_event(None)], []))
    assert "text unavailable" in rendered


def test_history_fallback_wording() -> None:
    assert "no published posts" in history_fallback(0).lower()
    assert "1" in history_fallback(1)
    assert "3" in history_fallback(3)
