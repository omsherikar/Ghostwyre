# Ghostwyre

Slack-native AI agent: reads recent channel messages → extracts postworthy
content → drafts in the user's voice → publishes to X after explicit approval.
Portfolio v1 — narrow on purpose. Full spec: `dev-docs/chat-to-content-agent-v1-plan.md`.

> Note: the files/commands below are the *intended* v1 layout. Most are not
> scaffolded yet — create them as you build the phases, and keep this file
> matching what's actually on disk.

## Stack
- Python 3.12 + FastAPI (async)
- Slack: Bolt for Python (Socket Mode in dev)
- LLM: Anthropic Python SDK (Claude)
- DB: PostgreSQL via SQLAlchemy (async) + Alembic migrations
- X API: Basic tier (write access)

## Commands
- `make dev` — run app w/ Socket Mode
- `make test` — pytest
- `make lint` — ruff + mypy
- `make db-up` — docker compose up postgres
- `alembic upgrade head` — apply migrations

## Conventions
- Async everywhere; no sync DB calls in request paths
- All LLM calls go through `services/llm.py` — never inline
- Secrets only via env / pydantic-settings; never hardcode
- Structured LLM output parsed in one place: strip code fences, then parse, retry once on bad JSON
- Slack's 3-second ack timeout: ack immediately, do real work async

## Hard rules
- NEVER commit `.env` or any token (a PreToolUse hook enforces this; don't work around it)
- NEVER auto-publish to X — the human-in-the-loop approval gate is mandatory
- NEVER log raw Slack message content at INFO level (PII / confidential-info leak risk)

## Build order
Phase 0 (access + hardcoded tweet) → 1 (Slack ingest) → 2 (generation) → 3 (approval UI) → 4 (publish) → 5 (polish).
Derisk infra first: get a hardcoded tweet posting and the slash-command echo working *before* touching the LLM.
