"""Tests for the `/draft-post` slash-command handler (app/slack/commands.py).

These are fast and Postgres-free: the DB layer is faked. We monkeypatch
`app.slack.commands.SessionLocal` to a fake sessionmaker (so no real session is
opened), and `app.slack.commands.repo.create_batch` /
`...set_batch_message_ts` to plain async fakes that record their calls. The
content chain (`generate_post_drafts`) is also faked, so no LLM call happens.

A shared call log proves ordering: persist (create_batch) must happen BEFORE the
card is posted (chat_postMessage), then the message ts is recorded.

Fakes are hand-rolled (no unittest.mock) to match the house style.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from slack_sdk.errors import SlackApiError

from app import repo
from app.db.models import BatchStatus, DraftPlatform, DraftStatus
from app.services.content import ContentResult
from app.services.schemas import Draft
from app.slack import commands
from app.slack.commands import register

CHANNEL = "C123"
USER = "U456"
POSTED_TS = "12345.6789"


# --------------------------------------------------------------------------- #
# Fakes (hand-rolled — no unittest.mock).
# --------------------------------------------------------------------------- #


class FakeApp:
    """Captures @app.command callbacks by command name."""

    def __init__(self) -> None:
        self.callbacks: dict[str, Any] = {}

    def command(self, name: str) -> Any:
        def decorator(fn: Any) -> Any:
            self.callbacks[name] = fn
            return fn

        return decorator


class FakeDraftRow:
    """Minimal stand-in for an ORM Draft row (slot_index + text + status)."""

    def __init__(self, slot_index: int, text: str) -> None:
        self.id = uuid.uuid4()
        self.slot_index = slot_index
        self.text = text
        self.status = DraftStatus.pending
        self.platform = DraftPlatform.x


class FakeBatch:
    """Minimal stand-in for a persisted DraftBatch (enough for blocks builder)."""

    def __init__(self, *, channel_id: str, draft_texts: list[str]) -> None:
        self.id = uuid.uuid4()
        self.slack_channel_id = channel_id
        self.slack_message_ts: str | None = None
        self.status = BatchStatus.pending
        self.drafts = [FakeDraftRow(i, t) for i, t in enumerate(draft_texts)]


class FakeSession:
    """Async-context session that no-ops begin()/close (DB layer is faked)."""

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    def begin(self) -> FakeSession:
        return self


def fake_sessionmaker() -> FakeSession:
    return FakeSession()


class FakeClient:
    """Records chat_postMessage kwargs; returns a fixed ts like the web client.

    `history` / `users_info` cover the ingest step so the handler reaches the
    success path with a known transcript.
    """

    def __init__(
        self,
        *,
        messages: list[dict[str, Any]] | None = None,
        post_error: SlackApiError | None = None,
    ) -> None:
        self._messages = messages if messages is not None else [{"user": USER, "text": "hello"}]
        self._post_error = post_error
        self.posts: list[dict[str, Any]] = []
        self.call_log: list[str] | None = None  # shared call log, wired in by _wire

    async def conversations_history(self, **kwargs: Any) -> dict[str, Any]:
        return {"messages": self._messages}

    async def users_info(self, **kwargs: Any) -> dict[str, Any]:
        return {"user": {"profile": {"display_name": "Ada"}}}

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        if self.call_log is not None:
            self.call_log.append("chat_postMessage")
        self.posts.append(kwargs)
        if self._post_error is not None:
            raise self._post_error
        return {"ts": POSTED_TS}


def _command() -> dict[str, Any]:
    return {"channel_id": CHANNEL, "user_id": USER}


class FakeVoiceProfile:
    """Duck-typed stand-in for a VoiceProfile row (build_voices reads these attrs)."""

    def __init__(self, *, voice_card: str, positioning: str, sample_posts: list[str]) -> None:
        self.voice_card = voice_card
        self.positioning = positioning
        self.sample_posts = sample_posts


class Recorder:
    """Shared call log + fake repo functions so we can assert ordering/args."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.create_kwargs: dict[str, Any] | None = None
        self.set_ts_args: tuple[Any, ...] | None = None
        self.batch: FakeBatch | None = None
        self.voices: dict[str, Any] | None = None  # the voices map passed to generation
        self.voice_user_id: str | None = None  # the user id voice profiles were fetched for

    async def create_batch(
        self,
        session: Any,
        *,
        channel_id: str,
        user_id: str,
        transcript: str,
        drafts: list[Any],
    ) -> FakeBatch:
        self.calls.append("create_batch")
        self.create_kwargs = {
            "channel_id": channel_id,
            "user_id": user_id,
            "transcript": transcript,
            "drafts": drafts,
        }
        self.batch = FakeBatch(channel_id=channel_id, draft_texts=[s.text for s in drafts])
        return self.batch

    async def set_batch_message_ts(
        self, session: Any, batch_id: Any, channel_id: str, message_ts: str
    ) -> None:
        self.calls.append("set_batch_message_ts")
        self.set_ts_args = (batch_id, channel_id, message_ts)


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: ContentResult | None = None,
    generate_raises: bool = False,
    profiles: dict[str, Any] | None = None,
) -> tuple[Recorder, FakeClient]:
    """Monkeypatch the DB + content collaborators; return (recorder, client)."""
    rec = Recorder()
    monkeypatch.setattr(commands, "SessionLocal", fake_sessionmaker)
    monkeypatch.setattr(repo, "create_batch", rec.create_batch)
    monkeypatch.setattr(repo, "set_batch_message_ts", rec.set_batch_message_ts)

    async def fake_get_voice_profiles(session: Any, slack_user_id: str) -> dict[str, Any]:
        rec.calls.append("get_voice_profiles")
        rec.voice_user_id = slack_user_id
        return profiles or {}

    monkeypatch.setattr(repo, "get_voice_profiles", fake_get_voice_profiles)

    async def fake_generate(*args: Any, **kwargs: Any) -> ContentResult:
        rec.calls.append("generate")
        rec.voices = args[1] if len(args) > 1 else kwargs.get("voices")
        if generate_raises:
            raise RuntimeError("LLM exploded")
        assert result is not None
        return result

    monkeypatch.setattr(commands, "generate_post_drafts", fake_generate)
    return rec, FakeClient()


