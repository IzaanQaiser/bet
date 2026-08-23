# 0008 — Email draft+send reuses the existing commit pipeline

**Status:** Decided

## Context
Agreed as a stretch feature: for obligations that are themselves an email to send, the system should be able to draft and, on confirmation, send it — not just calendar the deadline.

## Decision
This is not a new subsystem. It's the existing Extractor → Resolver → confirm → `items.confirmed` → `committer-svc` pipeline, with `committer-svc` given a second write target (Gmail send) alongside the existing one (Calendar write), selected by an `action_type` column on `obligations` (see `docs/architecture/overview.md` schema note). The confirm-gate (ADR 0003) is identical to the calendar path — same state machine, same "no write without an explicit Y."

## Alternatives considered
- **A separate email-action service/pipeline.** Rejected: would duplicate the entire dedupe/clarify/confirm machinery for no reason — an email obligation is extracted, clarified, and confirmed exactly like a calendar obligation, it just terminates in a different API call.

## Consequences
- Near-zero marginal architecture cost: one new IAM scope on `committer-svc` (Gmail send), one new column, one new branch in the committer's write step.
- Sits in the cut order (PRD §2) above the hard floor and can be dropped without touching the core pipeline if time runs out.
