"""Defensive JSON parsing helper for the LLM layer.

The Anthropic API's structured-output mode returns guaranteed JSON, but this
module provides a fallback parser for model text that may wrap JSON in code
fences or include surrounding whitespace.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Matches an optional leading ```json or ``` fence and a trailing ``` fence.
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)


class JSONParseError(Exception):
    """Raised when JSON parsing fails after fence-stripping."""


def strip_code_fences(text: str) -> str:
    """Remove leading ```json or ``` and trailing ``` fences, if present.

    Returns the inner content with surrounding whitespace stripped. If no
    fences are found, returns the trimmed text unchanged.
    """
    match = _FENCE_RE.match(text.strip())
    if match:
        return match.group(1).strip()
    return text.strip()


def loads_lenient(text: str) -> Any:
    """Parse JSON from *text*, stripping code fences first.

    Raises :class:`JSONParseError` if the result is not valid JSON after
    fence-stripping, including a short prefix of the offending text in the
    error message.
    """
    cleaned = strip_code_fences(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        prefix = cleaned[:60]
        raise JSONParseError(f"Invalid JSON (starts with: {prefix!r})") from exc
