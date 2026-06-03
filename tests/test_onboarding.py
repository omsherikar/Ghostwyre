"""Tests for the /setup onboarding modal + submission (app/slack/onboarding.py).

The command/view callbacks are captured via a tiny fake AsyncApp (no Socket Mode).
The pure modal-open test is fast; the submission tests are DB-backed (slow): they
monkeypatch `onboarding.SessionLocal` to the test DB and `distill_voice_profile`
to a fake, then assert profiles are upserted and a confirmation is DM'd.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import repo
from app.services.schemas import VoiceCard
from app.slack import onboarding
from app.slack.onboarding import register


class FakeApp:
    def __init__(self) -> None:
        self.commands: dict[str, Any] = {}
        self.views: dict[str, Any] = {}

    def command(self, name: str) -> Any:
        def deco(fn: Any) -> Any:
            self.commands[name] = fn
            return fn

        return deco

    def view(self, callback_id: str) -> Any:
        def deco(fn: Any) -> Any:
            self.views[callback_id] = fn
            return fn

        return deco


class FakeClient:
    def __init__(self) -> None:
        self.opened: list[dict[str, Any]] = []
        self.messages: list[dict[str, Any]] = []

    async def views_open(self, **kwargs: Any) -> dict[str, Any]:
        self.opened.append(kwargs)
        return {}

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.messages.append(kwargs)
        return {}


async def _ack(*args: Any, **kwargs: Any) -> None:
    return None


def _view(x_posts: str = "", linkedin_posts: str = "", goals: str = "") -> dict[str, Any]:
    def field(value: str) -> dict[str, Any]:
        return {"value": {"value": value}}

    return {
        "state": {
            "values": {
                "x_posts": field(x_posts),
                "linkedin_posts": field(linkedin_posts),
                "goals": field(goals),
            }
        }
    }


_db_test = pytest.mark.slow
_db_loop = pytest.mark.asyncio(loop_scope="session")


@pytest.mark.asyncio
async def test_setup_opens_modal() -> None:
    app = FakeApp()
    register(app)  # type: ignore[arg-type]
    client = FakeClient()

    await app.commands["/setup"](ack=_ack, body={"trigger_id": "T1"}, client=client)

    assert len(client.opened) == 1
    view = client.opened[0]["view"]
    assert view["callback_id"] == "ghostwyre_setup"
    block_ids = {b.get("block_id") for b in view["blocks"]}
    assert {"x_posts", "linkedin_posts", "goals"} <= block_ids


@_db_test
@_db_loop
async def test_setup_submission_distills_and_upserts(
    monkeypatch: pytest.MonkeyPatch,
    test_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async def fake_distill(
        posts: list[str], goals: str, platform: str, *, client: Any, settings: Any
    ) -> VoiceCard:
        return VoiceCard(voice_card=f"vc for {platform}", positioning="pos")

    monkeypatch.setattr(onboarding, "SessionLocal", test_sessionmaker)
    monkeypatch.setattr(onboarding, "distill_voice_profile", fake_distill)
    app = FakeApp()
    register(app)  # type: ignore[arg-type]
    client = FakeClient()

    view = _view(x_posts="post one\n\npost two", linkedin_posts="a linkedin post", goals="ship")
    await app.views["ghostwyre_setup"](
        ack=_ack, body={"user": {"id": "U1"}}, view=view, client=client
    )

    async with test_sessionmaker() as session:
        profiles = await repo.get_voice_profiles(session, "U1")
    assert set(profiles) == {"x", "linkedin"}
    assert profiles["x"].sample_posts == ["post one", "post two"]
    assert profiles["x"].voice_card == "vc for x"
    assert client.messages and "Learned your voice" in client.messages[-1]["text"]


@_db_test
@_db_loop
async def test_setup_submission_empty_saves_nothing(
    monkeypatch: pytest.MonkeyPatch,
    test_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async def boom(*args: Any, **kwargs: Any) -> VoiceCard:
        raise AssertionError("distill must not run when nothing was pasted")

    monkeypatch.setattr(onboarding, "SessionLocal", test_sessionmaker)
    monkeypatch.setattr(onboarding, "distill_voice_profile", boom)
    app = FakeApp()
    register(app)  # type: ignore[arg-type]
    client = FakeClient()

    await app.views["ghostwyre_setup"](
        ack=_ack, body={"user": {"id": "U2"}}, view=_view(), client=client
    )

    async with test_sessionmaker() as session:
        assert await repo.get_voice_profiles(session, "U2") == {}
    assert client.messages and "Nothing to learn" in client.messages[-1]["text"]
