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
