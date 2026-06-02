# Ghostwyre — v1 Plan

> An AI agent that lives in Slack, reads recent channel conversation, turns it into post-ready content in your voice, and publishes to X after you approve it.

**Name:** *Ghostwyre* — ghostwriter + wired into your chat. (Coined spelling chosen to dodge the *Ghostwire: Tokyo* game collision and improve domain availability. Confirm `.dev`/`.com` + GitHub/npm before locking it in.)

---

## 1. Overview

**Problem:** Early-stage founders ship constantly but have nothing to post on socials. The raw material (decisions, wins, bugs, lessons) is already sitting in team chat — it just never makes it out as content.

**Solution:** A Slack-native agent. You run a slash command, it pulls recent messages, figures out what's actually worth posting, drafts 2–3 options in your voice, and lets you approve/edit before it publishes to X.

**Scope of v1 (intentionally narrow):**
- One Slack channel
- Manual trigger (`/draft-post`)
- X only (no LinkedIn)
- Approve-before-publish (no auto-posting)

This is a portfolio project. The goal is a clean, working, *impressive* demo — not a SaaS.

---

## 2. The v1 Flow

```
/draft-post  (slash command in Slack)
   → fetch last N messages (conversations.history)
   → filter noise (bot msgs, joins, slash-command echoes)
   → LLM step A: extract what's postworthy
   → LLM step B: generate 2–3 drafts in your voice
   → post drafts back to Slack (Block Kit, buttons per draft)
   → [Approve] [Regenerate] [Cancel]
   → on Approve → X API publishes → reply in Slack with live link
```

**Two principles that define quality:**
1. **Human-in-the-loop is mandatory.** Never auto-publish from chat — leak risk, tone risk, confidential-info risk.
2. **Voice is the moat.** Anyone can wire Slack → LLM → X. The differentiator is capturing *your* voice and knowing what's interesting vs. internal noise.

---

## 3. Tech Stack

| Layer | Choice | Notes |
|-------|--------|-------|
| Backend | Python + FastAPI | Async, clean Slack Bolt + Anthropic SDK support |
| Slack | Bolt for Python | Handles slash commands + interactivity cleanly |
| LLM | Claude API (Anthropic Python SDK) | Drafting + postworthy extraction |
| Publishing | X API (Basic tier) | Write access requires paid tier — verify early |
| Storage | PostgreSQL | Draft history + approval state (via SQLAlchemy or asyncpg) |
| Secrets | `.env` / env vars | Never commit tokens |

---

## 4. Phases

### Phase 0 — Setup & Access
*Do this first. This is where projects die — derisk the boring infra before the fun part.*

- [ ] Create Slack app; grab **bot token** + **signing secret**
- [ ] Add OAuth scopes: `commands`, `channels:history`, `chat:write`
- [ ] Register the `/draft-post` slash command (point at your endpoint / use Socket Mode for local dev)
- [ ] Create X developer account; provision a **Basic tier** app
- [ ] **Critical checkpoint:** post one hardcoded tweet via the X API. If you can't post a hardcoded tweet, nothing downstream matters.
- [ ] Repo scaffold, `.env` config, secrets handling, `.gitignore`

**Exit criteria:** A hardcoded tweet posts successfully from a script, and the Slack app is installed in your workspace.

---

