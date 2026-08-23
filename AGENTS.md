# Project Instructions

Product requirements live in docs/product/prd.md — read it at the start of every session, it is the source of truth for scope, non-negotiables, and build order.

Architecture is being documented incrementally in docs/architecture/, one document at a time. Current state:
- docs/architecture/overview.md — service topology, write-access boundaries, data flow (done)
- docs/architecture/state-machine.md — item and latent lifecycle, transition rules, failure handling (done)
- docs/architecture/data-model.md — schema, indexes, migration strategy (done)
- docs/architecture/capacity-engine.md — snapshot computation and scoring, worked examples (done)
- docs/architecture/agent-contracts.md — exact I/O schemas and prompts for Extractor, Resolver, Dispatcher (done)
- infrastructure.md — not yet written

Do not invent architectural decisions that are not documented. If a sibling architecture doc doesn't exist yet, flag the gap rather than filling it in ad hoc.

Load-bearing decisions and their rationale are recorded in docs/decisions/ (ADRs) — read the relevant one before changing course on something it covers (e.g. don't reach for an orchestrator agent, a second data store, or vector search for matching without reading why those were rejected).

Engineering conventions (language, repo layout, testing, migrations, local dev) live in docs/engineering/conventions.md.

Before implementing a subsystem, read the relevant architecture documentation.

Prefer simple, explicit, testable designs over speculative abstraction.