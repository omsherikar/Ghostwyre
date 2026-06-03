"""Pure Block Kit builder for the approval card (no I/O).

`build_draft_blocks` turns an ORM `DraftBatch` (with its child `.drafts` and a
`.status`) into a Slack Block Kit blocks list. It is deliberately pure: it only
reads attributes off the passed-in instances — no DB session, no network, no
lookups. The actions handlers persist state and then re-render by calling this
again, so every render is derived solely from the batch's current attributes.

Card lifecycle (one living card per batch, edited in place):
- pending   -> header + context + per-draft sections with approve/regenerate/cancel.
- approved  -> the approved draft's text + a "Published (dry-run)." line, no buttons.
- cancelled/expired -> a single tombstone section, no buttons.

Button values are compact JSON (no whitespace) carrying only ids — never the
transcript or draft text (CONFIDENTIAL at rest). `fallback_text` supplies the
plain-text notification line the caller passes to chat_postMessage/chat_update.
"""

from __future__ import annotations

import json
from typing import Any

from app.db.models import (
    ApprovalEvent,
    BatchStatus,
    Draft,
    DraftBatch,
    DraftPlatform,
    DraftStatus,
)
from app.platforms import PLATFORMS
from app.services.publisher import MAX_TWEET_LEN

APPROVE_ACTION_ID = "approve_draft"
REGENERATE_ACTION_ID = "regenerate_draft"
CANCEL_ACTION_ID = "cancel_batch"
PICK_ACTION_ID = "pick_idea"


def _encode_value(**fields: str) -> str:
    """Compact-JSON-encode a button value (ids only — never message content)."""
    return json.dumps(fields, separators=(",", ":"))


def fallback_text(batch: DraftBatch) -> str:
    """Short plain-text notification line for the posted/updated card."""
    n = len(batch.drafts)
    if batch.status == BatchStatus.selecting:
        return idea_fallback_text(batch)
    if batch.status == BatchStatus.cancelled:
        return "Ghostwyre: drafts cancelled — nothing was published."
    if batch.status == BatchStatus.approved:
        return "Ghostwyre: draft approved and published (dry-run)."
    return f"Ghostwyre drafted {n} posts — approve, regenerate, or cancel."


def _header(text: str) -> dict[str, Any]:
    return {"type": "header", "text": {"type": "plain_text", "text": text}}


