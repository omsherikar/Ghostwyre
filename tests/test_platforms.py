"""Tests for the per-platform draft registry (pure, no I/O)."""

from __future__ import annotations

from app.platforms import PLATFORMS


def test_registry_has_x_and_linkedin() -> None:
    assert set(PLATFORMS) == {"x", "linkedin"}


def test_keys_match_their_dict_keys() -> None:
    assert all(key == spec.key for key, spec in PLATFORMS.items())


def test_x_is_publishable_linkedin_is_not() -> None:
    assert PLATFORMS["x"].publishable is True
    assert PLATFORMS["linkedin"].publishable is False


def test_labels_and_limits() -> None:
    assert PLATFORMS["x"].label == "X"
    assert PLATFORMS["linkedin"].label == "LinkedIn"
    # LinkedIn allows long posts; its display limit is well above a tweet.
    assert PLATFORMS["linkedin"].char_limit >= 1000


def test_every_platform_has_strategy() -> None:
    for spec in PLATFORMS.values():
        assert spec.strategy.strip(), f"{spec.key} is missing drafting strategy"
