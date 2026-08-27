# 0007 — Generalized agentic task execution is excluded permanently, not deferred

**Status:** Decided

## Context
Raised in scoping: should the system, beyond writing calendar events, actually *do* actionable items for the user — pay a bill, complete an arbitrary task? This would be a strong "wow factor" demo moment.

## Decision
Out of scope permanently. Not a cut-order item, not a "v2 if time allows" — a deliberate, stated boundary. See PRD §2 and §15.

## Alternatives considered
- **Build one narrow generalized-execution path (e.g. bill pay) as a stretch goal.** Rejected: every action category is its own integration surface (auth, API, failure semantics) with nothing shared with the existing pipeline — unlike the email action (ADR 0008), which reuses the commit pipeline directly. Bill pay specifically is also the highest-stakes, least-reversible action category available; getting it wrong is a real-world consequence, not a demo bug.
- **Generic "computer use" / browser automation to complete arbitrary tasks.** Already rejected in PRD §2 as out of scope; this decision reaffirms why — it's an open research problem, not a 9-day scope item, and would bypass the narrow, typed commit pipeline that makes ADR 0003 enforceable.

## Consequences
- Keeps the write-boundary story (ADR 0003) uniform across every write the system makes: narrow integrations, typed commit messages, and one external writer.
- Stated explicitly in the README/write-up as a scope boundary rather than a gap — this is intended to read as maturity on the Architectural Discipline axis, not as a missing feature, per the rubric risk noted in PRD §3.
