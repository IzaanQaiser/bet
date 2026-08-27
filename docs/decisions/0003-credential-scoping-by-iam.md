# 0003 — Write-scope isolation is enforced by IAM, not convention

**Status:** Decided

## Context
The system's core trust claim is that untrusted input and LLM output cannot directly call external write APIs. That could be implemented as a prompt instruction ("do not write to the calendar") or as a code-level check inside a shared service. Both are conventions that a bug, a refactor, or a prompt injection could violate.

Earlier versions also made an explicit user confirmation reply part of this ADR. V1 deliberately removed that product step: complete, non-duplicate items now commit automatically. The surviving architecture decision is the important one: where external write credentials live.

## Decision
Write-scope isolation is enforced structurally, at the infrastructure layer:
- `extractor-svc`'s service account has no Calendar scope, no Gmail scope, no DB write role — see the write-access matrix in `docs/architecture/overview.md` §3.
- Only `committer-svc` holds Calendar/Gmail write credentials.
- `resolver-svc` may publish to `items.confirmed` only after dedupe clears and required fields are present. `dispatcher-svc` may publish to the same topic only after classifying a suggestion reply as accepted.
- There is no code path from raw ingest or model output to Calendar/Gmail. The service capable of writing externally is reached only through typed pipeline messages or the narrow placeholder endpoints it owns.

## Alternatives considered
- **Application-level permission checks** ("if this caller should not write, refuse") inside a single monolithic service. Rejected: this is a convention enforced by code review, not by the platform. A future change to that service could remove or bypass the check.
- **Prompt-level instruction only** ("never write without asking"). Rejected outright — LLM instructions are not a security boundary against injected content in photographed/forwarded input.

## Consequences
- This is the answer to "what stops a malicious photo from writing to my calendar": the service that reads the photo cannot write to the calendar, full stop, regardless of what the model outputs.
- This is not a promise of an extra confirmation round trip. Complete items auto-commit by product design; incomplete or ambiguous items still clarify before they can become commit messages.
- Costs real infra setup — five service accounts with distinct IAM bindings instead of one shared identity. Accepted; this is exactly what `docs/architecture/infrastructure.md` will need to specify precisely.
- See ADR [0009](0009-tentative-placeholder-write-before-confirm.md) for tentative idea placeholders — the "exactly one service ever calls the Calendar write API" boundary this ADR establishes is preserved there too.
