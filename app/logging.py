"""Structured logging setup with PII redaction.

Hard rule (CLAUDE.md): never log raw Slack message content at INFO. Use
`redact()` on any user-message text before it goes near a log call, and prefer
DEBUG for anything that could contain channel content.
"""

from __future__ import annotations

import logging
from typing import cast

import structlog


def configure_logging(level: str = "INFO") -> None:
    """Configure structlog + stdlib logging once at startup."""
    logging.basicConfig(format="%(message)s", level=level.upper())
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return cast("structlog.stdlib.BoundLogger", structlog.get_logger(name))


def redact(text: str, keep: int = 12) -> str:
    """Shorten user content for safe logging: keep a short prefix, hide the rest.

    Use this for anything derived from Slack messages so confidential content
    never lands in logs verbatim.
    """
    text = text.replace("\n", " ")
    if len(text) <= keep:
        return f"<{len(text)} chars>"
    return f"{text[:keep]}… <{len(text)} chars total>"
