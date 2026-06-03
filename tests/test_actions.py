"""Tests for the Slack approval-action handlers (app/slack/actions.py).

The pure helper test (`test_parse_action_value`) is fast; everything that drives
a handler is DB-backed and therefore `@pytest.mark.slow`. The handlers are
captured via a tiny fake `AsyncApp` whose `.action` decorator just stores the
callback by action_id, then invoked directly with hand-rolled fakes (no
unittest.mock — matching the house style).

We monkeypatch `app.slack.actions.SessionLocal` to the conftest
`test_sessionmaker` so handler DB writes hit the dedicated test database, and
monkeypatch `app.slack.actions.generate_post_drafts` (regeneration) and the
module's `publisher` (approve) so no network/LLM call happens.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import repo
from app.db.models import ApprovalAction, BatchStatus, DraftPlatform, DraftStatus
from app.services.content import ContentResult
from app.services.publisher import PublishError, PublishResult, PublishUnknownError
from app.services.schemas import Draft
from app.slack import actions
from app.slack.actions import parse_action_value, parse_pick_value, register

CHANNEL = "C123"
USER = "U456"
TS = "1700000000.000100"
TRANSCRIPT = "confidential transcript text"


# --------------------------------------------------------------------------- #
# Fakes (hand-rolled — no unittest.mock).
# --------------------------------------------------------------------------- #


class FakeApp:
    """Captures @app.action callbacks by action_id."""

    def __init__(self) -> None:
        self.callbacks: dict[str, Any] = {}

    def action(self, action_id: str) -> Any:
        def decorator(fn: Any) -> Any:
            self.callbacks[action_id] = fn
            return fn

        return decorator


class FakeClient:
    """Captures chat_update calls; returns {} like the real web client."""

    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    async def chat_update(self, **kwargs: Any) -> dict[str, Any]:
        self.updates.append(kwargs)
        return {}


class FakePublisher:
    """Records publish() calls; returns a dry-run result or raises PublishError."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error
        self.calls: list[str] = []

    async def publish(self, text: str) -> PublishResult:
        self.calls.append(text)
        if self._error is not None:
            raise self._error
        return PublishResult(url="https://x.test/published/1", dry_run=True)


class FailingClient:
    """chat_update always raises — simulates a Slack failure *after* a publish."""

    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    async def chat_update(self, **kwargs: Any) -> dict[str, Any]:
        self.updates.append(kwargs)
        raise RuntimeError("slack down")


class FailOnNthClient:
    """chat_update succeeds until the nth call, which raises — simulates a Slack
    failure on the FINAL render after the DB writes already committed."""

    def __init__(self, *, n: int) -> None:
        self.updates: list[dict[str, Any]] = []
        self._n = n

    async def chat_update(self, **kwargs: Any) -> dict[str, Any]:
        self.updates.append(kwargs)
        if len(self.updates) >= self._n:
            raise RuntimeError("slack down")
        return {}


async def _ack() -> None:
    return None


def _body(user_id: str = USER) -> dict[str, Any]:
    return {"user": {"id": user_id}}


def _action(value: str, block_id: str | None = None) -> dict[str, Any]:
    a: dict[str, Any] = {"value": value}
    if block_id is not None:
        a["block_id"] = block_id
    return a


# --------------------------------------------------------------------------- #
# Fast (pure) test.
# --------------------------------------------------------------------------- #


def test_parse_action_value() -> None:
    b = str(uuid.uuid4())
    d = str(uuid.uuid4())
    assert parse_action_value(json.dumps({"b": b, "d": d})) == (b, d)
    # cancel form: no draft id.
    assert parse_action_value(json.dumps({"b": b})) == (b, None)


def test_parse_action_value_malformed_raises() -> None:
    with pytest.raises((json.JSONDecodeError, KeyError)):
        parse_action_value("not json")
    with pytest.raises(KeyError):
        parse_action_value(json.dumps({"d": "x"}))


