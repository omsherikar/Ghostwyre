"""Tests for Slack user-name resolution (no network — fake async client)."""

from __future__ import annotations

from app.slack.ingest import resolve_user_names, unique_user_ids

# Canned users.info payloads keyed by user id.
PROFILES = {
    "U01ALICE": {
        "user": {
            "name": "alice_login",
            "profile": {"display_name": "alice", "real_name": "Alice A"},
        }
    },
    "U02BOB": {
        "user": {"name": "bob_login", "profile": {"display_name": "", "real_name": "Bob B"}}
    },
    "U03CAR": {"user": {"name": "car_login", "profile": {"display_name": "", "real_name": ""}}},
}


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def users_info(self, *, user: str) -> dict[str, object]:
        self.calls.append(user)
        return PROFILES[user]


class RaisingClient:
    async def users_info(self, *, user: str) -> dict[str, object]:
        raise RuntimeError("user_not_found")


def test_unique_user_ids_dedups_and_skips_empty() -> None:
    messages = [
        {"user": "U01ALICE", "text": "a"},
        {"user": "U02BOB", "text": "b"},
        {"user": "U01ALICE", "text": "c"},
        {"text": "no user"},
        {"user": "", "text": "empty user"},
    ]
    assert unique_user_ids(messages) == ["U01ALICE", "U02BOB"]


async def test_resolve_prefers_display_then_real_then_name() -> None:
    client = FakeClient()
    names = await resolve_user_names(client, ["U01ALICE", "U02BOB", "U03CAR"])
    assert names == {
        "U01ALICE": "alice",  # display_name preferred
        "U02BOB": "Bob B",  # real_name fallback (display empty)
        "U03CAR": "car_login",  # name fallback (display + real empty)
    }


async def test_resolve_dedups_repeated_ids_one_fetch() -> None:
    client = FakeClient()
    names = await resolve_user_names(client, ["U01ALICE", "U01ALICE", "U02BOB"])
    assert client.calls == ["U01ALICE", "U02BOB"]
    assert names == {"U01ALICE": "alice", "U02BOB": "Bob B"}


async def test_resolve_failed_lookup_falls_back_to_id() -> None:
    names = await resolve_user_names(RaisingClient(), ["U0BAD"])
    assert names == {"U0BAD": "U0BAD"}
