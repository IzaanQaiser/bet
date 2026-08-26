# Architecture Decision Records

Short records of load-bearing decisions and why the alternative lost. These exist so a future session — yours or a coding agent's, working a narrow task under time pressure — doesn't quietly "improve" something that was actually a deliberate tradeoff.

If you're about to change one of these, read the Context/Alternatives section first. If the reasoning no longer holds, update the ADR rather than silently drifting from it.

**When to add a new one:** if you make a load-bearing tradeoff during implementation — something a future session (or a coding agent working a narrow task) could plausibly "fix" without knowing it was deliberate — write it down here as `000N-slug.md`, following the format of the existing ones (Context / Decision / Alternatives considered / Consequences). Don't let a real tradeoff live only in a commit message or a code comment; both get lost long before this file does. Routine implementation choices that follow directly from an architecture doc don't need one — this is for the decisions the docs don't already make for you.

| # | Decision |
|---|---|
| [0001](0001-no-orchestrator-agent.md) | No orchestrator agent — explicit state machine owns control flow |
| [0002](0002-sms-only-ingest.md) | SMS is the sole ingest channel |
| [0003](0003-credential-scoping-by-iam.md) | Confirm-before-write and write-scope isolation enforced by IAM, not by convention |
| [0004](0004-single-postgres-instance.md) | One Cloud SQL (Postgres + pgvector) instance, not AlloyDB or a separate vector DB |
| [0005](0005-vector-search-scope.md) | Vector search is for dedupe-on-write only, never for resurfacing/capacity matching |
| [0006](0006-python-runtime.md) | Python across all services |
| [0007](0007-no-generalized-execution.md) | Generalized agentic task execution is excluded permanently, not deferred |
| [0008](0008-email-action-reuses-pipeline.md) | Email draft+send reuses the existing commit pipeline instead of a new subsystem |
| [0009](0009-tentative-placeholder-write-before-confirm.md) | A tentative Calendar placeholder may be written before confirmation, narrowly — the single-writer boundary from 0003 stays intact |
