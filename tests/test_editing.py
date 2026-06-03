"""Tests for the Edit-a-draft flow (app/slack/editing.py).

The modal-open path is fast (DB faked); the view-submission path is DB-backed
(slow) — it seeds a pending draft, monkeypatches `distill_edit_memory`, and asserts
the draft text is updated, the card re-rendered, and learned rules are stored.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import repo
from app.db.models import BatchStatus, DraftPlatform
from app.services.schemas import VoiceMemoryNotes
from app.slack import editing
from app.slack.editing import EDIT_CALLBACK_ID, register

CHANNEL = "C123"
USER = "U456"
TS = "1700000000.000100"
_IDEA = {"summary": "s", "angle": "a", "score": 50, "evidence": ["q"]}


class FakeApp:
    def __init__(self) -> None:
        self.actions: dict[str, Any] = {}
        self.views: dict[str, Any] = {}
        self.commands: dict[str, Any] = {}

    def action(self, action_id: str) -> Any:
        def deco(fn: Any) -> Any:
            self.actions[action_id] = fn
            return fn

        return deco

    def view(self, callback_id: str) -> Any:
        def deco(fn: Any) -> Any:
            self.views[callback_id] = fn
            return fn

        return deco

    def command(self, name: str) -> Any:
        def deco(fn: Any) -> Any:
            self.commands[name] = fn
            return fn

        return deco


class RespondRecorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


class FakeClient:
    def __init__(self) -> None:
        self.opened: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []

    async def views_open(self, **kwargs: Any) -> dict[str, Any]:
        self.opened.append(kwargs)
        return {}

    async def chat_update(self, **kwargs: Any) -> dict[str, Any]:
        self.updates.append(kwargs)
        return {}


class FakeSession:
    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


def _fake_sessionmaker() -> FakeSession:
    return FakeSession()


async def _ack(*args: Any, **kwargs: Any) -> None:
    return None


def _callbacks() -> FakeApp:
    app = FakeApp()
    register(app)  # type: ignore[arg-type]
    return app


# --------------------------------------------------------------------------- #
# Modal open (fast — DB faked).
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_edit_button_opens_prefilled_modal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(editing, "SessionLocal", _fake_sessionmaker)
    b, d = uuid.uuid4(), uuid.uuid4()

    async def fake_get_draft(session: Any, draft_id: Any) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(text="DRAFT BODY TEXT")

    monkeypatch.setattr(repo, "get_draft", fake_get_draft)
    app = _callbacks()
    client = FakeClient()
    body = {
        "trigger_id": "T1",
        "channel": {"id": CHANNEL},
        "message": {"ts": TS},
    }
    action = {"value": json.dumps({"b": str(b), "d": str(d)})}

    await app.actions["edit_draft"](ack=_ack, body=body, action=action, client=client)

    assert len(client.opened) == 1
    view = client.opened[0]["view"]
    assert view["callback_id"] == EDIT_CALLBACK_ID
    # Pre-filled with the current draft text.
    assert view["blocks"][0]["element"]["initial_value"] == "DRAFT BODY TEXT"
    # private_metadata carries only ids; the card's (channel, ts) come from the batch.
    meta = json.loads(view["private_metadata"])
    assert meta == {"b": str(b), "d": str(d)}


@pytest.mark.asyncio
async def test_edit_button_malformed_value_opens_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(editing, "SessionLocal", _fake_sessionmaker)
    app = _callbacks()
    client = FakeClient()
    await app.actions["edit_draft"](
        ack=_ack, body={"trigger_id": "T1"}, action={"value": "garbage"}, client=client
    )
    assert client.opened == []


# --------------------------------------------------------------------------- #
# View submission (DB-backed).
# --------------------------------------------------------------------------- #

_db_test = pytest.mark.slow
_db_loop = pytest.mark.asyncio(loop_scope="session")


async def _seed_pending_draft(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    text: str,
    user_id: str = USER,
    platform: DraftPlatform = DraftPlatform.x,
    status: BatchStatus = BatchStatus.pending,
) -> Any:
    # Tests that assert on per-user memory pass a unique user_id — DB rows persist
    # across test_sessionmaker tests (only the `session` fixture truncates).
    async with sessionmaker() as session:
        async with session.begin():
            batch = await repo.create_idea_batch(
                session,
                channel_id=CHANNEL,
                user_id=user_id,
                transcript="t",
                candidate_ideas=[_IDEA],
            )
            drafts = await repo.replace_drafts(
                session, batch.id, [repo.DraftSpec(text=text, platform=platform)]
            )
            await repo.set_chosen_idea(session, batch.id, 0)
            await repo.set_batch_status(session, batch.id, status)
            await repo.set_batch_message_ts(session, batch.id, CHANNEL, TS)
            bid, did = batch.id, drafts[0].id
    return bid, did


def _view(*, b: Any, d: Any, edited: str) -> dict[str, Any]:
    meta = json.dumps({"b": str(b), "d": str(d), "channel": CHANNEL, "ts": TS})
    return {
        "private_metadata": meta,
        "state": {"values": {"draft": {"value": {"value": edited}}}},
    }


@_db_test
@_db_loop
async def test_edit_saves_text_and_learns(
    monkeypatch: pytest.MonkeyPatch,
    test_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    monkeypatch.setattr(editing, "SessionLocal", test_sessionmaker)

    async def fake_distill(
        original: str, edited: str, platform: str, *, client: Any, settings: Any
    ) -> VoiceMemoryNotes:
        return VoiceMemoryNotes(instructions=["No emojis."])

    monkeypatch.setattr(editing, "distill_edit_memory", fake_distill)
    app = _callbacks()
    client = FakeClient()

    b, d = await _seed_pending_draft(
        test_sessionmaker, text="I shipped it 🎉", user_id="Uedit_save"
    )
    await app.views[EDIT_CALLBACK_ID](
        ack=_ack, body={}, view=_view(b=b, d=d, edited="Shipped it."), client=client
    )

    async with test_sessionmaker() as session:
        loaded = await repo.get_batch(session, b)
        assert loaded is not None
        assert loaded.drafts[0].text == "Shipped it."  # edit saved (Approve publishes this)
        mem = await repo.get_voice_memory(session, "Uedit_save", limit_per_platform=10)
    assert mem.get("x") == ["No emojis."]  # learned rule stored
    # Card re-rendered with the edited text.
    assert client.updates and "Shipped it." in json.dumps(client.updates[-1]["blocks"])


@_db_test
@_db_loop
async def test_edit_noop_when_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    test_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    monkeypatch.setattr(editing, "SessionLocal", test_sessionmaker)

    async def boom(*args: Any, **kwargs: Any) -> VoiceMemoryNotes:
        raise AssertionError("must not distill when nothing changed")

    monkeypatch.setattr(editing, "distill_edit_memory", boom)
    app = _callbacks()
    client = FakeClient()

    b, d = await _seed_pending_draft(test_sessionmaker, text="Same text.", user_id="Uedit_noop")
    await app.views[EDIT_CALLBACK_ID](
        ack=_ack, body={}, view=_view(b=b, d=d, edited="Same text."), client=client
    )

    assert client.updates == []  # no re-render
    async with test_sessionmaker() as session:
        assert await repo.get_voice_memory(session, "Uedit_noop", limit_per_platform=10) == {}


@_db_test
@_db_loop
async def test_edit_noop_on_nonpending_batch(
    monkeypatch: pytest.MonkeyPatch,
    test_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    monkeypatch.setattr(editing, "SessionLocal", test_sessionmaker)

    async def boom(*args: Any, **kwargs: Any) -> VoiceMemoryNotes:
        raise AssertionError("must not distill on a non-pending batch")

    monkeypatch.setattr(editing, "distill_edit_memory", boom)
    app = _callbacks()
    client = FakeClient()

    b, d = await _seed_pending_draft(
        test_sessionmaker, text="original", user_id="Uedit_np", status=BatchStatus.approved
    )
    await app.views[EDIT_CALLBACK_ID](
        ack=_ack, body={}, view=_view(b=b, d=d, edited="a change"), client=client
    )

    assert client.updates == []
    async with test_sessionmaker() as session:
        loaded = await repo.get_batch(session, b)
        assert loaded is not None
        assert loaded.drafts[0].text == "original"  # unchanged


# --------------------------------------------------------------------------- #
# /voice — see & prune what's been learned.
# --------------------------------------------------------------------------- #


@_db_test
@_db_loop
async def test_voice_lists_rules_with_forget_buttons(
    monkeypatch: pytest.MonkeyPatch,
    test_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    monkeypatch.setattr(editing, "SessionLocal", test_sessionmaker)
    async with test_sessionmaker() as session:
        async with session.begin():
            await repo.add_voice_memory(
                session,
                slack_user_id="Uvoice",
                platform=DraftPlatform.x,
                instructions=["No emojis.", "Be terse."],
            )
    app = _callbacks()
    respond = RespondRecorder()

    await app.commands["/voice"](ack=_ack, command={"user_id": "Uvoice"}, respond=respond)

    assert len(respond.calls) == 1
    rendered = json.dumps(respond.calls[0]["blocks"])
    assert "No emojis." in rendered and "Be terse." in rendered
    assert "forget_memory" in rendered  # a Forget button per rule


@_db_test
@_db_loop
async def test_voice_empty_state(
    monkeypatch: pytest.MonkeyPatch,
    test_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    monkeypatch.setattr(editing, "SessionLocal", test_sessionmaker)
    app = _callbacks()
    respond = RespondRecorder()

    await app.commands["/voice"](ack=_ack, command={"user_id": "Uvoice_empty"}, respond=respond)

    assert "Nothing yet" in json.dumps(respond.calls[0]["blocks"])


@_db_test
@_db_loop
async def test_forget_memory_deletes_and_rerenders(
    monkeypatch: pytest.MonkeyPatch,
    test_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    monkeypatch.setattr(editing, "SessionLocal", test_sessionmaker)
    async with test_sessionmaker() as session:
        async with session.begin():
            rows = await repo.add_voice_memory(
                session,
                slack_user_id="Uforget",
                platform=DraftPlatform.x,
                instructions=["keep me", "forget me"],
            )
    forget_id = next(r.id for r in rows if r.instruction == "forget me")
    app = _callbacks()
    respond = RespondRecorder()

    await app.actions["forget_memory"](
        ack=_ack,
        body={"user": {"id": "Uforget"}},
        action={"value": json.dumps({"m": str(forget_id)})},
        respond=respond,
    )

    assert respond.calls and respond.calls[-1].get("replace_original") is True
    rendered = json.dumps(respond.calls[-1]["blocks"])
    assert "keep me" in rendered
    assert "forget me" not in rendered
    async with test_sessionmaker() as session:
        remaining = await repo.list_voice_memory(session, "Uforget")
    assert {r.instruction for r in remaining} == {"keep me"}
