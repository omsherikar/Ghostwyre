# Ghostwyre

A Slack-native AI agent. Run `/draft-post` in a channel and Ghostwyre reads the
recent conversation, decides whether anything is actually worth posting, drafts
2–3 options **in your voice**, and — only after you click **Approve** — publishes
the one you picked to X. The human-in-the-loop approval gate is mandatory; nothing
is ever auto-posted.

It's a small, deliberately-narrow portfolio project that takes one workflow
end-to-end with production-minded plumbing: async everywhere, a two-step LLM
pipeline, persisted approval state, and an at-most-once publish gate.

## Demo

![Ghostwyre demo](docs/demo.gif)

_Record: `/draft-post` → pick a draft → **Approve** → the live link posts back →
`/post-history` lists what you've published._

## How it works

```mermaid
flowchart TD
    U([You in Slack]) -->|/draft-post| CMD[commands.py]
    CMD --> ING[ingest.py<br/>fetch · filter · transcript]
    ING --> CON[content.py]
    CON -->|step A| EX[llm.extract_postworthy<br/>postworthy filter]
    EX -->|nothing worth posting| STOP([reply: nothing to post])
    EX -->|items| GEN[llm.generate_drafts<br/>drafts in your voice]
    GEN --> DB[(Postgres<br/>repo.create_batch)]
    DB --> CARD[blocks.py<br/>approval card]
    CARD -->|chat.postMessage| U
    U -->|Approve · Regenerate · Cancel| ACT[actions.py]
    ACT -->|Approve only| PUB[publisher<br/>DryRun / XPublisher]
    PUB -->|tweet| X([X / Twitter])
    ACT -->|audit + status| DB
    U -->|/post-history| CMD
    CMD -->|list_published| DB
```

- **Ingest** (`app/slack/ingest.py`) pulls recent messages, drops noise (bots,
  joins, the bot's own posts), resolves names, and builds a chronological
  transcript. The transcript is treated as confidential — never logged.
- **Generate** (`app/services/content.py` → `llm.py`) runs two LLM steps: a
  *postworthy filter* that can short-circuit with "nothing to post", then drafting
  in your voice. Both use structured JSON output + prompt caching on the system
  prompt and `voice.md`.
- **Approve** (`app/slack/{blocks,actions}.py`) persists the batch first, posts one
  living Block Kit card, and resolves every button by id. Approve publishes,
  Regenerate re-runs generation, Cancel dismisses — each idempotent.
- **Publish** (`app/services/publisher.py`, `x_publisher.py`) is a `PublisherClient`
  Protocol: `DryRunPublisher` by default, `XPublisher` (tweepy) when `PUBLISHER=x`.

## Quick start (dev)
```bash
make install        # uv sync
make db-up db-wait  # Postgres via Docker
make migrate        # alembic upgrade head
cp .env.example .env  # then fill in your tokens
make tweet          # smoke test: dry-run publish
make dev            # run the app (Slack Socket Mode)
make test           # fast tests   ·   make test-all = incl. Postgres
make lint           # ruff + mypy
```
Requires Python 3.12, Docker, and [uv](https://docs.astral.sh/uv/).

**Slack setup:** create an app, enable Socket Mode, add scopes `commands`,
`channels:history`, `chat:write`, and register two slash commands — `/draft-post`
and `/post-history`. Invite the bot to a channel, then run the commands there.

## Publishing to X

By default `PUBLISHER=dry` uses a no-network dry-run publisher (it returns a fake
link), so the whole flow works end-to-end without an X account. To publish for
real:

1. Provision the X **Basic** tier (paid) with **write** access and generate
   OAuth 1.0a user-context credentials (API key/secret + access token/secret).
2. Install the optional extra: `uv sync --extra x` (or `pip install ghostwyre[x]`).
3. In `.env`, set `PUBLISHER=x` and fill `X_API_KEY`, `X_API_SECRET`,
   `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET`.
4. `make tweet` posts a real hardcoded tweet (the publish-path smoke test); then
   `/draft-post` → **Approve** publishes the selected draft and drops the live
   link back in the channel.

**Approve is the only publish path** — nothing is ever auto-posted, and the
approval click publishes at most once. Reverting to dry-run is a one-setting
change (`PUBLISHER=dry`).

## Design decisions

- **Publishing is an interface, not a hard dependency.** Callers code against a
  `PublisherClient` Protocol; swapping dry-run ↔ real X is one setting, and tweepy
  stays an optional extra off the default path.
- **A postworthy filter before drafting.** The first LLM step can say "nothing
  here is worth posting" and skip drafting entirely — the difference between a real
  tool and a slop generator.
- **At-most-once publish gate.** Approve atomically claims the batch (a
  compare-and-swap `pending → approved`) and commits *before* the network call, so
  a double-click, Slack retry, or failed card update can never double-post.
  Ambiguous failures (rate limit / network) are surfaced for a human to verify
  rather than blindly retried.
- **Async everywhere**, structured logging with PII redaction (raw message and
  draft text are never logged), and secrets only via `pydantic-settings`.

## Stack
Python 3.12 · FastAPI · Slack Bolt (Socket Mode) · Anthropic SDK (Claude) ·
SQLAlchemy (async) + Alembic + PostgreSQL · tweepy (X, optional).

The full build plan lives in `dev-docs/chat-to-content-agent-v1-plan.md`.