async def _ack(*args: Any, **kwargs: Any) -> None:
    return None


class RespondRecorder:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def __call__(self, message: str) -> None:
        self.messages.append(message)


async def _run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: ContentResult | None = None,
    generate_raises: bool = False,
    client: FakeClient | None = None,
    profiles: dict[str, Any] | None = None,
) -> tuple[Recorder, FakeClient, RespondRecorder]:
    rec, default_client = _wire(
        monkeypatch, result=result, generate_raises=generate_raises, profiles=profiles
    )
    cl = client if client is not None else default_client
    cl.call_log = rec.calls  # so chat_postMessage ordering is in the shared log
    fake_app = FakeApp()
    register(fake_app)  # type: ignore[arg-type]
    respond = RespondRecorder()
    await fake_app.callbacks["/draft-post"](
        ack=_ack, command=_command(), client=cl, respond=respond
    )
    return rec, cl, respond


# --------------------------------------------------------------------------- #
# Tests.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_persists_batch_before_posting(monkeypatch: pytest.MonkeyPatch) -> None:
    result = ContentResult(postworthy=True, drafts=[Draft(text="d0"), Draft(text="d1")])
    rec, client, respond = await _run(monkeypatch, result=result)

    # create_batch ran before chat_postMessage.
    assert rec.calls.index("create_batch") < rec.calls.index("chat_postMessage")
    assert rec.create_kwargs is not None
    assert rec.create_kwargs["channel_id"] == CHANNEL
    assert rec.create_kwargs["user_id"] == USER
    assert [s.text for s in rec.create_kwargs["drafts"]] == ["d0", "d1"]
    assert isinstance(rec.create_kwargs["transcript"], str)
    assert rec.create_kwargs["transcript"]  # non-empty


