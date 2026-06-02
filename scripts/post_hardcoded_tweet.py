"""Phase 0 derisk checkpoint: prove the publish path end-to-end.

With PUBLISHER=dry this prints what would be posted and a (fake) link — no X
account or paid tier required. Once the real XPublisher exists, the *same*
script posts a real hardcoded tweet, satisfying the plan's critical checkpoint:
"if you can't post a hardcoded tweet, nothing downstream matters."

Run: `python -m scripts.post_hardcoded_tweet`  (or `make tweet`)
"""

from __future__ import annotations

import asyncio

from app.config import get_settings
from app.logging import configure_logging, get_logger
from app.services.publisher import PublishError, get_publisher

logger = get_logger(__name__)

HARDCODED = "Hello from Ghostwyre 👋 — wiring up the publish path."


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    publisher = get_publisher(settings)
    try:
        result = await publisher.publish(HARDCODED)
    except PublishError as exc:
        logger.error("publish_failed", error=str(exc))
        raise SystemExit(1) from exc
    mode = "DRY RUN" if result.dry_run else "LIVE"
    print(f"[{mode}] published: {result.url}")  # noqa: T201 — intentional CLI output


if __name__ == "__main__":
    asyncio.run(main())
