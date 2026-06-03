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
from app.db.models import ApprovalAction, BatchStatus, DraftPlatform, DraftStatus

# All DB-backed; share one session-scoped event loop so the async DB fixtures
# (whose setup/teardown must run on the same loop as the test) stay consistent
# under asyncio_mode=auto.
pytestmark = [pytest.mark.slow, pytest.mark.asyncio(loop_scope="session")]

CHANNEL = "C123"
USER = "U456"
TRANSCRIPT = "confidential transcript text"


def _specs(*texts: str, platform: DraftPlatform = DraftPlatform.x) -> list[repo.DraftSpec]:
    return [repo.DraftSpec(text=t, platform=platform) for t in texts]


async def test_create_batch_returns_populated_batch(session: AsyncSession) -> None:
    batch = await repo.create_batch(
        session,
        channel_id=CHANNEL,
        user_id=USER,
        transcript=TRANSCRIPT,
        drafts=_specs("first", "second"),
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
        drafts=_specs("a", "b", "c"),
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
        drafts=_specs("only"),
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
        drafts=_specs("x"),
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
        drafts=_specs("old0", "old1", "old2"),
    )
    old_ids = {d.id for d in created.drafts}

    new_drafts = await repo.replace_drafts(session, created.id, _specs("new0", "new1"))

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
        drafts=_specs("d"),
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
        drafts=_specs("d"),
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
        drafts=_specs("d"),
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
        drafts=_specs("d"),
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
        drafts=_specs("d"),
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
        drafts=_specs("d"),
    )
    # Commit so the status write lands in a *separate* transaction: Postgres'
    # now() is frozen per-transaction, so an in-transaction onupdate would reuse
    # the create timestamp. This guards the onupdate caveat end-to-end.
    await session.commit()
    before = created.updated_at

    await repo.set_batch_status(session, created.id, BatchStatus.cancelled)
    await session.refresh(created)

    assert created.updated_at > before


async def _publish(session: AsyncSession, text: str, url: str | None) -> None:
    """Helper: create a batch and record one approve event (committed so created_at
    advances — Postgres now() is frozen per transaction)."""
    batch = await repo.create_batch(
        session, channel_id=CHANNEL, user_id=USER, transcript=TRANSCRIPT, drafts=_specs(text)
    )
    await repo.record_event(
        session,
        batch_id=batch.id,
        draft_id=batch.drafts[0].id,
        action=ApprovalAction.approve,
        slack_user_id=USER,
        publish_url=url,
    )
    await session.commit()


async def test_list_published_newest_first_and_filters(session: AsyncSession) -> None:
    await _publish(session, "first post", "https://x.test/1")
    await _publish(session, "second post", "https://x.test/2")
    # Noise that must be excluded: an approve with no url (ambiguous) and a cancel.
    await _publish(session, "ambiguous", None)
    cx = await repo.create_batch(
        session, channel_id=CHANNEL, user_id=USER, transcript=TRANSCRIPT, drafts=_specs("c")
    )
    await repo.record_event(
        session, batch_id=cx.id, draft_id=None, action=ApprovalAction.cancel, slack_user_id=USER
    )
    await session.commit()

    published = await repo.list_published(session, limit=10)

    assert [e.publish_url for e in published] == ["https://x.test/2", "https://x.test/1"]
    assert all(e.action is ApprovalAction.approve for e in published)
    # eager-loaded relationships are usable without another query
    assert published[0].draft is not None
    assert published[0].batch.slack_channel_id == CHANNEL


async def test_list_published_respects_limit(session: AsyncSession) -> None:
    await _publish(session, "a", "https://x.test/a")
    await _publish(session, "b", "https://x.test/b")
    await _publish(session, "c", "https://x.test/c")

    assert len(await repo.list_published(session, limit=2)) == 2


async def test_list_unconfirmed_approved(session: AsyncSession) -> None:
    # Ambiguous: approved + an approve event with no url.
    amb = await repo.create_batch(
        session, channel_id=CHANNEL, user_id=USER, transcript=TRANSCRIPT, drafts=_specs("amb")
    )
    await repo.set_batch_status(session, amb.id, BatchStatus.approved)
    await repo.record_event(
        session,
        batch_id=amb.id,
        draft_id=amb.drafts[0].id,
        action=ApprovalAction.approve,
        slack_user_id=USER,
        publish_url=None,
    )
    # Crashed: approved with no events at all.
    crash = await repo.create_batch(
        session, channel_id=CHANNEL, user_id=USER, transcript=TRANSCRIPT, drafts=_specs("crash")
    )
    await repo.set_batch_status(session, crash.id, BatchStatus.approved)
    # Confirmed: approved WITH a url event -> excluded.
    ok = await repo.create_batch(
        session, channel_id=CHANNEL, user_id=USER, transcript=TRANSCRIPT, drafts=_specs("ok")
    )
    await repo.set_batch_status(session, ok.id, BatchStatus.approved)
    await repo.record_event(
        session,
        batch_id=ok.id,
        draft_id=ok.drafts[0].id,
        action=ApprovalAction.approve,
        slack_user_id=USER,
        publish_url="https://x.test/ok",
    )
    # Pending: excluded.
    pend = await repo.create_batch(
        session, channel_id=CHANNEL, user_id=USER, transcript=TRANSCRIPT, drafts=_specs("pend")
    )
    await session.commit()

    ids = {b.id for b in await repo.list_unconfirmed_approved(session)}

    assert amb.id in ids
    assert crash.id in ids
    assert ok.id not in ids
    assert pend.id not in ids


async def test_upsert_voice_profile_creates_then_updates(session: AsyncSession) -> None:
    first = await repo.upsert_voice_profile(
        session,
        slack_user_id="U1",
        platform=DraftPlatform.x,
        sample_posts=["a", "b"],
        voice_card="card v1",
        positioning="pos",
    )
    assert first.sample_posts == ["a", "b"]

    second = await repo.upsert_voice_profile(
        session,
        slack_user_id="U1",
        platform=DraftPlatform.x,
        sample_posts=["c"],
        voice_card="card v2",
        positioning="pos2",
    )
    assert second.id == first.id  # updated in place, not a new row
    assert second.voice_card == "card v2"
    assert second.sample_posts == ["c"]


async def test_get_voice_profiles_keyed_by_platform(session: AsyncSession) -> None:
    await repo.upsert_voice_profile(
        session,
        slack_user_id="U2",
        platform=DraftPlatform.x,
        sample_posts=["x post"],
        voice_card="xc",
        positioning="",
    )
    await repo.upsert_voice_profile(
        session,
        slack_user_id="U2",
        platform=DraftPlatform.linkedin,
        sample_posts=["li post"],
        voice_card="lic",
        positioning="",
    )

    profiles = await repo.get_voice_profiles(session, "U2")
    assert set(profiles) == {"x", "linkedin"}
    assert profiles["linkedin"].voice_card == "lic"


async def test_get_voice_profiles_absent_returns_empty(session: AsyncSession) -> None:
    assert await repo.get_voice_profiles(session, "nobody") == {}