def test_parse_pick_value() -> None:
    b = str(uuid.uuid4())
    assert parse_pick_value(json.dumps({"b": b, "i": 2})) == (b, 2)
    assert parse_pick_value(json.dumps({"b": b, "i": "0"})) == (b, 0)  # str index coerced
    # Malformed / missing -> (None, None), never raises.
    assert parse_pick_value("not json") == (None, None)
    assert parse_pick_value(json.dumps({"b": b})) == (None, None)


# --------------------------------------------------------------------------- #
# DB-backed handler tests.
# --------------------------------------------------------------------------- #
#
# These are slow (Postgres) and share one session-scoped loop so the async DB
# fixtures stay on the running test's loop under asyncio_mode=auto. Applied
# per-function (not via module pytestmark) so the pure tests above stay in the
# fast, Postgres-free subset.

_db_test = pytest.mark.slow
_db_loop = pytest.mark.asyncio(loop_scope="session")


async def _seed_pending_batch(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    draft_texts: list[str],
    platform: DraftPlatform = DraftPlatform.x,
) -> Any:
    async with sessionmaker() as session:
        async with session.begin():
            batch = await repo.create_batch(
                session,
                channel_id=CHANNEL,
                user_id=USER,
                transcript=TRANSCRIPT,
                drafts=[repo.DraftSpec(text=t, platform=platform) for t in draft_texts],
            )
            await repo.set_batch_message_ts(session, batch.id, CHANNEL, TS)
            batch_id = batch.id
            draft_ids = [d.id for d in batch.drafts]
    return batch_id, draft_ids


_IDEAS: list[dict[str, Any]] = [
    {
        "summary": "Shipped dark mode",
        "angle": "Ship ugly.",
        "score": 90,
        "evidence": ["a: shipped"],
    },
    {"summary": "Killed a feature", "angle": "Saying no.", "score": 70, "evidence": ["b: killed"]},
]


async def _seed_selecting_batch(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    ideas: list[dict[str, Any]] | None = None,
) -> Any:
    """A `selecting` batch with ranked ideas and no drafts yet (the pre-pick state)."""
    async with sessionmaker() as session:
        async with session.begin():
            batch = await repo.create_idea_batch(
                session,
                channel_id=CHANNEL,
                user_id=USER,
                transcript=TRANSCRIPT,
                candidate_ideas=ideas if ideas is not None else _IDEAS,
            )
            await repo.set_batch_message_ts(session, batch.id, CHANNEL, TS)
            batch_id = batch.id
    return batch_id


async def _seed_drafted_batch(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    draft_texts: list[str],
    platform: DraftPlatform = DraftPlatform.x,
) -> Any:
    """A `pending` batch already drafted from idea 0 (post-pick state) — what
    Regenerate operates on. Returns (batch_id, draft_ids)."""
    async with sessionmaker() as session:
        async with session.begin():
            batch = await repo.create_idea_batch(
                session,
                channel_id=CHANNEL,
                user_id=USER,
                transcript=TRANSCRIPT,
                candidate_ideas=_IDEAS,
            )
            new = await repo.replace_drafts(
                session,
                batch.id,
                [repo.DraftSpec(text=t, platform=platform) for t in draft_texts],
            )
            await repo.set_chosen_idea(session, batch.id, 0)
            await repo.set_batch_status(session, batch.id, BatchStatus.pending)
            await repo.set_batch_message_ts(session, batch.id, CHANNEL, TS)
            batch_id = batch.id
            draft_ids = [d.id for d in new]
    return batch_id, draft_ids


def _has_actions_block(blocks: list[dict[str, Any]]) -> bool:
    return any(b.get("type") == "actions" for b in blocks)


async def _invoke(
    callbacks: dict[str, Any],
    action_id: str,
    *,
    value: str,
    client: FakeClient,
    block_id: str | None = None,
    user_id: str = USER,
) -> None:
    await callbacks[action_id](
        ack=_ack,
        body=_body(user_id),
        action=_action(value, block_id),
        client=client,
    )