def _section(mrkdwn: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": mrkdwn}}


def _context(mrkdwn: str) -> dict[str, Any]:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": mrkdwn}]}


def _divider() -> dict[str, Any]:
    return {"type": "divider"}


def _approve_button(batch: DraftBatch, draft: Draft) -> dict[str, Any]:
    return {
        "type": "button",
        "action_id": APPROVE_ACTION_ID,
        "text": {"type": "plain_text", "text": "Approve"},
        "style": "primary",
        "value": _encode_value(b=str(batch.id), d=str(draft.id)),
        "confirm": {
            "title": {"type": "plain_text", "text": "Publish draft"},
            "text": {"type": "mrkdwn", "text": "Publish this draft? (dry-run for now)"},
            "confirm": {"type": "plain_text", "text": "Publish"},
            "deny": {"type": "plain_text", "text": "Keep editing"},
        },
    }


def _regenerate_button(batch: DraftBatch, draft: Draft) -> dict[str, Any]:
    return {
        "type": "button",
        "action_id": REGENERATE_ACTION_ID,
        "text": {"type": "plain_text", "text": "Regenerate"},
        "value": _encode_value(b=str(batch.id), d=str(draft.id)),
    }


def _cancel_button(batch: DraftBatch) -> dict[str, Any]:
    return {
        "type": "button",
        "action_id": CANCEL_ACTION_ID,
        "text": {"type": "plain_text", "text": "Cancel"},
        "style": "danger",
        "value": _encode_value(b=str(batch.id)),
    }


def _pending_draft_blocks(
    batch: DraftBatch, draft: Draft, x_char_limit: int
) -> list[dict[str, Any]]:
    """Render one pending draft: divider, platform-labelled section, char count,
    and platform-appropriate actions.

    Approve appears only for a *publishable* platform (X) whose draft is within the
    effective limit (`x_char_limit`, raisable for X Premium). LinkedIn and
    over-limit X drafts are copy-paste only. Regenerate/Cancel always show.
    """
    spec = PLATFORMS[str(draft.platform or DraftPlatform.x)]
    label = f"*{spec.label} draft*"
    blocks: list[dict[str, Any]] = [_divider()]

    if draft.status == DraftStatus.regenerating:
        # Placeholder — don't render the stale text, and offer no buttons.
        blocks.append(_section(f"{label}\n_Regenerating…_"))
        return blocks

    blocks.append(_section(f"{label}\n{draft.text}"))

    char_count = len(draft.text)
    # X's limit is configurable (Premium); other platforms use the registry value.
    limit = x_char_limit if spec.key == "x" else spec.char_limit
    over_limit = char_count > limit

    count_line = f"{char_count} characters"
    if over_limit:
        count_line += f" — over the {spec.label} limit ({limit})"
    blocks.append(_context(count_line))

    elements: list[dict[str, Any]] = []
    if spec.publishable and not over_limit:
        elements.append(_approve_button(batch, draft))
    elif not spec.publishable:
        blocks.append(_context(f"Copy & paste into {spec.label} — no auto-publish."))
    else:  # publishable (X) but over the limit
        blocks.append(
            _context("Too long to auto-publish — copy & paste, or raise X_CHAR_LIMIT (X Premium).")
        )
    elements.append(_regenerate_button(batch, draft))
    elements.append(_cancel_button(batch))
    blocks.append(
        {
            "type": "actions",
            "block_id": f"draft_actions:{batch.id}:{draft.slot_index}",
            "elements": elements,
        }
    )
    return blocks


def build_draft_blocks(
    batch: DraftBatch, *, x_char_limit: int = MAX_TWEET_LEN
) -> list[dict[str, Any]]:
    """Build the Block Kit blocks for *batch* from its current attributes.

    Pure: reads `batch.status` and `batch.drafts` only. `x_char_limit` is the
    effective X approve/publish ceiling (callers pass `settings.x_char_limit`).
    Returns ONLY the blocks list — the caller pairs it with `fallback_text(batch)`
    for the top-level `text` field when posting/updating.
    """
    if batch.status == BatchStatus.selecting:
        # A pre-draft batch has no drafts; render the ranked-idea picker instead, so
        # no caller can ever produce a degenerate, buttonless "drafts" card.
        return build_idea_blocks(batch)

    if batch.status in (BatchStatus.cancelled, BatchStatus.expired):
        return [
            _section("~Drafts cancelled~ — nothing was published. Run /draft-post to start over.")
        ]

    if batch.status == BatchStatus.approved:
        approved = next(
            (d for d in batch.drafts if d.status == DraftStatus.approved),
            None,
        )
        # Non-empty fallback: an empty section `text` is rejected by Slack. Only
        # reachable on a contract violation (status=approved with no approved draft).
        approved_text = approved.text if approved is not None else "_(draft unavailable)_"
        return [
            _section(approved_text),
            _context("*Published (dry-run).*"),
        ]

    # Pending: the full interactive card (terminal states handled above).
    drafts = sorted(batch.drafts, key=lambda d: d.slot_index)
    n = len(drafts)
    blocks: list[dict[str, Any]] = [
        _header("Drafts ready — pick one to publish"),
        _context(
            f"From <#{batch.slack_channel_id}> • {n} drafts • publishing is *dry-run* in this phase"
        ),
    ]
    for draft in drafts:
        blocks.extend(_pending_draft_blocks(batch, draft, x_char_limit))
    return blocks


def idea_fallback_text(batch: DraftBatch) -> str:
    """Plain-text notification line for the ranked-idea (selecting) card."""
    n = len(batch.candidate_ideas)
    word = "idea" if n == 1 else "ideas"
    return f"Ghostwyre found {n} {word} worth posting — pick one to draft."


def _pick_button(batch: DraftBatch, index: int) -> dict[str, Any]:
    return {
        "type": "button",
        "action_id": PICK_ACTION_ID,
        "text": {"type": "plain_text", "text": "Draft this"},
        "style": "primary",
        "value": _encode_value(b=str(batch.id), i=str(index)),
    }


def build_idea_blocks(batch: DraftBatch) -> list[dict[str, Any]]:
    """Render the ranked-idea shortlist for a `selecting` batch (pure, no I/O).

    Each idea (from `batch.candidate_ideas`: summary/angle/score/evidence) gets a
    rank+score line, its suggested angle, the verbatim transcript quotes that
    surfaced it, and a *Draft this* button carrying ids only (batch id + idea
    index — never the idea text). One Cancel button closes the batch.
    """
    ideas = batch.candidate_ideas
    n = len(ideas)
    word = "idea" if n == 1 else "ideas"
    blocks: list[dict[str, Any]] = [
        _header("Ideas worth posting"),
        _context(
            f"I read <#{batch.slack_channel_id}> and found {n} {word} worth posting — "
            "pick one to draft."
        ),
    ]
    for i, idea in enumerate(ideas):
        blocks.append(_divider())
        summary = idea.get("summary", "")
        score = idea.get("score")
        head = f"*{i + 1}. {summary}*"
        if isinstance(score, int):
            head += f"  ·  _score {score}/100_"
        blocks.append(_section(head))
        angle = idea.get("angle", "")
        if angle:
            blocks.append(_context(f"💡 {angle}"))
        evidence = idea.get("evidence") or []
        if evidence:
            quoted = "\n".join(f"> {q}" for q in evidence)
            blocks.append(_section(f"*From the conversation:*\n{quoted}"))
        blocks.append(
            {
                "type": "actions",
                "block_id": f"idea_actions:{batch.id}:{i}",
                "elements": [_pick_button(batch, i)],
            }
        )
    blocks.append(
        {
            "type": "actions",
            "block_id": f"idea_actions:{batch.id}:cancel",
            "elements": [_cancel_button(batch)],
        }
    )
    return blocks


def history_fallback(count: int) -> str:
    """Plain-text notification line for the /post-history card."""
    if count == 0:
        return "Ghostwyre: no published posts yet."
    word = "post" if count == 1 else "posts"
    return f"Ghostwyre: your {count} most recent published {word}."


def build_history_blocks(
    published: list[ApprovalEvent],
    unconfirmed: list[DraftBatch],
) -> list[dict[str, Any]]:
    """Render the published-post history (newest first), pure/no-I/O.

    `published` are confirmed approve events (each with a publish_url + eager
    draft/batch). `unconfirmed` are "approved without link" batches — surfaced as a
    single warning so the user knows to check X. Times use Slack's <!date> token so
    each viewer sees them localized.
    """
    blocks: list[dict[str, Any]] = [_header("Published posts")]

    if unconfirmed:
        n = len(unconfirmed)
        word = "post" if n == 1 else "posts"
        blocks.append(
            _context(f"⚠️ {n} approved {word} couldn't be confirmed — check X before reposting.")
        )

    if not published:
        blocks.append(_section("No published posts yet. Approve a draft and it shows up here."))
        return blocks

    for event in published:
        blocks.append(_divider())
        text = event.draft.text if event.draft is not None else "_(text unavailable)_"
        blocks.append(_section(text))
        ts = int(event.created_at.timestamp())
        when = f"<!date^{ts}^{{date_short}} at {{time}}|{event.created_at:%Y-%m-%d}>"
        meta = f"<{event.publish_url}|View on X> • {when} • <#{event.batch.slack_channel_id}>"
        blocks.append(_context(meta))
    return blocks