@pytest.mark.asyncio
async def test_sets_message_ts_after_post(monkeypatch: pytest.MonkeyPatch) -> None:
    result = ContentResult(postworthy=True, drafts=[Draft(text="d0")])
    rec, client, respond = await _run(monkeypatch, result=result)

    assert rec.calls.index("chat_postMessage") < rec.calls.index("set_batch_message_ts")
    assert rec.set_ts_args is not None
    batch_id, channel_id, ts = rec.set_ts_args
    assert rec.batch is not None
    assert batch_id == rec.batch.id
    assert channel_id == CHANNEL
    assert ts == POSTED_TS


@pytest.mark.asyncio
async def test_success_uses_block_kit_not_respond(monkeypatch: pytest.MonkeyPatch) -> None:
    result = ContentResult(postworthy=True, drafts=[Draft(text="d0")])
    rec, client, respond = await _run(monkeypatch, result=result)

    assert respond.messages == []  # respond NOT called on success
    assert len(client.posts) == 1
    posted = client.posts[0]
    assert posted["channel"] == CHANNEL
    assert "blocks" in posted and isinstance(posted["blocks"], list) and posted["blocks"]
    assert "text" in posted and isinstance(posted["text"], str) and posted["text"]


@pytest.mark.asyncio
async def test_nothing_postworthy_does_not_persist(monkeypatch: pytest.MonkeyPatch) -> None:
    result = ContentResult(postworthy=False, drafts=[])
    rec, client, respond = await _run(monkeypatch, result=result)

    assert "create_batch" not in rec.calls
    assert client.posts == []
    assert len(respond.messages) == 1
    assert "postworthy" in respond.messages[0].lower()


@pytest.mark.asyncio
async def test_postworthy_but_no_drafts_does_not_persist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # postworthy=True with an empty drafts list must not persist or post a
    # degenerate card — it falls through the gate to the nothing-postworthy reply.
    result = ContentResult(postworthy=True, drafts=[])
    rec, client, respond = await _run(monkeypatch, result=result)

    assert "create_batch" not in rec.calls
    assert client.posts == []
    assert len(respond.messages) == 1
    assert "postworthy" in respond.messages[0].lower()


@pytest.mark.asyncio
async def test_empty_transcript_does_not_persist(monkeypatch: pytest.MonkeyPatch) -> None:
    # No usable messages -> empty transcript -> early respond, no generation.
    client = FakeClient(messages=[])
    rec, cl, respond = await _run(monkeypatch, result=None, client=client)

    assert "create_batch" not in rec.calls
    assert "generate" not in rec.calls
    assert cl.posts == []
    assert len(respond.messages) == 1
    assert "couldn't find" in respond.messages[0].lower()


@pytest.mark.asyncio
async def test_generation_failure_does_not_persist(monkeypatch: pytest.MonkeyPatch) -> None:
    rec, client, respond = await _run(monkeypatch, generate_raises=True)

    assert "create_batch" not in rec.calls
    assert client.posts == []
    assert len(respond.messages) == 1
    assert "went wrong" in respond.messages[0].lower()


@pytest.mark.asyncio
async def test_transcript_passed_equals_to_transcript_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The transcript persisted must equal exactly what to_transcript produces for
    # the same canned history, so Regenerate has the exact input.
    from app.slack.ingest import to_transcript

    messages = [{"user": USER, "text": "hello"}, {"user": USER, "text": "world"}]
    client = FakeClient(messages=messages)
    expected = to_transcript(messages, user_names={USER: "Ada"})

    result = ContentResult(postworthy=True, drafts=[Draft(text="d0")])
    rec, cl, respond = await _run(monkeypatch, result=result, client=client)

    assert rec.create_kwargs is not None
    assert rec.create_kwargs["transcript"] == expected


