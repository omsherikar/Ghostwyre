"""Tests for the defensive JSON parsing helper (no network, no API)."""

from __future__ import annotations

import pytest

from app.services.json_parse import JSONParseError, loads_lenient, strip_code_fences


def test_json_fenced_with_language_tag() -> None:
    text = '```json\n{"key": "value"}\n```'
    result = loads_lenient(text)
    assert result == {"key": "value"}


def test_json_fenced_without_language_tag() -> None:
    text = '```\n{"key": "value"}\n```'
    result = loads_lenient(text)
    assert result == {"key": "value"}


def test_unfenced_raw_json() -> None:
    text = '{"key": "value"}'
    result = loads_lenient(text)
    assert result == {"key": "value"}


def test_surrounding_whitespace_tolerated() -> None:
    text = '  \n  ```json\n{"key": "value"}\n```  \n  '
    result = loads_lenient(text)
    assert result == {"key": "value"}


def test_json_array_fenced() -> None:
    text = "```json\n[1, 2, 3]\n```"
    result = loads_lenient(text)
    assert result == [1, 2, 3]


def test_invalid_json_raises_json_parse_error() -> None:
    text = '{"bad": json}'
    with pytest.raises(JSONParseError):
        loads_lenient(text)


def test_strip_code_fences_json_tag() -> None:
    text = '```json\n{"a": 1}\n```'
    assert strip_code_fences(text) == '{"a": 1}'


def test_strip_code_fences_no_tag() -> None:
    text = '```\n{"a": 1}\n```'
    assert strip_code_fences(text) == '{"a": 1}'


def test_strip_code_fences_no_fences() -> None:
    text = '{"a": 1}'
    assert strip_code_fences(text) == '{"a": 1}'


def test_strip_code_fences_preserves_inner_content() -> None:
    text = "```\nhello world\n```"
    assert strip_code_fences(text) == "hello world"


def test_json_parse_error_message_includes_prefix() -> None:
    bad = "not valid json at all"
    with pytest.raises(JSONParseError, match="not valid"):
        loads_lenient(bad)
