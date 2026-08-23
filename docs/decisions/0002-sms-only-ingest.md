# 0002 — SMS is the sole ingest channel

**Status:** Decided

## Context
The BYOF friction (PRD §1) names four real channels obligations/ideas arrive through: email, text, screenshots, verbal. Building ingest for all four is a plausible-sounding "completeness" move.

## Decision
SMS/MMS (via Twilio) is the only ingest channel. It already covers three of the four channels in practice — a screenshot is an MMS, a verbal note becomes a voice memo or a typed text, a forwarded email can be screenshotted or forwarded to the number as text. True inbox-level email ingest (OAuth to Gmail, polling/webhooks, parsing arbitrary email structure) is not built.

## Alternatives considered
- **Native email ingest.** Rejected for this build: it's a second, structurally different ingest surface (different auth, different parsing, different spam/abuse surface) for a channel SMS already covers functionally via forwarding/screenshotting. Not worth the 9-day budget.
- **Slack/other chat ingest.** Rejected: not part of the stated friction, pure scope creep.

## Consequences
- One webhook, one auth flow, one conversation model. This is what makes `resolver-svc` able to own the entire clarification loop as synchronous SMS exchanges rather than juggling channel-specific reply mechanics.
- Framed explicitly as "one channel, done well" in the PRD (§2) — a strength to state on camera, not a gap to apologize for.
