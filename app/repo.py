"""Async CRUD for the Phase 3 approval flow (pure DB — no Slack/LLM here).

Every function takes an `AsyncSession` and *flushes* but never commits — the
caller owns the transaction (handlers wrap a `session.begin()` block). Reads and
writes go through the ORM (`session.get`, `select`, attribute assignment +
`session.flush()`) rather than bulk Core statements where `onupdate=func.now()`
needs to fire, so timestamps stay correct.

The transcript persisted on a `DraftBatch` is CONFIDENTIAL: never log it and
never place it in a Slack button value.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import (
    ApprovalAction,
    ApprovalEvent,
    BatchStatus,
    Draft,
    DraftBatch,
    DraftPlatform,
    DraftStatus,
    VoiceMemory,
    VoiceProfile,
)


@dataclass(frozen=True)
class DraftSpec:
    """A draft to persist: its text and target platform. Keeps the repo decoupled
    from the LLM-layer `schemas.Draft`."""

    text: str
    platform: DraftPlatform


async def create_batch(
    session: AsyncSession,
    *,
    channel_id: str,
    user_id: str,
    transcript: str,
    drafts: list[DraftSpec],
) -> DraftBatch:
    """Persist a pending batch plus one pending draft per spec (0-based slots)."""
    batch = DraftBatch(
        slack_channel_id=channel_id,
        slack_user_id=user_id,
        transcript=transcript,
        status=BatchStatus.pending,
    )
    batch.drafts = [
        Draft(slot_index=i, text=spec.text, platform=spec.platform, status=DraftStatus.pending)
        for i, spec in enumerate(drafts)
    ]
    session.add(batch)
    await session.flush()
    await session.refresh(batch, ["drafts"])
    return batch


async def create_idea_batch(
    session: AsyncSession,
    *,
    channel_id: str,
    user_id: str,
    transcript: str,
    candidate_ideas: list[dict[str, Any]],
) -> DraftBatch:
    """Persist a `selecting` batch carrying the ranked candidate ideas (no drafts yet).

    The ideas are stored as JSON on the batch; the pick action later reads the
    chosen one by index, drafts it, and flips the batch to `pending`.
    """
    batch = DraftBatch(
        slack_channel_id=channel_id,
        slack_user_id=user_id,
        transcript=transcript,
        status=BatchStatus.selecting,
        candidate_ideas=candidate_ideas,
    )
    session.add(batch)
    await session.flush()
    await session.refresh(batch, ["drafts"])
    return batch


async def set_chosen_idea(session: AsyncSession, batch_id: uuid.UUID, index: int | None) -> None:
    """Record which ranked idea (by index) the user chose to draft; None clears it."""
    batch = await session.get(DraftBatch, batch_id)
    if batch is None:
        raise LookupError(f"DraftBatch {batch_id} not found")
    batch.chosen_idea_index = index
    await session.flush()


async def claim_idea_for_drafting(session: AsyncSession, batch_id: uuid.UUID, index: int) -> bool:
    """Atomically claim a `selecting` batch's idea for drafting (CAS).

    Sets `chosen_idea_index` iff the batch is still `selecting` AND unclaimed
    (`chosen_idea_index IS NULL`), returning True only for the call that won. A
    concurrent second "Draft this" click then loses the claim and can't fire a
    duplicate (expensive) LLM draft or clobber the first draft set — the pick-side
    sibling of `claim_for_publish`. Stamps `updated_at` by hand (Core UPDATE).
    """
    result = await session.execute(
        update(DraftBatch)
        .where(
            DraftBatch.id == batch_id,
            DraftBatch.status == BatchStatus.selecting,
            DraftBatch.chosen_idea_index.is_(None),
        )
        .values(chosen_idea_index=index, updated_at=func.now())
    )
    await session.flush()
    return cast("CursorResult[Any]", result).rowcount == 1


async def get_batch(session: AsyncSession, batch_id: uuid.UUID) -> DraftBatch | None:
    """Load a batch with its drafts (slot-ordered) and events, or None if missing."""
    result = await session.execute(
        select(DraftBatch)
        .where(DraftBatch.id == batch_id)
        .options(selectinload(DraftBatch.drafts), selectinload(DraftBatch.events))
    )
    return result.scalar_one_or_none()


async def get_draft(session: AsyncSession, draft_id: uuid.UUID) -> Draft | None:
    """Load a draft with its parent batch eager-loaded, or None if missing."""
    result = await session.execute(
        select(Draft).where(Draft.id == draft_id).options(selectinload(Draft.batch))
    )
    return result.scalar_one_or_none()


async def list_published(session: AsyncSession, *, limit: int) -> list[ApprovalEvent]:
    """Published-post history: approve events that carry a publish URL, newest first.

    Each event eager-loads its batch (for channel) and draft (for the posted text).
    Read-only.
    """
    result = await session.execute(
        select(ApprovalEvent)
        .where(
            ApprovalEvent.action == ApprovalAction.approve,
            ApprovalEvent.publish_url.isnot(None),
        )
        .order_by(ApprovalEvent.created_at.desc())
        .limit(limit)
        .options(selectinload(ApprovalEvent.batch), selectinload(ApprovalEvent.draft))
    )
    return list(result.scalars().all())


async def list_unconfirmed_approved(session: AsyncSession) -> list[DraftBatch]:
    """Batches stuck `approved` with no confirming publish event ("approved without
    link"): an ambiguous publish (PublishUnknownError) or a crash mid-approve. These
    need a human to check X. Read-only; oldest first so the longest-waiting surface up.
    """
    confirmed = select(ApprovalEvent.id).where(
        ApprovalEvent.batch_id == DraftBatch.id,
        ApprovalEvent.action == ApprovalAction.approve,
        ApprovalEvent.publish_url.isnot(None),
    )
    result = await session.execute(
        select(DraftBatch)
        .where(DraftBatch.status == BatchStatus.approved, ~confirmed.exists())
        .order_by(DraftBatch.created_at)
        .options(selectinload(DraftBatch.drafts))
    )
    return list(result.scalars().all())


async def set_batch_message_ts(
    session: AsyncSession,
    batch_id: uuid.UUID,
    channel_id: str,
    message_ts: str,
) -> None:
    """Record the in-channel card's (channel, ts) so handlers can chat_update it."""
    batch = await session.get(DraftBatch, batch_id)
    if batch is None:
        raise LookupError(f"DraftBatch {batch_id} not found")
    batch.slack_channel_id = channel_id
    batch.slack_message_ts = message_ts
    await session.flush()


async def replace_drafts(
    session: AsyncSession,
    batch_id: uuid.UUID,
    drafts: list[DraftSpec],
) -> list[Draft]:
    """Replace the batch's whole draft set with fresh pending rows (Regenerate)."""
    # synchronize_session="fetch" evicts the deleted rows from the session's
    # identity map too, so a later get_batch/selectinload won't resurface stale
    # Draft objects (or a stale parent.drafts collection).
    await session.execute(
        delete(Draft)
        .where(Draft.batch_id == batch_id)
        .execution_options(synchronize_session="fetch")
    )
    new_rows = [
        Draft(
            batch_id=batch_id,
            slot_index=i,
            text=spec.text,
            platform=spec.platform,
            status=DraftStatus.pending,
        )
        for i, spec in enumerate(drafts)
    ]
    session.add_all(new_rows)
    await session.flush()
    # If the parent batch is already loaded, its `drafts` collection still holds
    # the old set; expire it so the next access (e.g. get_batch's selectinload)
    # reloads the fresh rows rather than the stale cached collection. Only touch
    # the identity map (no extra SELECT) — if the batch was never loaded in this
    # session there is nothing cached to go stale.
    cached = session.identity_map.get((DraftBatch, (batch_id,), None))
    if cached is not None:
        session.expire(cached, ["drafts"])
    result = await session.execute(
        select(Draft).where(Draft.batch_id == batch_id).order_by(Draft.slot_index)
    )
    return list(result.scalars().all())


async def set_draft_status(
    session: AsyncSession,
    draft_id: uuid.UUID,
    status: DraftStatus,
) -> None:
    """Set a draft's status; raise LookupError if it does not exist."""
    draft = await session.get(Draft, draft_id)
    if draft is None:
        raise LookupError(f"Draft {draft_id} not found")
    draft.status = status
    await session.flush()


async def set_draft_text(
    session: AsyncSession,
    draft_id: uuid.UUID,
    text: str,
) -> None:
    """Replace a single draft's text in place (a user's edit); raise LookupError if missing."""
    draft = await session.get(Draft, draft_id)
    if draft is None:
        raise LookupError(f"Draft {draft_id} not found")
    draft.text = text
    await session.flush()


async def set_batch_status(
    session: AsyncSession,
    batch_id: uuid.UUID,
    status: BatchStatus,
) -> None:
    """Set a batch's status; raise LookupError if it does not exist."""
    batch = await session.get(DraftBatch, batch_id)
    if batch is None:
        raise LookupError(f"DraftBatch {batch_id} not found")
    batch.status = status
    await session.flush()


async def claim_for_publish(session: AsyncSession, batch_id: uuid.UUID) -> bool:
    """Atomically claim a pending batch for publishing (CAS pending -> approved).

    Returns True iff this call won the claim (exactly one row transitioned).
    The Approve handler calls this and commits BEFORE the network publish, so a
    concurrent double-click or Slack retry that arrives afterwards loses the CAS
    and can never publish a second time — the at-most-once guarantee.

    This is the one writer that uses a Core UPDATE instead of ORM attribute
    assignment (the WHERE clause has to match atomically), so it must stamp
    updated_at by hand — onupdate=func.now() only fires for ORM unit-of-work
    flushes, not Core statements.
    """
    result = await session.execute(
        update(DraftBatch)
        .where(DraftBatch.id == batch_id, DraftBatch.status == BatchStatus.pending)
        .values(status=BatchStatus.approved, updated_at=func.now())
    )
    await session.flush()
    # session.execute() is typed Result[Any]; a Core UPDATE yields a CursorResult
    # whose rowcount tells us whether the CAS matched a pending row.
    return cast("CursorResult[Any]", result).rowcount == 1


async def record_event(
    session: AsyncSession,
    *,
    batch_id: uuid.UUID,
    draft_id: uuid.UUID | None,
    action: ApprovalAction,
    slack_user_id: str,
    publish_url: str | None = None,
) -> ApprovalEvent:
    """Append an audit row for an approve/regenerate/cancel action."""
    event = ApprovalEvent(
        batch_id=batch_id,
        draft_id=draft_id,
        action=action,
        slack_user_id=slack_user_id,
        publish_url=publish_url,
    )
    session.add(event)
    await session.flush()
    return event


async def upsert_voice_profile(
    session: AsyncSession,
    *,
    slack_user_id: str,
    platform: DraftPlatform,
    sample_posts: list[str],
    voice_card: str,
    positioning: str,
) -> VoiceProfile:
    """Create or update the user's voice profile for *platform* (one per pair)."""
    result = await session.execute(
        select(VoiceProfile).where(
            VoiceProfile.slack_user_id == slack_user_id,
            VoiceProfile.platform == platform,
        )
    )
    profile = result.scalar_one_or_none()
    if profile is None:
        profile = VoiceProfile(
            slack_user_id=slack_user_id,
            platform=platform,
            sample_posts=sample_posts,
            voice_card=voice_card,
            positioning=positioning,
        )
        session.add(profile)
    else:
        profile.sample_posts = sample_posts
        profile.voice_card = voice_card
        profile.positioning = positioning
    await session.flush()
    return profile


async def get_voice_profiles(session: AsyncSession, slack_user_id: str) -> dict[str, VoiceProfile]:
    """Return the user's voice profiles keyed by platform value ("x"/"linkedin")."""
    result = await session.execute(
        select(VoiceProfile).where(VoiceProfile.slack_user_id == slack_user_id)
    )
    return {str(p.platform): p for p in result.scalars().all()}


async def add_voice_memory(
    session: AsyncSession,
    *,
    slack_user_id: str,
    platform: DraftPlatform,
    instructions: list[str],
    source: str = "edit",
) -> list[VoiceMemory]:
    """Append distilled style rules for (user, platform). Flush only."""
    rows = [
        VoiceMemory(slack_user_id=slack_user_id, platform=platform, instruction=text, source=source)
        for text in instructions
    ]
    session.add_all(rows)
    await session.flush()
    return rows


async def get_voice_memory(
    session: AsyncSession, slack_user_id: str, *, limit_per_platform: int
) -> dict[str, list[str]]:
    """The user's most recent learned rules per platform (newest first, capped at
    *limit_per_platform*) as plain strings, keyed by platform value — for drafting."""
    result = await session.execute(
        select(VoiceMemory)
        .where(VoiceMemory.slack_user_id == slack_user_id)
        .order_by(VoiceMemory.created_at.desc())
    )
    out: dict[str, list[str]] = {}
    for row in result.scalars().all():
        bucket = out.setdefault(str(row.platform), [])
        if len(bucket) < limit_per_platform:
            bucket.append(row.instruction)
    return out


async def list_voice_memory(
    session: AsyncSession, slack_user_id: str, *, limit: int = 40
) -> list[VoiceMemory]:
    """The user's learned rules (rows with ids), newest first, capped at *limit* — for
    the /voice view. The cap keeps the rendered card under Slack's 50-block ceiling;
    build_voice_blocks regroups by platform."""
    result = await session.execute(
        select(VoiceMemory)
        .where(VoiceMemory.slack_user_id == slack_user_id)
        .order_by(VoiceMemory.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def delete_voice_memory(
    session: AsyncSession, memory_id: uuid.UUID, *, slack_user_id: str
) -> bool:
    """Delete one of *slack_user_id*'s learned rules by id; True iff a row was removed.

    The owner filter is mandatory: a forged/replayed Forget click carrying another
    user's rule id must not delete it.
    """
    result = await session.execute(
        delete(VoiceMemory)
        .where(VoiceMemory.id == memory_id, VoiceMemory.slack_user_id == slack_user_id)
        .execution_options(synchronize_session="fetch")
    )
    await session.flush()
    return cast("CursorResult[Any]", result).rowcount == 1
