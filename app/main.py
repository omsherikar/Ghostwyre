"""Application entrypoint: FastAPI app with the Slack Bolt Socket Mode handler
started in its lifespan.

We use FastAPI for a health endpoint and future HTTP needs; the Slack connection
runs over Socket Mode (a websocket), so no public URL is required in dev. The
handler is `connect`ed (non-blocking) on startup and closed on shutdown.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from app.config import get_settings
from app.logging import configure_logging, get_logger
from app.slack.actions import register as register_actions
from app.slack.commands import register as register_commands
from app.slack.onboarding import register as register_onboarding

settings = get_settings()
configure_logging(settings.log_level)
logger = get_logger(__name__)

bolt_app = AsyncApp(
    token=settings.slack_bot_token,
    signing_secret=settings.slack_signing_secret,
)
register_commands(bolt_app)
register_actions(bolt_app)
register_onboarding(bolt_app)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Construct the handler here, not at import time: the Socket Mode client
    # grabs the running event loop on creation, which only exists once we're
    # inside the ASGI lifespan (importing the module must not require a loop).
    handler = AsyncSocketModeHandler(bolt_app, settings.slack_app_token)
    await handler.connect_async()  # type: ignore[no-untyped-call]
    logger.info("slack_socket_connected")
    try:
        yield
    finally:
        await handler.close_async()  # type: ignore[no-untyped-call]
        logger.info("slack_socket_closed")


app = FastAPI(title="Ghostwyre", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
