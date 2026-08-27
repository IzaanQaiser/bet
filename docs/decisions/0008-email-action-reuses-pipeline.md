# 0008 — Email draft+send reuses the existing commit pipeline

**Status:** Decided

## Context
Agreed as a stretch feature: for obligations that are themselves an email to send, the system should be able to draft and send it through the same commit machinery — not just calendar the deadline.

## Decision
This is not a new subsystem. It's the existing Extractor → Resolver → `items.confirmed` → `committer-svc` pipeline, with `committer-svc` given a second write target (Gmail send) alongside the existing one (Calendar write), selected by an `action_type` column on `obligations` (see `docs/architecture/data-model.md` §2's DDL). The commit gate is identical to the calendar path: dedupe must clear, required fields must be present, and only `committer-svc` can perform the external write.

**Resolved, step 15:** the drafting mechanism this ADR deliberately left unspecified is now designed — `docs/architecture/agent-contracts.md` §2.1 (extractor schema/prompt), §3.2/§3.5 (resolver staging/conversation), and `docs/architecture/state-machine.md` §1.5 (the commit mechanics and idempotency). No new subsystem, no new state — this ADR's original decision held exactly as scoped.

## Alternatives considered
- **A separate email-action service/pipeline.** Rejected: would duplicate the entire dedupe/clarify/commit machinery for no reason — an email obligation is extracted and clarified exactly like a calendar obligation, it just terminates in a different API call.

## Consequences
- Near-zero marginal architecture cost: one new IAM scope on `committer-svc` (Gmail send), one new column, one new branch in the committer's write step.
- Sits in the cut order (PRD §2) above the hard floor and can be dropped without touching the core pipeline if time runs out.
