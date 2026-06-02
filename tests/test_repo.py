"""DB-backed tests for app/repo.py (all @pytest.mark.slow).

Exercises the async CRUD against the dedicated `ghostwyre_test` database via the
`session` fixture in conftest. repo functions flush but do not commit; reads go
through the same session, and the fixture rolls back after each test.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app import repo
from app.db.models import ApprovalAction, BatchStatus, DraftStatus

# All DB-backed; share one session-scoped event loop so the async DB fixtures
# (whose setup/teardown must run on the same loop as the test) stay consistent
# under asyncio_mode=auto.
pytestmark = [pytest.mark.slow, pytest.mark.asyncio(loop_scope="session")]

CHANNEL = "C123"
USER = "U456"
TRANSCRIPT = "confidential transcript text"


async def test_create_batch_returns_populated_batch(session: AsyncSession) -> None:
    batch = await repo.create_batch(
        session,
        channel_id=CHANNEL,
        user_id=USER,
        transcript=TRANSCRIPT,
        draft_texts=["first", "second"],
    )

    assert isinstance(batch.id, uuid.UUID)
    assert batch.status is BatchStatus.pending
    assert batch.transcript == TRANSCRIPT
    assert batch.slack_channel_id == CHANNEL
    assert batch.slack_user_id == USER
    assert len(batch.drafts) == 2
    assert [d.slot_index for d in batch.drafts] == [0, 1]
    assert [d.text for d in batch.drafts] == ["first", "second"]
    assert all(d.status is DraftStatus.pending for d in batch.drafts)


async def test_get_batch_returns_none_for_unknown_id(session: AsyncSession) -> None:
    assert await repo.get_batch(session, uuid.uuid4()) is None


async def test_get_batch_loads_drafts_in_slot_order(session: AsyncSession) -> None:
    created = await repo.create_batch(
        session,
        channel_id=CHANNEL,
        user_id=USER,
        transcript=TRANSCRIPT,
        draft_texts=["a", "b", "c"],
    )

    loaded = await repo.get_batch(session, created.id)

    assert loaded is not None
    assert [d.slot_index for d in loaded.drafts] == [0, 1, 2]
    assert [d.text for d in loaded.drafts] == ["a", "b", "c"]


async def test_get_draft_loads_batch(session: AsyncSession) -> None:
    created = await repo.create_batch(
        session,
        channel_id=CHANNEL,
        user_id=USER,
        transcript=TRANSCRIPT,
        draft_texts=["only"],
    )
    draft_id = created.drafts[0].id

    draft = await repo.get_draft(session, draft_id)

    assert draft is not None
    assert draft.id == draft_id
    assert draft.batch.id == created.id


async def test_get_draft_returns_none_for_unknown_id(session: AsyncSession) -> None:
    assert await repo.get_draft(session, uuid.uuid4()) is None


async def test_set_batch_message_ts_persists(session: AsyncSession) -> None:
    created = await repo.create_batch(
        session,
        channel_id=CHANNEL,
        user_id=USER,
        transcript=TRANSCRIPT,
        draft_texts=["x"],
    )

    await repo.set_batch_message_ts(session, created.id, "C999", "1700000000.000100")

    loaded = await repo.get_batch(session, created.id)
    assert loaded is not None
    assert loaded.slack_channel_id == "C999"
    assert loaded.slack_message_ts == "1700000000.000100"


async def test_replace_drafts_swaps_whole_set(session: AsyncSession) -> None:
    created = await repo.create_batch(
        session,
        channel_id=CHANNEL,
        user_id=USER,
        transcript=TRANSCRIPT,
        draft_texts=["old0", "old1", "old2"],
    )
    old_ids = {d.id for d in created.drafts}

    new_drafts = await repo.replace_drafts(session, created.id, ["new0", "new1"])

    assert [d.slot_index for d in new_drafts] == [0, 1]
    assert [d.text for d in new_drafts] == ["new0", "new1"]
    assert all(d.status is DraftStatus.pending for d in new_drafts)
    assert old_ids.isdisjoint({d.id for d in new_drafts})

    loaded = await repo.get_batch(session, created.id)
    assert loaded is not None
    assert len(loaded.drafts) == 2
    assert [d.slot_index for d in loaded.drafts] == [0, 1]
    assert [d.text for d in loaded.drafts] == ["new0", "new1"]


async def test_set_draft_status_reflects_via_get(session: AsyncSession) -> None:
    created = await repo.create_batch(
        session,
        channel_id=CHANNEL,
        user_id=USER,
        transcript=TRANSCRIPT,
        draft_texts=["d"],
    )
    draft_id = created.drafts[0].id

    await repo.set_draft_status(session, draft_id, DraftStatus.approved)

    draft = await repo.get_draft(session, draft_id)
    assert draft is not None
    assert draft.status is DraftStatus.approved


async def test_set_draft_status_missing_raises(session: AsyncSession) -> None:
    with pytest.raises(LookupError):
        await repo.set_draft_status(session, uuid.uuid4(), DraftStatus.approved)


async def test_set_batch_status_reflects_via_get(session: AsyncSession) -> None:
    created = await repo.create_batch(
        session,
        channel_id=CHANNEL,
        user_id=USER,
        transcript=TRANSCRIPT,
        draft_texts=["d"],
    )

    await repo.set_batch_status(session, created.id, BatchStatus.cancelled)

    loaded = await repo.get_batch(session, created.id)
    assert loaded is not None
    assert loaded.status is BatchStatus.cancelled


async def test_set_batch_status_missing_raises(session: AsyncSession) -> None:
    with pytest.raises(LookupError):
        await repo.set_batch_status(session, uuid.uuid4(), BatchStatus.cancelled)


async def test_record_event_approve_with_url(session: AsyncSession) -> None:
    created = await repo.create_batch(
        session,
        channel_id=CHANNEL,
        user_id=USER,
        transcript=TRANSCRIPT,
        draft_texts=["d"],
    )
    draft_id = created.drafts[0].id

    event = await repo.record_event(
        session,
        batch_id=created.id,
        draft_id=draft_id,
        action=ApprovalAction.approve,
        slack_user_id=USER,
        publish_url="https://x.test/1",
    )

    assert isinstance(event.id, uuid.UUID)

    loaded = await repo.get_batch(session, created.id)
    assert loaded is not None
    assert len(loaded.events) == 1
    assert loaded.events[0].action is ApprovalAction.approve
    assert loaded.events[0].publish_url == "https://x.test/1"
    assert loaded.events[0].draft_id == draft_id


async def test_record_event_cancel_without_draft(session: AsyncSession) -> None:
    created = await repo.create_batch(
        session,
        channel_id=CHANNEL,
        user_id=USER,
        transcript=TRANSCRIPT,
        draft_texts=["d"],
    )

    event = await repo.record_event(
        session,
        batch_id=created.id,
        draft_id=None,
        action=ApprovalAction.cancel,
        slack_user_id=USER,
    )

    assert event.draft_id is None
    assert event.action is ApprovalAction.cancel
    assert event.publish_url is None


async def test_claim_for_publish_wins_once_then_loses(session: AsyncSession) -> None:
    created = await repo.create_batch(
        session,
        channel_id=CHANNEL,
        user_id=USER,
        transcript=TRANSCRIPT,
        draft_texts=["d"],
    )

    first = await repo.claim_for_publish(session, created.id)
    second = await repo.claim_for_publish(session, created.id)

    assert first is True  # CAS pending -> approved matched exactly one row
    assert second is False  # already approved -> the WHERE no longer matches
    loaded = await repo.get_batch(session, created.id)
    assert loaded is not None
    assert loaded.status is BatchStatus.approved


async def test_claim_for_publish_unknown_id_returns_false(session: AsyncSession) -> None:
    assert await repo.claim_for_publish(session, uuid.uuid4()) is False


async def test_updated_at_advances_on_status_write(session: AsyncSession) -> None:
    created = await repo.create_batch(
        session,
        channel_id=CHANNEL,
        user_id=USER,
        transcript=TRANSCRIPT,
        draft_texts=["d"],
    )
    # Commit so the status write lands in a *separate* transaction: Postgres'
    # now() is frozen per-transaction, so an in-transaction onupdate would reuse
    # the create timestamp. This guards the onupdate caveat end-to-end.
    await session.commit()
    before = created.updated_at

    await repo.set_batch_status(session, created.id, BatchStatus.cancelled)
    await session.refresh(created)

    assert created.updated_at > before
