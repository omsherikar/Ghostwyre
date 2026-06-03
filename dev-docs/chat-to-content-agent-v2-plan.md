# Ghostwyre v2 — "Actually usable": learn the voice, know the platforms, find the signal

> Forward-looking product + technical plan for the next version. The v1 spec is in
> `chat-to-content-agent-v1-plan.md`; v1 is built and merged. This document is a
> plan, not a commitment to build it all at once — see **Phasing**.

## Context (why v2)
v1 shipped working plumbing: Slack ingest, LLM generation (Claude/Groq), the
human-in-the-loop approval gate, and multi-platform long drafts. The pieces work —
but the **product** is generic. As the critique put it: nobody asked to see my past
posts, nobody asked my tone or what does well on LinkedIn vs X, and nobody actually
filtered 100 Slack messages to find the *one* idea worth posting. It also never
asks about the user and never learns from their choices. Without these, "ours is the
same as everyone else's." v2 closes exactly those gaps so the tool is usable and
differentiated.

## The gaps (verbatim) → the fixes
1. *"No one asked to see my previous posts."* → **Pillar A**: learn the voice from the user's real posts.
2. *"No one asked what tone I write in or what does well on LinkedIn vs X."* → **Pillar B**: per-platform voice + researched platform strategy + ask the user's goals.
3. *"No one thought about filtering 100 Slack messages to find the one idea worth posting."* → **Pillar C**: multi-stage extract → cluster → score → rank, **with evidence**.
4. Latent: *it never learns from what I picked.* → **Pillar D**: feedback memory (close the loop on `ApprovalEvent`).

## Current baseline (what v2 extends/replaces — from the code)
- **Voice** = one static `voice.md` via `content.load_voice()`: generic, shared, no per-user / per-platform / learning.
- **Extraction** = a single `llm.extract_postworthy` pass over a 50-message transcript → `items{summary,reason}`; no clustering, scoring, ranking, evidence, or map-reduce; "pick the strongest" is implicit in the draft prompt.
- **No `User`/`VoiceProfile`/preferences tables** — effectively single-tenant (only `DraftBatch`/`Draft`/`ApprovalEvent`); only `/draft-post` + `/post-history`; no onboarding.
- **`ApprovalEvent`** (approve/regenerate/cancel) is recorded but **never consumed** — a dead-end feedback signal.

## Product principles
Know the user · know the platforms · find the signal · improve with use · keep the human-in-the-loop publish gate.

---

## Pillar A — Voice that's actually yours
Goal: drafts read like *you* typed them, not a press release.
- **Onboarding ingest (`/setup`)**: collect the user's real posts + a few questions (who you are, what you want to be known for, topics, per-platform tone/goals). Sources: **X via API import** (read recent posts) and/or **paste**; **LinkedIn via paste / data-export** (no read API). ~10–20 posts/platform gives a strong clone (industry tools reach "in your voice" within minutes from 20+ posts).
- **`VoiceProfile` (per user, per platform)** holds three things:
  1. a **corpus** of the user's real posts (embedded for retrieval),
  2. a distilled **voice card** — LLM-inferred *hypotheses* about hooks, sentence rhythm, vocabulary, formatting, do/don't (research: hypothesis-driven style modeling beats tone "sliders"),
  3. a short **positioning blurb** (themes, audience, what they stand for).
- **Retrieval-augmented few-shot at draft time**: for the chosen idea + platform, retrieve the 3–5 most relevant of the user's real posts and few-shot them alongside the voice card. Research is clear: **RAG few-shot ≥ fine-tuning**, and **2–5 samples suffice** — so no fine-tuning needed (cheaper, instant, no training infra).
- Replaces static `voice.md`; the file becomes the seed/fallback for users who haven't onboarded.

## Pillar B — Platform-native strategy
- A **strategy layer per platform** encoding researched best practices, used to shape generation (structure, length, hook), distinct per platform — not one prompt:
  - **LinkedIn**: ~1,200–1,800 chars gets best organic reach; **personal-story hook ≈ 4×**, contrarian ≈ 3× engagement; medium-form, story-driven, whitespace.
  - **X**: text outperforms images/video/links; short (71–100 chars) ≈ +17% engagement, 240–259 chars get the most likes; built for **conversation/reply chains**; long-form is the Premium option.