@_db_test
@_db_loop
async def test_approve_publishes_and_records_event(
    monkeypatch: pytest.MonkeyPatch,
    test_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    pub = FakePublisher()
    monkeypatch.setattr(actions, "SessionLocal", test_sessionmaker)
    fake_app = FakeApp()
    register(fake_app)  # type: ignore[arg-type]
    # register() built a DryRunPublisher; swap in our recording fake.
    actions._set_publisher(pub)
    callbacks = fake_app.callbacks

    batch_id, draft_ids = await _seed_pending_batch(test_sessionmaker, draft_texts=["hello world"])
    value = json.dumps({"b": str(batch_id), "d": str(draft_ids[0])})
    client = FakeClient()

    await _invoke(callbacks, "approve_draft", value=value, client=client)

    assert pub.calls == ["hello world"]
    async with test_sessionmaker() as session:
        loaded = await repo.get_batch(session, batch_id)
        assert loaded is not None
        assert loaded.status is BatchStatus.approved
        assert loaded.drafts[0].status is DraftStatus.approved
        assert len(loaded.events) == 1
        assert loaded.events[0].action is ApprovalAction.approve
        assert loaded.events[0].publish_url == "https://x.test/published/1"

    assert client.updates
    final_blocks = client.updates[-1]["blocks"]
    assert not _has_actions_block(final_blocks)


@_db_test
@_db_loop
async def test_approve_publish_error_keeps_pending(
    monkeypatch: pytest.MonkeyPatch,
    test_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    pub = FakePublisher(error=PublishError("Post is 281 chars; the limit is 280."))
    monkeypatch.setattr(actions, "SessionLocal", test_sessionmaker)
    fake_app = FakeApp()
    register(fake_app)  # type: ignore[arg-type]
    actions._set_publisher(pub)
    callbacks = fake_app.callbacks

    batch_id, draft_ids = await _seed_pending_batch(test_sessionmaker, draft_texts=["x" * 281])
    value = json.dumps({"b": str(batch_id), "d": str(draft_ids[0])})
    client = FakeClient()

    await _invoke(callbacks, "approve_draft", value=value, client=client)

    assert pub.calls == ["x" * 281]
    async with test_sessionmaker() as session:
        loaded = await repo.get_batch(session, batch_id)
        assert loaded is not None
        assert loaded.status is BatchStatus.pending
        assert loaded.drafts[0].status is DraftStatus.pending
        # No approve event with a url was recorded.
        assert all(e.publish_url is None for e in loaded.events)

    rendered = json.dumps(client.updates[-1]["blocks"])
    assert "publish" in rendered.lower()


@_db_test
@_db_loop
async def test_approve_idempotent_does_not_republish(
    monkeypatch: pytest.MonkeyPatch,
    test_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    pub = FakePublisher()
    monkeypatch.setattr(actions, "SessionLocal", test_sessionmaker)
    fake_app = FakeApp()
    register(fake_app)  # type: ignore[arg-type]
    actions._set_publisher(pub)
    callbacks = fake_app.callbacks

    batch_id, draft_ids = await _seed_pending_batch(test_sessionmaker, draft_texts=["hello"])
    value = json.dumps({"b": str(batch_id), "d": str(draft_ids[0])})

    await _invoke(callbacks, "approve_draft", value=value, client=FakeClient())
    assert pub.calls == ["hello"]

    # Second click on an already-approved batch must NOT publish again.
    await _invoke(callbacks, "approve_draft", value=value, client=FakeClient())
    assert pub.calls == ["hello"]

    async with test_sessionmaker() as session:
        loaded = await repo.get_batch(session, batch_id)
        assert loaded is not None
        assert len(loaded.events) == 1


@_db_test
@_db_loop
async def test_approve_ambiguous_failure_leaves_approved(
    monkeypatch: pytest.MonkeyPatch,
    test_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    pub = FakePublisher(error=PublishUnknownError("X didn't confirm the post — check X."))
    monkeypatch.setattr(actions, "SessionLocal", test_sessionmaker)
    fake_app = FakeApp()
    register(fake_app)  # type: ignore[arg-type]
    actions._set_publisher(pub)
    callbacks = fake_app.callbacks

    batch_id, draft_ids = await _seed_pending_batch(test_sessionmaker, draft_texts=["hello world"])
    value = json.dumps({"b": str(batch_id), "d": str(draft_ids[0])})
    client = FakeClient()

    await _invoke(callbacks, "approve_draft", value=value, client=client)

    assert pub.calls == ["hello world"]
    async with test_sessionmaker() as session:
        loaded = await repo.get_batch(session, batch_id)
        assert loaded is not None
        # At-most-once: stay approved (do NOT re-arm), audit row with no url.
        assert loaded.status is BatchStatus.approved
        assert loaded.drafts[0].status is DraftStatus.approved
        assert len(loaded.events) == 1
        assert loaded.events[0].publish_url is None

    final_blocks = client.updates[-1]["blocks"]
    assert not _has_actions_block(final_blocks)
    assert "unknown" in json.dumps(final_blocks).lower()


@_db_test
@_db_loop
async def test_approve_chat_update_failure_does_not_republish(
    monkeypatch: pytest.MonkeyPatch,
    test_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    pub = FakePublisher()
    monkeypatch.setattr(actions, "SessionLocal", test_sessionmaker)
    fake_app = FakeApp()
    register(fake_app)  # type: ignore[arg-type]
    actions._set_publisher(pub)
    callbacks = fake_app.callbacks

    batch_id, draft_ids = await _seed_pending_batch(test_sessionmaker, draft_texts=["hello"])
    value = json.dumps({"b": str(batch_id), "d": str(draft_ids[0])})

    # The publish succeeds and is committed (status -> approved, event recorded);
    # the terminal chat_update then fails and the error propagates.
    with pytest.raises(RuntimeError):
        await _invoke(callbacks, "approve_draft", value=value, client=FailingClient())
    assert pub.calls == ["hello"]

    # Because the claim was committed BEFORE the network call, a re-click sees an
    # already-approved batch and is idempotent — it does NOT republish. This is
    # the double-publish race fix.
    await _invoke(callbacks, "approve_draft", value=value, client=FakeClient())
    assert pub.calls == ["hello"]

    async with test_sessionmaker() as session:
        loaded = await repo.get_batch(session, batch_id)
        assert loaded is not None
        assert loaded.status is BatchStatus.approved
        assert len(loaded.events) == 1


@_db_test
@_db_loop
async def test_approve_linkedin_draft_does_not_publish(
    monkeypatch: pytest.MonkeyPatch,
    test_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    pub = FakePublisher()
    monkeypatch.setattr(actions, "SessionLocal", test_sessionmaker)
    fake_app = FakeApp()
    register(fake_app)  # type: ignore[arg-type]
    actions._set_publisher(pub)
    callbacks = fake_app.callbacks

    batch_id, draft_ids = await _seed_pending_batch(
        test_sessionmaker,
        draft_texts=["a developed linkedin post"],
        platform=DraftPlatform.linkedin,
    )
    value = json.dumps({"b": str(batch_id), "d": str(draft_ids[0])})

    # LinkedIn is copy-only — the approve guard must refuse before claiming.
    await _invoke(callbacks, "approve_draft", value=value, client=FakeClient())

    assert pub.calls == []  # never published
    async with test_sessionmaker() as session:
        loaded = await repo.get_batch(session, batch_id)
        assert loaded is not None
        assert loaded.status is BatchStatus.pending  # not claimed
        assert loaded.events == []


@_db_test
@_db_loop
async def test_regenerate_replaces_drafts_and_records_event(
    monkeypatch: pytest.MonkeyPatch,
    test_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    monkeypatch.setattr(actions, "SessionLocal", test_sessionmaker)

    new_drafts = [Draft(text="fresh-zero"), Draft(text="fresh-one")]

    async def fake_generate(*args: Any, **kwargs: Any) -> ContentResult:
        return ContentResult(postworthy=True, drafts=new_drafts)

    monkeypatch.setattr(actions, "generate_idea_drafts", fake_generate)
    fake_app = FakeApp()
    register(fake_app)  # type: ignore[arg-type]
    callbacks = fake_app.callbacks

    batch_id, draft_ids = await _seed_drafted_batch(
        test_sessionmaker, draft_texts=["old-zero", "old-one"]
    )
    value = json.dumps({"b": str(batch_id), "d": str(draft_ids[0])})
    client = FakeClient()

    await _invoke(callbacks, "regenerate_draft", value=value, client=client)

    async with test_sessionmaker() as session:
        loaded = await repo.get_batch(session, batch_id)
        assert loaded is not None
        assert loaded.status is BatchStatus.pending
        assert [d.text for d in loaded.drafts] == ["fresh-zero", "fresh-one"]
        regen_events = [e for e in loaded.events if e.action is ApprovalAction.regenerate]
        assert len(regen_events) == 1
        assert regen_events[0].draft_id is None

    # interim ("Regenerating…") + final render.
    assert len(client.updates) >= 2
    final = json.dumps(client.updates[-1]["blocks"])
    assert "fresh-zero" in final


@_db_test
@_db_loop
async def test_regenerate_uses_invokers_voice_profile(
    monkeypatch: pytest.MonkeyPatch,
    test_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # Regeneration must use the ORIGINAL invoker's (batch.slack_user_id) stored
    # voice — its card + sample posts reach generation as that platform's context.
    monkeypatch.setattr(actions, "SessionLocal", test_sessionmaker)

    captured: dict[str, Any] = {}

    async def fake_generate(*args: Any, **kwargs: Any) -> ContentResult:
        # generate_idea_drafts(idea, transcript, voices, ...) — voices is the 3rd arg.
        captured["voices"] = args[2] if len(args) > 2 else kwargs.get("voices")
        return ContentResult(postworthy=True, drafts=[Draft(text="fresh")])

    monkeypatch.setattr(actions, "generate_idea_drafts", fake_generate)
    fake_app = FakeApp()
    register(fake_app)  # type: ignore[arg-type]
    callbacks = fake_app.callbacks

    async with test_sessionmaker() as session:
        async with session.begin():
            await repo.upsert_voice_profile(
                session,
                slack_user_id=USER,
                platform=DraftPlatform.x,
                sample_posts=["mine1", "mine2"],
                voice_card="USER X VOICE",
                positioning="testing",
            )

    batch_id, draft_ids = await _seed_drafted_batch(test_sessionmaker, draft_texts=["old"])
    value = json.dumps({"b": str(batch_id), "d": str(draft_ids[0])})

    await _invoke(callbacks, "regenerate_draft", value=value, client=FakeClient())

    voices = captured["voices"]
    assert voices is not None
    assert voices["x"].voice_card == "USER X VOICE"
    assert voices["x"].sample_posts == ["mine1", "mine2"]
    assert voices["x"].positioning == "testing"


@_db_test
@_db_loop
async def test_regenerate_failure_keeps_old_drafts(
    monkeypatch: pytest.MonkeyPatch,
    test_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    monkeypatch.setattr(actions, "SessionLocal", test_sessionmaker)

    async def fake_generate(*args: Any, **kwargs: Any) -> ContentResult:
        raise RuntimeError("LLM exploded")

    monkeypatch.setattr(actions, "generate_idea_drafts", fake_generate)
    fake_app = FakeApp()
    register(fake_app)  # type: ignore[arg-type]
    callbacks = fake_app.callbacks

    batch_id, draft_ids = await _seed_drafted_batch(
        test_sessionmaker, draft_texts=["keep-zero", "keep-one"]
    )
    value = json.dumps({"b": str(batch_id), "d": str(draft_ids[0])})
    client = FakeClient()

    await _invoke(callbacks, "regenerate_draft", value=value, client=client)

    async with test_sessionmaker() as session:
        loaded = await repo.get_batch(session, batch_id)
        assert loaded is not None
        assert loaded.status is BatchStatus.pending
        assert [d.text for d in loaded.drafts] == ["keep-zero", "keep-one"]

    final = json.dumps(client.updates[-1]["blocks"])
    assert "keep-zero" in final
    assert "failed" in final.lower()


@_db_test
@_db_loop
async def test_regenerate_without_chosen_idea_noops(
    monkeypatch: pytest.MonkeyPatch,
    test_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # A batch with no recorded chosen idea (e.g. a legacy/direct batch) can't be
    # re-drafted: Regenerate just re-renders the card and never calls generation.
    monkeypatch.setattr(actions, "SessionLocal", test_sessionmaker)

    async def boom(*args: Any, **kwargs: Any) -> ContentResult:
        raise AssertionError("generation must not run without a chosen idea")

    monkeypatch.setattr(actions, "generate_idea_drafts", boom)
    fake_app = FakeApp()
    register(fake_app)  # type: ignore[arg-type]
    callbacks = fake_app.callbacks

    batch_id, draft_ids = await _seed_pending_batch(test_sessionmaker, draft_texts=["old"])
    value = json.dumps({"b": str(batch_id), "d": str(draft_ids[0])})
    client = FakeClient()

    await _invoke(callbacks, "regenerate_draft", value=value, client=client)

    async with test_sessionmaker() as session:
        loaded = await repo.get_batch(session, batch_id)
        assert loaded is not None
        assert [d.text for d in loaded.drafts] == ["old"]  # unchanged
        assert not [e for e in loaded.events if e.action is ApprovalAction.regenerate]
    # The card was re-rendered (still the pending approval card).
    assert json.dumps(client.updates[-1]["blocks"]).find("old") != -1


@_db_test
@_db_loop
async def test_cancel_marks_cancelled_and_records_event(
    monkeypatch: pytest.MonkeyPatch,
    test_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    monkeypatch.setattr(actions, "SessionLocal", test_sessionmaker)
    fake_app = FakeApp()
    register(fake_app)  # type: ignore[arg-type]
    callbacks = fake_app.callbacks

    batch_id, _ = await _seed_pending_batch(test_sessionmaker, draft_texts=["a", "b"])
    value = json.dumps({"b": str(batch_id)})
    client = FakeClient()

    await _invoke(callbacks, "cancel_batch", value=value, client=client)

    async with test_sessionmaker() as session:
        loaded = await repo.get_batch(session, batch_id)
        assert loaded is not None
        assert loaded.status is BatchStatus.cancelled
        cancel_events = [e for e in loaded.events if e.action is ApprovalAction.cancel]
        assert len(cancel_events) == 1
        assert cancel_events[0].draft_id is None

    final_blocks = client.updates[-1]["blocks"]
    assert not _has_actions_block(final_blocks)
    assert "cancelled" in json.dumps(final_blocks).lower()


@_db_test
@_db_loop
async def test_unknown_batch_id_friendly_update(
    monkeypatch: pytest.MonkeyPatch,
    test_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    pub = FakePublisher()
    monkeypatch.setattr(actions, "SessionLocal", test_sessionmaker)
    fake_app = FakeApp()
    register(fake_app)  # type: ignore[arg-type]
    actions._set_publisher(pub)
    callbacks = fake_app.callbacks

    value = json.dumps({"b": str(uuid.uuid4()), "d": str(uuid.uuid4())})
    client = FakeClient()

    # Must not raise.
    await _invoke(
        callbacks,
        "approve_draft",
        value=value,
        client=client,
        block_id=f"draft_actions:{uuid.uuid4()}:0",
    )
    assert pub.calls == []


@_db_test
@_db_loop
async def test_bad_uuid_button_friendly_update(
    monkeypatch: pytest.MonkeyPatch,
    test_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    monkeypatch.setattr(actions, "SessionLocal", test_sessionmaker)
    fake_app = FakeApp()
    register(fake_app)  # type: ignore[arg-type]
    callbacks = fake_app.callbacks

    # Malformed JSON value with a recoverable batch id in block_id, but the
    # recovered id is not a UUID -> friendly "couldn't read that button".
    client = FakeClient()
    await _invoke(
        callbacks,
        "cancel_batch",
        value="garbage",
        client=client,
        block_id="draft_actions:not-a-uuid:0",
    )
    assert client.updates
    rendered = json.dumps(client.updates[-1]["blocks"]).lower()
    assert "couldn't read" in rendered or "couldn’t read" in rendered


@_db_test
@_db_loop
async def test_regenerate_chat_update_failure_persists(
    monkeypatch: pytest.MonkeyPatch,
    test_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    monkeypatch.setattr(actions, "SessionLocal", test_sessionmaker)

    async def fake_generate(*args: Any, **kwargs: Any) -> ContentResult:
        return ContentResult(
            postworthy=True, drafts=[Draft(text="fresh-zero"), Draft(text="fresh-one")]
        )

    monkeypatch.setattr(actions, "generate_idea_drafts", fake_generate)
    fake_app = FakeApp()
    register(fake_app)  # type: ignore[arg-type]
    callbacks = fake_app.callbacks

    batch_id, draft_ids = await _seed_drafted_batch(
        test_sessionmaker, draft_texts=["old-zero", "old-one"]
    )
    value = json.dumps({"b": str(batch_id), "d": str(draft_ids[0])})

    # Interim update succeeds; the FINAL render fails — but replace_drafts/record_event
    # committed before it, so the regenerate persists (chat_update is outside the txn).
    with pytest.raises(RuntimeError):
        await _invoke(callbacks, "regenerate_draft", value=value, client=FailOnNthClient(n=2))

    async with test_sessionmaker() as session:
        loaded = await repo.get_batch(session, batch_id)
        assert loaded is not None
        assert [d.text for d in loaded.drafts] == ["fresh-zero", "fresh-one"]
        assert any(e.action is ApprovalAction.regenerate for e in loaded.events)


@_db_test
@_db_loop
async def test_cancel_chat_update_failure_persists(
    monkeypatch: pytest.MonkeyPatch,
    test_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    monkeypatch.setattr(actions, "SessionLocal", test_sessionmaker)
    fake_app = FakeApp()
    register(fake_app)  # type: ignore[arg-type]
    callbacks = fake_app.callbacks

    batch_id, _ = await _seed_pending_batch(test_sessionmaker, draft_texts=["a", "b"])
    value = json.dumps({"b": str(batch_id)})

    # The single (final) chat_update fails, but the cancel committed before it.
    with pytest.raises(RuntimeError):
        await _invoke(callbacks, "cancel_batch", value=value, client=FailingClient())

    async with test_sessionmaker() as session:
        loaded = await repo.get_batch(session, batch_id)
        assert loaded is not None
        assert loaded.status is BatchStatus.cancelled
        assert any(e.action is ApprovalAction.cancel for e in loaded.events)


# --------------------------------------------------------------------------- #
# pick_idea (the ranked-idea picker -> draft).
# --------------------------------------------------------------------------- #


@_db_test
@_db_loop
async def test_pick_idea_drafts_and_flips_to_pending(
    monkeypatch: pytest.MonkeyPatch,
    test_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    monkeypatch.setattr(actions, "SessionLocal", test_sessionmaker)

    async def fake_generate(*args: Any, **kwargs: Any) -> ContentResult:
        return ContentResult(postworthy=True, drafts=[Draft(text="drafted-x")])

    monkeypatch.setattr(actions, "generate_idea_drafts", fake_generate)
    fake_app = FakeApp()
    register(fake_app)  # type: ignore[arg-type]
    callbacks = fake_app.callbacks

    batch_id = await _seed_selecting_batch(test_sessionmaker)
    value = json.dumps({"b": str(batch_id), "i": 1})  # pick the 2nd idea
    client = FakeClient()

    await _invoke(callbacks, "pick_idea", value=value, client=client)

    async with test_sessionmaker() as session:
        loaded = await repo.get_batch(session, batch_id)
        assert loaded is not None
        assert loaded.status is BatchStatus.pending
        assert loaded.chosen_idea_index == 1
        assert [d.text for d in loaded.drafts] == ["drafted-x"]
        pick_events = [e for e in loaded.events if e.action is ApprovalAction.pick]
        assert len(pick_events) == 1

    # interim ("Drafting…") + final approval card.
    assert len(client.updates) >= 2
    assert "drafted-x" in json.dumps(client.updates[-1]["blocks"])


@_db_test
@_db_loop
async def test_pick_idea_uses_invokers_voice(
    monkeypatch: pytest.MonkeyPatch,
    test_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    monkeypatch.setattr(actions, "SessionLocal", test_sessionmaker)

    captured: dict[str, Any] = {}

    async def fake_generate(*args: Any, **kwargs: Any) -> ContentResult:
        captured["voices"] = args[2] if len(args) > 2 else kwargs.get("voices")
        return ContentResult(postworthy=True, drafts=[Draft(text="drafted")])

    monkeypatch.setattr(actions, "generate_idea_drafts", fake_generate)
    fake_app = FakeApp()
    register(fake_app)  # type: ignore[arg-type]
    callbacks = fake_app.callbacks

    async with test_sessionmaker() as session:
        async with session.begin():
            await repo.upsert_voice_profile(
                session,
                slack_user_id=USER,
                platform=DraftPlatform.x,
                sample_posts=["mine1"],
                voice_card="USER X VOICE",
                positioning="testing",
            )
    batch_id = await _seed_selecting_batch(test_sessionmaker)
    value = json.dumps({"b": str(batch_id), "i": 0})

    await _invoke(callbacks, "pick_idea", value=value, client=FakeClient())

    voices = captured["voices"]
    assert voices is not None
    assert voices["x"].voice_card == "USER X VOICE"


@_db_test
@_db_loop
async def test_pick_idea_idempotent_on_nonselecting_batch(
    monkeypatch: pytest.MonkeyPatch,
    test_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # A batch already drafted (pending) must not re-draft on a stray pick click.
    monkeypatch.setattr(actions, "SessionLocal", test_sessionmaker)

    async def boom(*args: Any, **kwargs: Any) -> ContentResult:
        raise AssertionError("generation must not run for a non-selecting batch")

    monkeypatch.setattr(actions, "generate_idea_drafts", boom)
    fake_app = FakeApp()
    register(fake_app)  # type: ignore[arg-type]
    callbacks = fake_app.callbacks

    batch_id, _ = await _seed_drafted_batch(test_sessionmaker, draft_texts=["already"])
    value = json.dumps({"b": str(batch_id), "i": 0})
    client = FakeClient()

    await _invoke(callbacks, "pick_idea", value=value, client=client)

    # Re-rendered the existing approval card; no new pick event.
    assert "already" in json.dumps(client.updates[-1]["blocks"])
    async with test_sessionmaker() as session:
        loaded = await repo.get_batch(session, batch_id)
        assert loaded is not None
        assert not [e for e in loaded.events if e.action is ApprovalAction.pick]


@_db_test
@_db_loop
async def test_pick_idea_bad_index_shows_notice(
    monkeypatch: pytest.MonkeyPatch,
    test_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    monkeypatch.setattr(actions, "SessionLocal", test_sessionmaker)

    async def boom(*args: Any, **kwargs: Any) -> ContentResult:
        raise AssertionError("generation must not run for an out-of-range idea")

    monkeypatch.setattr(actions, "generate_idea_drafts", boom)
    fake_app = FakeApp()
    register(fake_app)  # type: ignore[arg-type]
    callbacks = fake_app.callbacks

    batch_id = await _seed_selecting_batch(test_sessionmaker, ideas=_IDEAS[:1])  # only index 0
    value = json.dumps({"b": str(batch_id), "i": 5})  # out of range
    client = FakeClient()

    await _invoke(callbacks, "pick_idea", value=value, client=client)

    rendered = json.dumps(client.updates[-1]["blocks"]).lower()
    assert "idea is gone" in rendered or "idea not found" in rendered
    async with test_sessionmaker() as session:
        loaded = await repo.get_batch(session, batch_id)
        assert loaded is not None
        assert loaded.status is BatchStatus.selecting  # unchanged
