# 0005 — Vector search is for dedupe-on-write only, never for resurfacing

**Status:** Decided

## Context
Once embeddings exist in the system (for dedupe), it's tempting to reuse them everywhere semantic matching could plausibly apply — including matching latent items to capacity snapshots, since "vector search" is the reflexive tool reached for whenever an LLM-adjacent system needs to match two things.

## Decision
Embeddings are used exactly once: at write time, to catch near-duplicate items with low lexical overlap (a screenshot of an email and a text about the same deadline). Resurfacing/capacity matching (PRD §6) is a SQL filter over structured columns (`effort_minutes`, `dismissal_count`, timestamps) against a computed `fit_score` — never a vector query.

## Alternatives considered
- **Embed capacity snapshots and match via similarity search.** Rejected: the dispatcher holds a *shape* — "150 contiguous minutes, deep focus, day is 40% lighter than average" — not a natural-language query. There is nothing semantic about matching an effort-minutes bucket to a contiguous-block duration; it's arithmetic, and arithmetic is more precise, cheaper, and more explainable in logs than a nearest-neighbor search over a synthetic snapshot embedding.

## Consequences
- The capacity engine is inspectable: every score in `docs/architecture/capacity-engine.md` will be a formula over named columns, reproducible by hand from a `capacity_snapshots` row. That's what makes the numbers on screen during the demo ("3h clear, lightest day in two weeks") verifiable rather than a black box.
- This is a subtlety worth stating explicitly in the write-up — it signals the vector column was a deliberate choice, not a "we used embeddings because it's an AI hackathon" reflex.