### Phase 1 — Slack Ingestion
- [ ] Bolt app running locally (Socket Mode is easiest for dev — no public URL needed)
- [ ] `/draft-post` responds (even just an "on it…" ack within 3 seconds — Slack's timeout)
- [ ] Pull last `N` messages via `conversations.history`
- [ ] Noise filter: drop bot messages, channel joins/leaves, and your own slash-command echoes
- [ ] Normalize messages into a clean transcript string (user + text, chronological)

**Exit criteria:** Running `/draft-post` prints a clean, filtered transcript of recent channel activity to your logs.

---

### Phase 2 — Content Generation
- [ ] Create `voice.md`: 5–10 of your real past posts + explicit tone rules (e.g. *casual, direct, no corporate-speak, no emojis, no hashtags*)
- [ ] **LLM step A — Postworthy extraction:** given the transcript, identify what's worth posting (a shipped feature, a lesson, a funny bug) vs. logistics chatter. Return nothing if there's nothing good.
- [ ] **LLM step B — Drafting:** given the postworthy items + `voice.md`, generate 2–3 distinct drafts
- [ ] Force structured output: ask for a strict JSON array of drafts; parse safely (strip code fences before `JSON.parse`)
- [ ] Handle the "nothing postworthy" case gracefully

**Exit criteria:** Given a transcript, the chain returns 2–3 drafts as clean structured data that actually sound like you.

> The **postworthy filter** is the single most important step for output quality. Most clones skip it and produce slop. Don't skip it.

---

### Phase 3 — Approval UI
- [ ] Post drafts back to Slack as a **Block Kit** message
- [ ] Each draft gets its own section + `[Approve] [Regenerate] [Cancel]` buttons
- [ ] Wire up action handlers (the fiddly Bolt part — parse the interaction payload, identify which draft/button)
- [ ] **Approve** → trigger publish (Phase 4)
- [ ] **Regenerate** → re-run Phase 2 for that draft
- [ ] **Cancel** → dismiss / update the message

**Exit criteria:** Drafts render with working buttons; clicking each one does the right thing.

> Buttons (not "type yes") signal you understand real UX, not just API glue. Worth the extra effort for a portfolio piece.

---

### Phase 4 — Publish
- [ ] On Approve → call X API to post the selected draft
- [ ] Reply in Slack with the **live tweet link** on success
- [ ] Error handling: rate limit, auth failure, oversized post → readable message back in Slack (never a silent failure)

**Exit criteria:** Approving a draft publishes it to X and drops the live link back in the channel.

---

### Phase 5 — Polish (Portfolio Layer)
- [ ] Persist draft history + approval outcomes in Postgres (shows you think about state, not just a script)
- [ ] `README.md` with: what it does, architecture diagram, setup steps, and a **demo GIF** of the full flow
- [ ] Clean repo structure, env example file, short "design decisions" section in the README

**Exit criteria:** Someone landing on the repo understands it in 30 seconds and sees it work in the GIF.

---

## 5. Build Sequence & Timeline

**Order:** Phase 0 → 1 → 2 → 3 → 4 → 5

Get the hardcoded tweet (Phase 0) and the slash-command echo (Phase 1) working *before* touching the LLM. Derisk infra first so the fun part is never blocked.

**Realistic solo timeline:**
- Focused weekend: Phases 0–3
- Short follow-up session: Phases 4–5

---

## 6. Explicitly Out of Scope for v1

Resist these until v1 ships:

- Event-driven / proactive triggers (watching for "postworthy" moments automatically)
- LinkedIn publishing (API approval pain — not worth it for a portfolio piece)
- Multiple channels
- Post scheduling
- Analytics / performance tracking
- Multi-user / team accounts

All of the above = v2 territory.

---

## 7. Known Risks & Gotchas

| Risk | Mitigation |
|------|------------|
| X write access is paywalled (Basic ~paid tier) | Verify posting in Phase 0 before building anything else |
| Slack 3-second ack timeout | Ack immediately, do work async |
| LLM returns invalid JSON | Strip code fences, wrap parse in try/catch, retry once |
| Output sounds like generic AI | Invest in `voice.md`; this is the moat |
| Confidential info leaking into posts | Human-in-the-loop approval is non-negotiable |

---

## 8. Definition of Done (v1)

You can run `/draft-post` in your Slack channel, get 2–3 drafts that genuinely sound like you, approve one with a button, and watch it go live on X with the link posted back — all without touching the terminal. The repo has a README + demo GIF.
