# 0003 — Write-scope isolation and confirm-before-write enforced by IAM, not convention

**Status:** Decided

## Context
The system's core trust claim (PRD §5.2 non-negotiable: "never write on inference alone") could be implemented as a prompt instruction ("do not write to the calendar without confirmation") or as a code-level check inside a shared service. Both are conventions that a bug, a refactor, or a prompt injection could violate.

## Decision
Write-scope isolation is enforced structurally, at the infrastructure layer:
- `extractor-svc`'s service account has no Calendar scope, no Gmail scope, no DB write role — see the write-access matrix in `docs/architecture/overview.md` §3.
- Only `committer-svc` holds Calendar/Gmail write credentials, and it only ever consumes messages from `items.confirmed`, a topic only `resolver-svc` publishes to, only after parsing an explicit user affirmative.
- There is no code path — not a bug, not a bypassed check — from raw ingest to an external write that skips confirmation, because the service capable of writing externally is never in the call path for unconfirmed data.

## Alternatives considered
- **Application-level permission checks** ("if not confirmed, refuse to write") inside a single monolithic service. Rejected: this is a convention enforced by code review, not by the platform. A future change to that service could remove or bypass the check.
- **Prompt-level instruction only** ("never write without asking"). Rejected outright — LLM instructions are not a security boundary against injected content in photographed/forwarded input.

## Consequences
- This is the answer to "what stops a malicious photo from writing to my calendar": the service that reads the photo cannot write to the calendar, full stop, regardless of what the model outputs.
- Costs real infra setup — five service accounts with distinct IAM bindings instead of one shared identity. Accepted; this is exactly what `docs/architecture/infrastructure.md` will need to specify precisely.