@pytest.mark.asyncio
async def test_uses_invokers_stored_voice_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    # A stored profile for the invoker is fetched for command["user_id"] and its
    # voice card + sample posts reach generation as that platform's VoiceContext.
    profiles = {
        "x": FakeVoiceProfile(
            voice_card="USER X VOICE", positioning="known for testing", sample_posts=["p1", "p2"]
        )
    }
    result = ContentResult(postworthy=True, drafts=[Draft(text="d0")])
    rec, client, respond = await _run(monkeypatch, result=result, profiles=profiles)

    assert rec.voice_user_id == USER
    assert rec.voices is not None
    assert rec.voices["x"].voice_card == "USER X VOICE"
    assert rec.voices["x"].positioning == "known for testing"
    assert rec.voices["x"].sample_posts == ["p1", "p2"]


@pytest.mark.asyncio
async def test_falls_back_to_seed_when_no_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    # No stored profile -> every platform falls back to the voice.md seed
    # (no exemplars). The seed path is the most-tested (pre-onboarding) path.
    from app.platforms import PLATFORMS

    result = ContentResult(postworthy=True, drafts=[Draft(text="d0")])
    rec, client, respond = await _run(monkeypatch, result=result)

    assert rec.voices is not None
    assert set(rec.voices) == set(PLATFORMS)
    for ctx in rec.voices.values():
        assert ctx.sample_posts == []  # seed has no exemplars


@pytest.mark.asyncio
async def test_not_in_channel_responds_with_invite(monkeypatch: pytest.MonkeyPatch) -> None:
    err = SlackApiError("not_in_channel", {"ok": False, "error": "not_in_channel"})
    client = FakeClient(post_error=err)
    result = ContentResult(postworthy=True, drafts=[Draft(text="d0")])

    # Must not raise.
    rec, cl, respond = await _run(monkeypatch, result=result, client=client)

    assert len(respond.messages) == 1
    msg = respond.messages[0].lower()
    assert "invite" in msg
    # We attempted the post (so create_batch ran first), but never set the ts.
    assert "create_batch" in rec.calls
    assert "set_batch_message_ts" not in rec.calls


# --------------------------------------------------------------------------- #
# /post-history
# --------------------------------------------------------------------------- #


class HistoryRespond:
    """Captures respond(**kwargs) calls (blocks + text)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


def _fake_published(text: str, url: str) -> SimpleNamespace:
    """Duck-typed stand-in for an eager-loaded ApprovalEvent (renderer reads attrs)."""
    return SimpleNamespace(
        draft=SimpleNamespace(text=text),
        publish_url=url,
        created_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        batch=SimpleNamespace(slack_channel_id=CHANNEL),
    )


async def _run_history(
    monkeypatch: pytest.MonkeyPatch,
    *,
    published: list[Any],
    unconfirmed: list[Any],
) -> HistoryRespond:
    monkeypatch.setattr(commands, "SessionLocal", fake_sessionmaker)

    async def fake_list_published(session: Any, *, limit: int) -> list[Any]:
        return published

    async def fake_list_unconfirmed(session: Any) -> list[Any]:
        return unconfirmed

    monkeypatch.setattr(repo, "list_published", fake_list_published)
    monkeypatch.setattr(repo, "list_unconfirmed_approved", fake_list_unconfirmed)

    fake_app = FakeApp()
    register(fake_app)  # type: ignore[arg-type]
    respond = HistoryRespond()
    await fake_app.callbacks["/post-history"](ack=_ack, respond=respond)
    return respond


@pytest.mark.asyncio
async def test_post_history_renders_published_and_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    respond = await _run_history(
        monkeypatch,
        published=[_fake_published("hello post", "https://x.test/1")],
        unconfirmed=[SimpleNamespace()],
    )
    assert len(respond.calls) == 1
    call = respond.calls[0]
    assert isinstance(call["text"], str) and call["text"]
    rendered = json.dumps(call["blocks"])
    assert "hello post" in rendered
    assert "https://x.test/1" in rendered
    assert "couldn't be confirmed" in rendered


@pytest.mark.asyncio
async def test_post_history_empty_state(monkeypatch: pytest.MonkeyPatch) -> None:
    respond = await _run_history(monkeypatch, published=[], unconfirmed=[])
    assert len(respond.calls) == 1
    rendered = json.dumps(respond.calls[0]["blocks"])
    assert "No published posts yet" in rendered
