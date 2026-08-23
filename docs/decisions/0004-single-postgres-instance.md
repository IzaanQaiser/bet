# 0004 — One Cloud SQL (Postgres + pgvector) instance, not AlloyDB or a separate vector DB

**Status:** Decided

## Context
Two concerns need a data store: relational state (items, conversations, suggestions, the state machine) and vector similarity (dedupe embeddings). AlloyDB, a managed vector DB (e.g. Vertex AI Vector Search), or a second Postgres instance are all plausible "more correct at scale" choices.

## Decision
A single Cloud SQL for PostgreSQL instance with the `pgvector` extension holds both concerns.

## Alternatives considered
- **AlloyDB.** Better performance/scaling characteristics for vector workloads, but a second thing to provision, IAM-bind, and operate correctly within 9 days, for a workload (single-user, low item volume) where the performance difference is irrelevant. Rejected for this build; not rejected as a real production choice.
- **A dedicated vector database.** Rejected: introduces a second system to keep in sync with the relational state (an item and its embedding must never disagree about existence), for no benefit at this scale — see ADR 0005 on why vector search itself is scoped narrowly.

## Consequences
- One connection pool, one backup story, one thing to provision in Terraform.
- This is explicitly a scale-appropriate choice, not a technology ignorance — worth stating that framing if a judge asks about it.