- **Ask the user's per-platform goals/tone** during onboarding (what to be known for; professional vs spicy).
- Generation conditions on **(platform strategy × the user's per-platform voice)**.
- *Stretch*: weight the user's top-performing posts (X engagement via API) higher in retrieval — learn what does well *for them*.

## Pillar C — Find the one idea worth posting
Replace the single shallow pass with a transparent, inspectable pipeline:
1. Ingest a larger, configurable window; **map-reduce chunking** for long histories (beyond the 50-message cap).
2. **Extract** candidate ideas per chunk, each tagged with the supporting message refs.
3. **Cluster/dedupe** equivalent ideas (LLM-as-judge incremental bucketing — e.g. MUSERAG-style).
4. **Score** each candidate against explicit criteria — novelty, specificity, audience value, "would you proudly share this", platform fit — via LLM-as-judge (optional multi-vote for robustness).
5. **Rank → surface the top idea(s) WITH evidence** (the source messages + the score/why). Optionally show the ranked shortlist in Slack and let the user **pick which idea to draft**. This makes "we filtered 100 messages down to the one worth posting" visible and trustworthy — the exact gap called out.
- New `idea_ranking.py` + `chunker.py`; extend `PostworthyItem` with `score`, `cluster`, `evidence`; reuse the existing structured-output + retry-once infra.

## Pillar D — Gets better as you use it (feedback memory)
- Consume `ApprovalEvent`: **approvals → add to the voice corpus** (positive exemplars); **regenerate/cancel → negative signal**; **edits before posting → the strongest signal** (the corrected text becomes a sticky memory instruction).
- A learning loop appends memory instructions to the voice card ("don't open with 'I'", "stop using 'unlock'") and tunes ranking weights — the industry "every edit sticks" model. Closes the loop the critique flagged.

---

## Data model (new) + migrations
`User`, `VoiceProfile` (per platform: corpus, embeddings, voice_card, positioning), `UserPreference`, `OnboardingState`; extend `PostworthyItem` with `score`/`cluster`/`evidence`; per-user scoping on batches. Alembic migrations (the enum/VARCHAR conventions and `native_enum=False` pattern carry over).

## Onboarding & commands (UX)
`/setup` (guided Block-Kit modals: paste/import posts + answer goal questions), `/teach-voice` (add posts / correct the voice), `/settings`, and a **"pick which idea"** step on `/draft-post`. Gate `/draft-post` on onboarding for first run.

## Architecture & seams (extend, don't rewrite)
- **New**: `app/services/{voice_profile,idea_ranking,chunker,feedback}.py`, `app/slack/onboarding.py`, new tables + `repo` queries.
- **Extend**: `content.generate_post_drafts` (per-user voice fetch + ranking layer), `llm.generate_drafts` (RAG few-shot + per-platform strategy), `slack/ingest` (larger window + chunking), `slack/{commands,actions}` (per-user voice, feedback hook, idea-pick step).
- **Keep**: provider-agnostic LLM (Claude/Groq), async SQLAlchemy + Alembic, Bolt Socket Mode, the at-most-once publish gate, and "never log raw transcript/draft text."

## What makes this different (vs generic AI post tools)
Learns from **your real posts** (RAG few-shot + memory, not tone sliders) · **platform-native** strategy with real engagement data · **transparent signal-finding with evidence** (not one shallow pass) · **improves every time you edit or approve**.

## Phasing (independently shippable)
- **v2.0 — ✅ SHIPPED** (Pillars A + B). `/setup` onboarding modal → `distill_voice_profile` → a `VoiceProfile` per `(slack_user_id, platform)`; per-platform strategy in `app/platforms.py`; generation refactored to **one LLM pass per platform** (`generate_platform_draft`) fed that platform's voice card + positioning + strategy + a few of the user's real posts as exemplars (lightweight token-overlap selection, no embeddings yet); `/draft-post` + **Regenerate** wired to the invoker's voice, falling back to the `voice.md` seed when no profile exists. Exemplar count via `VOICE_EXEMPLAR_COUNT`. *(Biggest perceived-quality jump; drafts finally sound like the user.)*
- **v2.1** — Multi-stage idea ranking with evidence + the "pick the idea" UI.
- **v2.2** — Feedback memory loop (edits/approvals refine voice + ranking).

## Risks / open questions
- **LinkedIn has no read API** → rely on paste / data-export. **X read API** tier/cost for import + engagement data.
- **Storing users' real posts** = a privacy/retention concern (encrypt, scope per user, allow delete); the no-confidential-content rules still apply.
- **Multi-tenant scope** (per-user within a workspace) is a real shift from v1's single-workspace assumption.
- LLMs are still imperfect at nuanced personal style → lean on RAG few-shot + memory + user edits, and set expectations (first draft ~80%, converges with memory).

## Verification (when built)
- **Voice**: paste ~15 posts → a voice card + retrievable corpus is stored; a draft visibly echoes the user's hooks/phrasing.
- **Platform**: the LinkedIn draft lands ~1,200–1,800 chars, story/hook-led; the X draft is punchy/conversational — measurably different shapes.
- **Ranking**: feed a ~150-message transcript with one strong idea buried in noise → the pipeline surfaces it with its supporting messages; the shortlist is inspectable.
- **Feedback**: edit a draft before approving → the correction appears as a memory instruction and the next draft reflects it.

## References (web research)
- Oiti — AI ghostwriter that builds a "digital clone" (narrative timeline + tone-of-voice clone + long-term memory from edits): <https://www.ghostwriting-ai.com/>
- Buffer — best content format on social (45M+ posts analyzed): <https://buffer.com/resources/data-best-content-format-social-media/>
- LinkedIn hooks & formats 2026: <https://meet-lea.com/en/blog/linkedin-content-hooks-templates> · <https://medium.com/@alemeyer/linkedin-in-2026-formats-hooks-and-posting-cadence-3d279be9d71e>
- X/Twitter algorithm & growth 2026: <https://posteverywhere.ai/blog/how-the-x-twitter-algorithm-works> · <https://socialrails.com/blog/how-to-grow-on-twitter-x-complete-guide>
- Idea extraction/ranking from transcripts: Ranker-Generator for query-focused meeting summarization (arXiv 2305.12753); MUSERAG/MuseScorer idea-originality bucketing (arXiv 2505.16232)
- Style personalization: RAG few-shot vs fine-tune & hypothesis-driven alignment — HyPerAlign (arXiv 2505.00038); "Catch Me If You Can?" on imitating implicit styles (arXiv 2509.14543); Panza local writing assistant (arXiv 2407.10994)
