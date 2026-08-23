# 0006 — Python across all services

**Status:** Decided

## Context
Five Cloud Run services need a language. Google ADK (the required Google Agent Framework) is Python-first, with the Node port less mature and Go effectively unsupported by ADK's agent abstractions.

## Decision
All five services (`ingest-svc`, `extractor-svc`, `resolver-svc`, `committer-svc`, `dispatcher-svc`) are Python. Full conventions in `docs/engineering/conventions.md`.

## Alternatives considered
- **TypeScript/Node.js.** Viable, ADK has a port, but less mature — more friction working against the framework rather than with it, in a 9-day build where every hour matters.
- **Go.** Best fit for Cloud Run itself and for the pipeline/state-machine services specifically, but ADK's agent abstractions aren't available — would mean hand-rolling Gemini calls, which weakens the "uses a Google Agent Framework" requirement from being a clean, idiomatic fit to a bolted-on technicality.

## Consequences
- One language, one dependency toolchain, one test runner across every service — reduces context-switching during a solo, time-boxed build.
- `ingest-svc` and `committer-svc` don't call an LLM at all and could in principle be any language; kept in Python anyway for consistency, not because they need ADK.
