# Project Instructions

Product requirements live in docs/product/prd.md — read it at the start of every session, it is the source of truth for scope, non-negotiables, and build order.

Current build progress lives in docs/product/status.md — read it right after the PRD to know which build-order step is active without inferring it from git log. Update it whenever a step completes, starts, or gets blocked; keep it short.

Architecture is being documented incrementally in docs/architecture/, one document at a time. Current state:
- docs/architecture/overview.md — service topology, write-access boundaries, data flow (done)
- docs/architecture/state-machine.md — item and latent lifecycle, transition rules, failure handling (done)
- docs/architecture/data-model.md — schema, indexes, migration strategy (done)
- docs/architecture/capacity-engine.md — snapshot computation and scoring, worked examples (done)
- docs/architecture/agent-contracts.md — exact I/O schemas and prompts for Extractor, Resolver, Dispatcher (done)
- docs/architecture/infrastructure.md — GCP resource inventory, IAM bindings, IaC structure (done)

The architecture doc set is complete. Work moving forward is implementation, governed by docs/engineering/conventions.md.

Do not invent architectural decisions that are not documented. If a sibling architecture doc doesn't exist yet, flag the gap rather than filling it in ad hoc.

Load-bearing decisions and their rationale are recorded in docs/decisions/ (ADRs) — read the relevant one before changing course on something it covers (e.g. don't reach for an orchestrator agent, a second data store, or vector search for matching without reading why those were rejected).

Engineering conventions (language, repo layout, testing, migrations, local dev) live in docs/engineering/conventions.md. Per-step acceptance criteria and named unit/integration/manual tests live in docs/engineering/test-plan.md — read only the section for the step you're on.

Before implementing a subsystem, read the relevant architecture documentation.

Prefer simple, explicit, testable designs over speculative abstraction.

## Commit messages

Conventional Commits: `<type>(<scope>): <summary>`.

- Types: `feat`, `fix`, `refactor`, `test`, `chore`, `docs`.
- Scope: a service name (`ingest-svc`, `extractor-svc`, `resolver-svc`, `committer-svc`, `dispatcher-svc`), `shared`, `infra`, or `docs` for documentation-only changes. Omit the scope for something genuinely repo-wide.
- Summary: imperative mood, no trailing period, under ~70 characters.
- Body (optional, blank line after the summary): explain *why*, not *what* — the diff already shows what changed. Use it for the reasoning a future session would otherwise have to reconstruct (a bug that motivated the fix, a tradeoff that was considered and rejected).
- Only create commits when asked to.

This matches the pattern already used across the `chore(docs): ...` commits in this repo's history — keep using it once implementation starts (`feat(extractor-svc): ...`, `fix(resolver-svc): ...`, etc.).