"""Tests for the `/draft-post` slash-command handler (app/slack/commands.py).

Fast and Postgres-free: the DB layer is faked. We monkeypatch
`app.slack.commands.SessionLocal` to a fake sessionmaker and the `repo.*`
functions to async fakes that record their calls, and fake the content chain
(`rank_channel_ideas` / `generate_idea_drafts`) so no LLM call happens. The pure
block builders run for real against the fake batches.

The flow has two branches: 2+ ranked ideas -> a `selecting` idea-pick card;
exactly 1 idea -> auto-draft now -> the approval card. A shared call log proves
persist happens BEFORE the card is posted, then the message ts is recorded.

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
from app.services.content import ContentResult, RankResult
from app.services.schemas import Draft, RankedIdea
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
    """Minimal stand-in for an ORM Draft row (enough for build_draft_blocks)."""

    def __init__(self, slot_index: int, text: str) -> None:
        self.id = uuid.uuid4()
        self.slot_index = slot_index
        self.text = text
        self.status = DraftStatus.pending
        self.platform = DraftPlatform.x


class FakeBatch:
    """Stand-in for a persisted DraftBatch (enough for both block builders)."""

    def __init__(
        self,
        *,
        channel_id: str,
        status: BatchStatus = BatchStatus.pending,
        candidate_ideas: list[dict[str, Any]] | None = None,
        draft_texts: list[str] | None = None,
    ) -> None:
        self.id = uuid.uuid4()
        self.slack_channel_id = channel_id
        self.slack_message_ts: str | None = None
        self.status = status
        self.candidate_ideas = candidate_ideas or []
        self.drafts = [FakeDraftRow(i, t) for i, t in enumerate(draft_texts or [])]


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
    """Records chat_postMessage kwargs; returns a fixed ts like the web client."""

    def __init__(
        self,
        *,
        messages: list[dict[str, Any]] | None = None,
        post_error: SlackApiError | None = None,
    ) -> None:
        self._messages = messages if messages is not None else [{"user": USER, "text": "hello"}]
        self._post_error = post_error
        self.posts: list[dict[str, Any]] = []
        self.call_log: list[str] | None = None  # shared call log, wired in by _run

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


def _idea(summary: str = "Shipped dark mode", score: int = 80) -> RankedIdea:
    return RankedIdea(summary=summary, angle="Ship it.", score=score, evidence=["alice: shipped"])


class Recorder:
    """Shared call log + fake repo/content functions so we can assert ordering/args."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.idea_batch_kwargs: dict[str, Any] | None = None
        self.draft_specs: list[Any] | None = None
        self.chosen_index: int | None = None
        self.statuses: list[BatchStatus] = []
        self.set_ts_args: tuple[Any, ...] | None = None
        self.batch: FakeBatch | None = None
        self.rank_transcript: str | None = None
        self.voices: dict[str, Any] | None = None
        self.voice_user_id: str | None = None
        self.generated_for: RankedIdea | None = None

    async def create_idea_batch(
        self,
        session: Any,
        *,
        channel_id: str,
        user_id: str,
        transcript: str,
        candidate_ideas: list[dict[str, Any]],
    ) -> FakeBatch:
        self.calls.append("create_idea_batch")
        self.idea_batch_kwargs = {
            "channel_id": channel_id,
            "user_id": user_id,
            "transcript": transcript,
            "candidate_ideas": candidate_ideas,
        }
        self.batch = FakeBatch(
            channel_id=channel_id,
            status=BatchStatus.selecting,
            candidate_ideas=candidate_ideas,
        )
        return self.batch

    async def replace_drafts(self, session: Any, batch_id: Any, specs: list[Any]) -> list[Any]:
        self.calls.append("replace_drafts")
        self.draft_specs = specs
        return []

    async def set_chosen_idea(self, session: Any, batch_id: Any, index: int) -> None:
        self.calls.append("set_chosen_idea")
        self.chosen_index = index

    async def set_batch_status(self, session: Any, batch_id: Any, status: BatchStatus) -> None:
        self.calls.append("set_batch_status")
        self.statuses.append(status)

    async def get_batch(self, session: Any, batch_id: Any) -> FakeBatch:
        self.calls.append("get_batch")
        texts = [s.text for s in (self.draft_specs or [])]
        drafted = FakeBatch(channel_id=CHANNEL, status=BatchStatus.pending, draft_texts=texts)
        if self.batch is not None:
            drafted.id = self.batch.id
        return drafted

    async def set_batch_message_ts(
        self, session: Any, batch_id: Any, channel_id: str, message_ts: str
    ) -> None:
        self.calls.append("set_batch_message_ts")
        self.set_ts_args = (batch_id, channel_id, message_ts)


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ideas: list[RankedIdea] | None = None,
    postworthy: bool = True,
    content: ContentResult | None = None,
    rank_raises: bool = False,
    generate_raises: bool = False,
    profiles: dict[str, Any] | None = None,
) -> tuple[Recorder, FakeClient]:
    """Monkeypatch the DB + content collaborators; return (recorder, client)."""
    rec = Recorder()
    monkeypatch.setattr(commands, "SessionLocal", fake_sessionmaker)
    monkeypatch.setattr(repo, "create_idea_batch", rec.create_idea_batch)
    monkeypatch.setattr(repo, "replace_drafts", rec.replace_drafts)
    monkeypatch.setattr(repo, "set_chosen_idea", rec.set_chosen_idea)
    monkeypatch.setattr(repo, "set_batch_status", rec.set_batch_status)
    monkeypatch.setattr(repo, "get_batch", rec.get_batch)
    monkeypatch.setattr(repo, "set_batch_message_ts", rec.set_batch_message_ts)

    async def fake_get_voice_profiles(session: Any, slack_user_id: str) -> dict[str, Any]:
        rec.calls.append("get_voice_profiles")
        rec.voice_user_id = slack_user_id
        return profiles or {}

    monkeypatch.setattr(repo, "get_voice_profiles", fake_get_voice_profiles)

    async def fake_rank(transcript: str, *, client: Any, settings: Any) -> RankResult:
        rec.calls.append("rank")
        rec.rank_transcript = transcript
        if rank_raises:
            raise RuntimeError("rank exploded")
        return RankResult(postworthy=postworthy, ideas=ideas or [])

    monkeypatch.setattr(commands, "rank_channel_ideas", fake_rank)

    async def fake_generate(
        idea: RankedIdea, transcript: str, voices: dict[str, Any], *, client: Any, settings: Any
    ) -> ContentResult:
        rec.calls.append("generate")
        rec.generated_for = idea
        rec.voices = voices
        if generate_raises:
            raise RuntimeError("LLM exploded")
        assert content is not None
        return content

    monkeypatch.setattr(commands, "generate_idea_drafts", fake_generate)
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
    ideas: list[RankedIdea] | None = None,
    postworthy: bool = True,
    content: ContentResult | None = None,
    rank_raises: bool = False,
    generate_raises: bool = False,
    client: FakeClient | None = None,
    profiles: dict[str, Any] | None = None,
) -> tuple[Recorder, FakeClient, RespondRecorder]:
    rec, default_client = _wire(
        monkeypatch,
        ideas=ideas,
        postworthy=postworthy,
        content=content,
        rank_raises=rank_raises,
        generate_raises=generate_raises,
        profiles=profiles,
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
# Tests — multi-idea path (the ranked-idea picker).
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_multiple_ideas_post_idea_card_before_setting_ts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ideas = [_idea("Shipped dark mode", 90), _idea("Killed a feature", 70)]
    rec, client, respond = await _run(monkeypatch, ideas=ideas)

    # Persist (selecting) BEFORE posting; ts recorded after.
    assert rec.calls.index("create_idea_batch") < rec.calls.index("chat_postMessage")
    assert rec.calls.index("chat_postMessage") < rec.calls.index("set_batch_message_ts")
    # Both ideas stored as JSON dicts; no drafting happened.
    assert rec.idea_batch_kwargs is not None
    assert len(rec.idea_batch_kwargs["candidate_ideas"]) == 2
    assert "generate" not in rec.calls
    # The posted card is the idea picker (a "Draft this" button per idea).
    rendered = json.dumps(client.posts[0]["blocks"])
    assert "pick_idea" in rendered
    assert respond.messages == []


@pytest.mark.asyncio
async def test_idea_card_records_message_ts(monkeypatch: pytest.MonkeyPatch) -> None:
    ideas = [_idea("a", 90), _idea("b", 70)]
    rec, client, respond = await _run(monkeypatch, ideas=ideas)
    assert rec.set_ts_args is not None
    batch_id, channel_id, ts = rec.set_ts_args
    assert rec.batch is not None
    assert batch_id == rec.batch.id
    assert channel_id == CHANNEL
    assert ts == POSTED_TS


# --------------------------------------------------------------------------- #
# Tests — single-idea path (auto-draft straight to the approval card).
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_single_idea_auto_drafts_approval_card(monkeypatch: pytest.MonkeyPatch) -> None:
    content = ContentResult(postworthy=True, drafts=[Draft(text="d0")])
    rec, client, respond = await _run(monkeypatch, ideas=[_idea()], content=content)

    # Drafted now: generate ran, chosen index set to 0, batch flipped to pending.
    assert "generate" in rec.calls
    assert rec.chosen_index == 0
    assert BatchStatus.pending in rec.statuses
    assert [s.text for s in (rec.draft_specs or [])] == ["d0"]
    # The posted card is the approval card (the draft text + the transparency line).
    rendered = json.dumps(client.posts[0]["blocks"])
    assert "d0" in rendered
    assert "1 idea worth posting" in rendered
    assert respond.messages == []


@pytest.mark.asyncio
async def test_single_idea_generation_failure_does_not_persist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec, client, respond = await _run(monkeypatch, ideas=[_idea()], generate_raises=True)
    assert "create_idea_batch" not in rec.calls
    assert client.posts == []
    assert len(respond.messages) == 1
    assert "went wrong" in respond.messages[0].lower()


@pytest.mark.asyncio
async def test_single_idea_uses_invokers_stored_voice(monkeypatch: pytest.MonkeyPatch) -> None:
    profiles = {
        "x": FakeVoiceProfile(
            voice_card="USER X VOICE", positioning="known for testing", sample_posts=["p1", "p2"]
        )
    }
    content = ContentResult(postworthy=True, drafts=[Draft(text="d0")])
    rec, client, respond = await _run(
        monkeypatch, ideas=[_idea()], content=content, profiles=profiles
    )
    assert rec.voice_user_id == USER
    assert rec.voices is not None
    assert rec.voices["x"].voice_card == "USER X VOICE"
    assert rec.voices["x"].sample_posts == ["p1", "p2"]


@pytest.mark.asyncio
async def test_single_idea_falls_back_to_seed_when_no_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.platforms import PLATFORMS

    content = ContentResult(postworthy=True, drafts=[Draft(text="d0")])
    rec, client, respond = await _run(monkeypatch, ideas=[_idea()], content=content)
    assert rec.voices is not None
    assert set(rec.voices) == set(PLATFORMS)
    for ctx in rec.voices.values():
        assert ctx.sample_posts == []  # seed has no exemplars


# --------------------------------------------------------------------------- #
# Tests — gates and failures.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_nothing_postworthy_does_not_persist(monkeypatch: pytest.MonkeyPatch) -> None:
    rec, client, respond = await _run(monkeypatch, postworthy=False, ideas=[])
    assert "create_idea_batch" not in rec.calls
    assert client.posts == []
    assert len(respond.messages) == 1
    assert "postworthy" in respond.messages[0].lower()


@pytest.mark.asyncio
async def test_empty_transcript_does_not_rank(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient(messages=[])
    rec, cl, respond = await _run(monkeypatch, client=client)
    assert "rank" not in rec.calls
    assert "create_idea_batch" not in rec.calls
    assert cl.posts == []
    assert len(respond.messages) == 1
    assert "couldn't find" in respond.messages[0].lower()


@pytest.mark.asyncio
async def test_ranking_failure_responds_gracefully(monkeypatch: pytest.MonkeyPatch) -> None:
    rec, client, respond = await _run(monkeypatch, rank_raises=True)
    assert "create_idea_batch" not in rec.calls
    assert client.posts == []
    assert len(respond.messages) == 1
    assert "went wrong" in respond.messages[0].lower()


@pytest.mark.asyncio
async def test_transcript_passed_to_ranking(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.slack.ingest import to_transcript

    messages = [{"user": USER, "text": "hello"}, {"user": USER, "text": "world"}]
    client = FakeClient(messages=messages)
    expected = to_transcript(messages, user_names={USER: "Ada"})

    rec, cl, respond = await _run(monkeypatch, ideas=[_idea(), _idea("b")], client=client)
    assert rec.rank_transcript == expected
    # The same transcript is persisted on the batch (so the pick step can draft it).
    assert rec.idea_batch_kwargs is not None
    assert rec.idea_batch_kwargs["transcript"] == expected


@pytest.mark.asyncio
async def test_not_in_channel_responds_with_invite(monkeypatch: pytest.MonkeyPatch) -> None:
    err = SlackApiError("not_in_channel", {"ok": False, "error": "not_in_channel"})
    client = FakeClient(post_error=err)
    rec, cl, respond = await _run(monkeypatch, ideas=[_idea(), _idea("b")], client=client)

    assert len(respond.messages) == 1
    assert "invite" in respond.messages[0].lower()
    # We attempted the post (so create_idea_batch ran first), but never set the ts.
    assert "create_idea_batch" in rec.calls
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
