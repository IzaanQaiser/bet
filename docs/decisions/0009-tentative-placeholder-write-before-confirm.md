# 0009 — Tentative Calendar placeholders stay tagged, reversible, and single-writer

**Status:** Decided

## Context
User-directed feature: every committed idea should be pre-booked as a real, visibly-tagged `[idea] {title}` event on the user's calendar at its own next-fit slot, and texted at the exact instant that slot arrives — rather than the previous model (a scored, "at most one suggestion per run" text with nothing on the calendar until an explicit accept). The load-bearing question is not whether this has a separate confirmation step; v1 removed that pattern. The question is whether this widens Calendar write credentials beyond the single writer established in ADR [0003](0003-credential-scoping-by-iam.md).

## Decision
The invariant ADR 0003 protects — exactly one service ever calls the Calendar write API — is preserved exactly. What changes is scope, not the boundary itself:
- `committer-svc` remains the **only** service with Calendar write credentials. No other service's IAM binding changes.
- The placeholder write is narrowly scoped so it can never be mistaken for, or silently become, a real commitment: it is always tagged `[idea] {title}` and carries a fixed description ("Auto-scheduled — you'll get a text when it's time."), it is inert (no `obligations` row, does not affect any lifecycle other than its own latent's), and it is always either promoted in place on an accepted suggestion reply or deleted on every other outcome (decline, snooze, second dismissal, item cancellation).
- `dispatcher-svc` (which already owns every Calendar *read* and all `next_fit_start` computation) requests the write via a new **synchronous** call to committer-svc — `PUT`/`DELETE /latents/{item_id}/placeholder` — rather than the async publish-and-forget pattern the rest of the pipeline uses for confirmed writes. This is the second synchronous cross-service asymmetry in this codebase, after `ingest-svc`'s existing direct forward to resolver-svc/dispatcher-svc (`overview.md` §2).

## Alternatives considered
- **Give `dispatcher-svc` its own Calendar write scope.** Rejected: this duplicates a credential ADR 0003 specifically kept unique to one service, for no real benefit — dispatcher-svc still needs committer-svc's help for the *real* accept-time write regardless, so a second write-capable service would exist only for the placeholder, widening the blast radius of a dispatcher-svc compromise for no corresponding gain.
- **Publish the placeholder request over Pub/Sub instead of a synchronous call**, matching the rest of the pipeline. Rejected: dispatcher-svc needs the real Calendar event id back immediately, to persist into `latents.placeholder_event_id` before the same request completes (a later recompute needs that id to know whether to move or create). An async ack would need a second round trip to correlate back to that column write, which is more moving parts for no isolation benefit — the synchronous call is a request/response by nature, and Cloud Run's IAM-authenticated service-to-service call (the same mechanism `ingest-svc` already uses) is the direct fit.
- **Don't write anything until the suggestion is accepted**, keeping the prior scored-suggestion model. Rejected outright by explicit user direction — this ADR exists because the feature itself requires the placeholder to be visible at the next-fit slot before the user is texted, not after.

## Consequences
- ADR 0003's Consequences section gets a one-line pointer here; the single-writer boundary it describes is otherwise unchanged.
- `overview.md` §2's "one asymmetry" framing becomes two; §3's write-access matrix gets a footnote on dispatcher-svc's row, since it now *causes* a Calendar write indirectly for placeholders.
- New IAM: `sa-dispatcher` gets `roles/run.invoker` on `committer-svc` (the synchronous call) and `roles/cloudtasks.enqueuer` on the `reminders` queue (dispatcher-svc enqueues its own fire-time Cloud Task for the first time — previously only ever a Cloud Tasks target, never an enqueuer).
- A failure mode this ADR accepts: if committer-svc is unreachable, a placeholder write silently fails (logged, swallowed) and that one idea's `next_fit_start`/`placeholder_event_id` simply stay at their prior value until the next sweep retries — never fatal to anything else, never a stuck state.
