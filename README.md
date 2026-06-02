# Ghostwyre

A Slack-native AI agent: run `/draft-post`, and it reads recent channel
conversation, drafts 2–3 posts in your voice, and publishes the one you approve
to X — all from chat.

> 🚧 In development. This README is a placeholder; the full writeup
> (architecture diagram + demo GIF + setup) lands in Phase 5. See the build plan
> in `dev-docs/chat-to-content-agent-v1-plan.md`.

## Quick start (dev)
```bash
make install        # uv sync
make db-up db-wait  # Postgres via Docker
make migrate        # alembic upgrade head
cp .env.example .env  # then fill in your tokens
make tweet          # Phase 0 smoke: dry-run publish
make dev            # run the app (Phase 1+)
```

Requires Python 3.12, Docker, and [uv](https://docs.astral.sh/uv/).
