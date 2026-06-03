# Ghostwyre

Slack-native AI agent. `/draft-post` reads a channel → ranks the ideas worth
posting (with evidence) → you pick one → it drafts a LinkedIn + an X post **in
your voice** → you approve/edit → it publishes to X. It learns your voice from
your real posts (`/setup`) and from how you **edit** drafts (`/voice`).

Portfolio project — narrow on purpose. Specs: `dev-docs/chat-to-content-agent-v1-plan.md`
(v1, Phases 0–5) and `dev-docs/chat-to-content-agent-v2-plan.md` (v2, Pillars A–D).
Keep this file matching what's actually on disk.

## Stack
- Python 3.12 + FastAPI (async)
- Slack: Bolt for Python (Socket Mode in dev)
- LLM: provider-agnostic — Anthropic SDK (Claude, default) or Groq SDK; one client
  type, chosen by `LLM_PROVIDER`. All calls go through `app/services/llm.py`.
- DB: PostgreSQL via SQLAlchemy (async) + Alembic migrations
- X API: Basic tier (write), via tweepy — an optional extra (`uv sync --extra x`)

## Commands
- `make dev` — run app w/ Socket Mode   ·   `make tweet` — dry-run publish smoke test
- `make test` — fast tests   ·   `make test-all` — incl. Postgres (`@pytest.mark.slow`)
- `make lint` — ruff + mypy   ·   `make format` — ruff format
- `make db-up db-wait` — Postgres via Docker   ·   `make db-down`
- `make migrate` — `alembic upgrade head`   ·   `make revision m="…"` — autogenerate
- Slash commands (register in the Slack app): `/setup`, `/draft-post`, `/post-history`, `/voice`

## Where things live
- `app/main.py` — FastAPI + Bolt app; registers commands / actions / onboarding / editing.
- `app/config.py` — all settings (pydantic-settings).   `app/platforms.py` — per-platform spec + strategy.
- `app/slack/` — `ingest` (fetch/filter/transcript), `commands` (`/draft-post`, `/post-history`),
  `actions` (approve/regenerate/cancel/pick/reopen), `blocks` (Block Kit builders),
  `onboarding` (`/setup`), `editing` (Edit modal + `/voice`).
- `app/services/` — `llm` (every LLM call + structured-output helpers), `content`
  (orchestration: rank → draft), `schemas` (Pydantic structured-output models),
  `json_parse`, `publisher` + `x_publisher`.
- `app/db/` — `models`, `session`.   `migrations/versions/` — Alembic.

## Conventions
- Async everywhere; no sync DB calls in request paths.
- All LLM calls go through `app/services/llm.py` via `_structured_call` — never inline.
- Structured LLM output parsed in one place: strip code fences, parse, retry once on bad JSON.
- Slack's 3-second ack timeout: `ack()` immediately, then do the real (slow) work.
- One living card per batch, edited in place via `chat_update`; action handlers resolve
  everything **by id** from the button payload and are idempotent (safe on retries/double-clicks).
- Repo functions flush, never commit — the handler/caller owns the transaction.
- Status/action enums are VARCHAR (`Enum(..., native_enum=False, values_callable=…)`) so a
  new member needs no migration; UUID PKs; `server_default` on new non-null columns.
- Secrets only via env / pydantic-settings; never hardcode.
- Governance: build in small units, each green (`ruff` + `mypy` + `pytest`) before commit;
  no AI attribution in commits/PRs.

## Hard rules
- NEVER commit `.env` or any token (a PreToolUse hook enforces this; don't work around it)
- NEVER auto-publish to X — the human-in-the-loop approval gate is mandatory
- NEVER log raw Slack message content at INFO level (PII / confidential-info leak risk)
- NEVER put raw transcript / draft / idea / voice text in a Slack button value — ids only

## Status
v1 Phases 0–5 and v2 Pillars A–D are shipped:
- **A — voice from your posts:** `/setup` distills a per-platform `VoiceProfile`; drafts in your voice (seed = `voice.md`).
- **B — platform-native:** one LLM pass per platform, each fed that platform's voice + strategy + exemplars.
- **C — find the idea:** scan → `extract_postworthy` gate → `rank_ideas` (score + dedupe + evidence) → ranked-idea card → pick.
- **D — feedback memory:** **Edit** a draft → distil durable rules into `VoiceMemory` → steer future drafts; `/voice` to inspect/forget.

Known follow-ups (not yet built): data **retention/cleanup** (transcripts + post history are kept indefinitely) and encryption-at-rest.
