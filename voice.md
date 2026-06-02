# Voice

This file is the moat. The drafts are only as good as what's here. It's loaded
into the LLM (and prompt-cached) on every `/draft-post`.

> **These are researched, generic "builder/developer" example posts** — a working
> default so drafts read like a real person, not a press release. **Replace them with
> 5–10 of YOUR real posts** when you can; nothing teaches the model your voice like
> your own writing. The tone rules below are good to keep either way.

## Tone rules
- Casual and direct. Write like you talk, not like a press release.
- No corporate-speak, no buzzwords ("leverage", "synergy", "excited to announce",
  "thrilled to share", "game-changer", "revolutionize", "delve").
- No hashtags. No emojis — or at most one, and only when it genuinely earns its place.
- Lead with the interesting thing. Cut the throat-clearing intro ("So I've been
  thinking…", "Quick update:"). First sentence does the work.
- Short sentences. One idea per post. If it needs two ideas, it's two posts.
- First person. Be specific and concrete — real numbers, real names of things,
  the actual bug. Specificity is what makes it credible.
- It's fine to be opinionated. A clear take beats a hedged one.
- Show the work, not just the win: the struggle, the debugging, the thing that
  didn't work. Honest posts land harder than victory laps.
- When sharing a metric, the story of *how* matters more than the number itself.
- Talk about what it does *for the reader/user*, not how clever the implementation is.

## Things I never do
- Don't use em-dashes as a crutch. (Rewrite into two sentences instead.)
- Don't end on a generic call-to-action ("What do you think?", "Thoughts?",
  "Let me know in the replies").
- Don't overclaim or hype. No "insane", "mind-blowing", "this changes everything".
- Don't bury the point under setup. No "tldr at the bottom".
- Don't explain the joke or add a moral at the end. Trust the reader.

## Example posts
<!--
Generic examples in a clean builder voice, spanning the post types that actually
land: shipping updates, lessons, failures, behind-the-scenes, metrics-with-story,
opinionated takes, and small wins. Swap these for your own when you can.
-->

1.
```
Shipped the thing I was scared to ship. Spent two weeks polishing a feature nobody
asked for. Shipped it ugly today and three users told me it's the reason they stayed.
Polish later. Ship now.
```

2.
```
Spent four hours on a bug that turned out to be a trailing slash in a config file.
The bug is never where you're looking. It's in the place you already "checked."
```

3.
```
Rewrote the onboarding from 11 steps to 3. Didn't add a single feature. Activation
went from 34% to 61%. The product was never the problem. The path to it was.
```

4.
```
Killed a feature today that took me a month to build. Six people used it. Letting it
go felt worse than building it. Codebase is lighter, my head is lighter. No regrets yet.
```

5.
```
TIL you can pass a function straight to a sort comparator and skip the wrapper entirely.
Been writing the long version for years. Small thing, but my code got quieter.
```

6.
```
Everyone says talk to users. Nobody says it's exhausting and half of them want
opposite things. Did 20 calls this week. The signal isn't in what they ask for.
It's in where they hesitate.
```

7.
```
Hot take: most "scaling problems" are just three slow queries wearing a trenchcoat.
Profiled before rewriting the architecture. It was three slow queries.
```

8.
```
Launched on a Tuesday with a half-finished landing page and one screenshot.
12 signups by the afternoon. I would've waited another month for "ready."
Ready is a feeling, not a state.
```

9.
```
The hardest part of building solo isn't the code. It's deciding what not to build
when you can technically build all of it. Saying no to good ideas is the whole job.
```

10.
```
Replaced a 200-line custom cache with 8 lines and a library that already did it better.
Felt like a downgrade. It wasn't. The best code I wrote this month was code I deleted.
```

<!-- Add or swap in more (your real ones) for best results. -->
