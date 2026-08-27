# Agent Contracts

Fifth doc in the architecture set — see `overview.md` §0. This closes out every "exact schema / exact rendering" item deferred by `overview.md`, `state-machine.md`, `data-model.md`, and `capacity-engine.md`. Pydantic models shown here are the spec; `shared/obligation_engine_shared/schemas.py` (`docs/engineering/conventions.md`) is the literal code — if they ever disagree, the code is buggy, not this doc.

---

## 0. Where Gemini is actually called

**Superseded (see below): `overview.md`'s diagram originally labeled `dispatcher-svc` `(Cloud Run, ADK)`, was corrected to drop it, and has now been restored** — dispatcher-svc gained its own Gemini call after all, described below. Every *number* it produces is still pure arithmetic (`capacity-engine.md`) and always will be; only the fire-time suggestion text and the reply-intent classification are LLM-driven now.

That's three LLM call sites in the whole system:

| Call site | Service | Purpose | Structured output |
|---|---|---|---|
| Extraction | `extractor-svc` | Raw input → structured item, plus a chat/actionable triage flag (Phase G step B) | Yes, strict schema |
| Conversation | `resolver-svc` | Merge a reply into missing fields, classify intent (`AFFIRM`/`DENY`/`OTHER`), and write the actual outbound SMS in the user's own mirrored voice (Phase G step D, `resolver_svc/conversation.py`) | Yes, strict schema |
| Suggestion | `dispatcher-svc` | Write the fire-time nudge, and classify a reply to it as `ACCEPT`/`DECLINE`/`SNOOZE`/`OTHER` (`dispatcher_svc/conversation.py`, §4.2 below) | Yes, strict schema |

Everything else user-facing — the dedupe question and its terminal messages (`MERGED`, `NEEDS_REVIEW`), and dispatcher-svc's own accept/decline/snooze acknowledgments (`render_accepted`/`render_deferred`/`render_dismissed`/`render_snoozed`) — is still a deterministic Python template (each service's own `templates.py`). Those acknowledgments stay templated deliberately, not an oversight: they state real, just-computed facts (the actual committed time, the actual rescheduled day) that must never be an LLM guess, the same ADR 0003 boundary the conversation call site's own locked-in message already respects.

**Why dispatcher-svc has an LLM call now, when this section used to say the opposite:** the original reasoning ("would only introduce unpredictability into the one output that most needs to be exactly reproducible") predates Phase G's own resolver-svc precedent, which crossed the same bridge for conversational output and answered the concern with `thinking_config.thinking_budget=0` + empirical verification against real Vertex AI, not by avoiding the LLM. This was always the tracked Phase G follow-up ("dispatcher-svc suggestion flow gets the same treatment," `docs/product/status.md`), landing here — not a new, undocumented decision. ADR 0001 is unaffected either way: this call still never decides control flow, it only supplies a nudge sentence and a classified `intent` field that dispatcher-svc's own Python state machine branches on, the identical shape resolver-svc's own `intent` field already uses.

All three call sites use Google ADK with a Gemini 3.5 Flash model and a JSON response schema (ADK's structured-output mode) — no free-text parsing of a model response anywhere in this system. All three also explicitly set `thinking_config.thinking_budget=0` — a real-latency fix, not a default: left unset, the model's `AUTOMATIC` thinking budget spent real, highly variable time deliberating (~6.3s avg, up to 9.7s, in `resolver-svc`'s case) before producing output, for tasks that are fundamentally rule-based classification against an explicit prompt, not open-ended reasoning. Verified against real Vertex AI before shipping: disabling it cut resolver-svc's call to ~2-2.5s average with zero classification regressions across its full scenario suite (docs/product/status.md has the counts); dispatcher-svc's own call reuses the identical mitigation for the same reason, verified the same way before shipping.

**Phase G note (docs/product/status.md):** the real safety property here was never "the LLM's surface area stays narrow" — it's ADR 0001 (an explicit state machine, not the LLM, decides what happens next) and ADR 0003 (only `committer-svc` holds Calendar/Gmail write creds, and LLM call sites never receive write tools). That means the LLM's output can become more conversational without weakening either guarantee, as long as pipeline code still owns the branch decisions. `resolver-svc` is the only forward-path publisher of `items.confirmed`, and it publishes when dedupe clears and required fields are present; `dispatcher-svc` can publish only on a suggestion reply classified as accepted.

---

## 1. Pub/Sub message schemas

Per `overview.md` §2/§4. All three carry the **full payload** needed by the next stage — no service reaches back into the DB to re-fetch what the previous stage already knew, since `extractor-svc` in particular has no DB access at all (ADR 0003).

```python
# items.raw — published by ingest-svc
class RawItemMessage(BaseModel):
    item_id: UUID
    user_id: UUID
    media_uri: str | None        # GCS URI, null if text-only
    mime_type: str | None        # null if text-only
    text: str | None             # the SMS body, if any
    received_at: datetime

# items.extracted — published by extractor-svc
class ExtractedItemMessage(BaseModel):
    item_id: UUID
    user_id: UUID
    type: Literal["obligation", "latent"]
    title: str
    summary: str
    due_at: datetime | None
    effort_minutes: Literal[15, 30, 60, 120, 240]
    confidence: float                 # 0.0-1.0
    missing_fields: list[str]
    reasoning: str                    # log-only, never shown to the user
    action_type: Literal["calendar", "email"] = "calendar"   # step 15, §2.1
    email_recipient: str | None = None    # a real address, never a guessed name — §2.1
    email_draft: str | None = None        # set only when action_type == "email"

# items.confirmed — published by resolver-svc (normal complete-item path) or
#                    dispatcher-svc (accepted-suggestion path, state-machine.md §2.3)
class ConfirmedItemMessage(BaseModel):
    item_id: UUID
    user_id: UUID
    type: Literal["obligation", "latent"]   # a surfaced latent arrives here already flipped to "obligation"
    title: str
    summary: str
    due_at: datetime | None                 # required for a calendar obligation; may be null for an email one (§2.1); always null for a latent
    effort_minutes: Literal[15, 30, 60, 120, 240]
    action_type: Literal["calendar", "email"] | None   # null for a latent; "calendar" for a dispatcher-accepted latent (§1.5's scope note)
    email_recipient: str | None              # step 15 — carries a resolved recipient one hop from conversations.resolved_fields to committer-svc (§3.2); null unless action_type == "email"
    email_draft: str | None                 # null unless action_type == "email" — see §2.1
```

**Resolved bug:** `due_at` was originally typed as required (non-null) — wrong, since a latent legitimately has no due date and this message type also carries latents through the normal commit path. `committer-svc` branches on `type`: for `"obligation"` it writes Calendar (and Gmail if `action_type == "email"`) and `INSERT`s into `obligations`; for `"latent"` it does no external write at all and just `INSERT`s into `latents`.

`ExtractedItemMessage` carries the *entire* extraction result, not just `item_id` — `extractor-svc` has no DB write role (ADR 0003), so `resolver-svc` is the first service to actually persist these fields into `items`. `ConfirmedItemMessage` likewise carries its full payload rather than just `item_id` — `resolver-svc` could technically let `committer-svc` re-`SELECT` from `items`, but `due_at` has no `items` column to `SELECT` from in the first place (see `data-model.md` §2.4), so the full-payload shape stays uniform across all three messages by necessity, not just convention.

**Resolved gap, found in step 6 — the Calendar event's time span.** Neither the PRD nor this doc specified how long the written Calendar event actually runs for; only that a `due_at` instant exists. Decided here: the event spans `due_at` to `due_at + effort_minutes` — the calendar block represents the time set aside to actually do the task, consistent with the capacity engine treating obligations as real occupied time (`capacity-engine.md` §2). `due_at` may arrive from Gemini as a naive datetime (no UTC offset) when the model reasons in local terms; `committer-svc` treats a naive `due_at` as already being in the user's `users.timezone` (attaching that zone, not converting) before sending `dateTime`+`timeZone` to the Calendar API — never assumes UTC for a naive value, which would silently shift the event by however many hours off UTC the user's zone is.

---

## 2. Extractor contract

**Input:** media bytes (if any) + MIME type + SMS text, from `RawItemMessage`.
**Output:** `ExtractedItemMessage` fields (minus `item_id`/`user_id`, which the service adds).

**Resolved gap, found in step 4's real implementation:** Gemini's structured-output schema (via ADK's `output_schema`) only supports **string** enum values, not integer ones — `Literal[15, 30, 60, 120, 240]` fails Vertex AI's schema validation outright. The wire schema `extractor-svc` actually gives the model uses `Literal["15", "30", "60", "120", "240"]` (strings); the service casts to `int` when constructing the real `ExtractedItemMessage`. This is purely a wire-format detail — `ExtractedItemMessage` itself is unchanged, still `int`.

**Resolved gap: the real Vertex AI model resource name and location.** `infrastructure.md` §3 originally left this as "confirm at build time" — confirmed now: `gemini-3.5-flash` is **not** available via any regional endpoint (`us-central1`, `us-east5`, etc. all 404) — it's only served via the **global** endpoint. `VERTEX_LOCATION` must be `global`, not a region, or every extraction call 404s. See `infrastructure.md` §3 for the corrected env var.

**System prompt:**
```
You are the extraction stage of a personal obligation-tracking system. You
are given a message a user sent via SMS — text, and optionally an attached
image or PDF (a screenshot, a scanned letter, a photo of a note). Extract
exactly one structured item from it.

Rules:
- Classify as "obligation" if it has or implies a deadline; otherwise
  "latent" (an idea, a project, an intention with no deadline).
- Never invent a due_at. If a date is implied but ambiguous ("next week",
  "soon"), leave due_at null and add "due_at" to missing_fields — do not
  guess a specific date.
- effort_minutes must be exactly one of 15, 30, 60, 120, 240 — pick the
  closest realistic bucket. Never output any other number.
- confidence reflects your overall certainty about the classification and
  fields, not any single field in isolation.
- reasoning is one sentence explaining the classification, for logs only —
  it is never shown to the user.

Output must conform exactly to the provided schema. No text outside it.
```

If a message plausibly contains **more than one item** (e.g. a screenshot with two separate deadlines), the extractor still emits exactly one — the most salient one, by its own judgment — and folds the rest into `summary` as context. Splitting one message into multiple items is out of scope; noted here so it isn't rediscovered as a bug later, it's a boundary.

The system prompt shown above predates Phase G step B (below) — the live prompt in `extractor-svc/main.py` also carries the `is_actionable`/`chat_reply` triage instructions; this doc's code block isn't kept byte-for-byte in sync with every prompt edit, `main.py` is the source of truth per this doc's own header.

### 2.1 Email action — resolved, step 15 (ADR 0008)

This resolves the "Open gap, flagged rather than invented" note this doc used to carry in §3.2 (kept below, struck through in spirit — see the note there for what was deliberately *not* decided before now).

**Resolved gap: how `action_type`/`email_draft`/`email_recipient` ever get set.** Extended in the *same* extraction call, not a third LLM call site — §0's "exactly two call sites" stays true. `ExtractedItemMessage` gains three fields:

```python
action_type: Literal["calendar", "email"] = "calendar"
email_recipient: str | None = None   # a real address, never a name — see below
email_draft: str | None = None       # only set when action_type == "email"
```

**System prompt addition:**
```
- action_type is "email" only if the message is unambiguously asking to send
  an email (e.g. "email X about...", "send Sarah an email saying...") AND
  the message itself contains a literal, syntactically valid email address
  for the recipient. Otherwise action_type is "calendar" — this covers
  every non-email obligation, which is most of them.
- If the message is clearly email-intent but no valid address is present
  (e.g. "email Sarah about the delay" — a name, not an address), still set
  action_type to "email", leave email_recipient null, and add
  "email_recipient" to missing_fields. Never guess an address from a name —
  there is no contacts lookup in this system, and guessing an address risks
  sending to the wrong person, the one failure mode worse than not sending
  at all.
- Whenever action_type is "email", classify type as "obligation" even if no
  deadline is present or implied — sending a message is an immediate action
  someone asked for, not a someday idea, so it never becomes a latent
  regardless of the usual obligation/latent deadline rule. If the message
  implies no deadline at all, leave due_at null and do NOT add "due_at" to
  missing_fields — there is nothing to ask about. Only add "due_at" to
  missing_fields for an email action if a date is implied but genuinely
  ambiguous, same as any other obligation.
- When action_type is "email", draft email_draft: a complete, sendable email
  body in the user's own voice, based on what the message says — a greeting,
  the substance, a sign-off. Keep it concise. Never draft a body for
  action_type "calendar".
```

**Real finding, verified in a scratch test against real Vertex AI before deploying this:** the first draft of the due_at rule above didn't explicitly override the general "an obligation implies a deadline" assumption baked into every other rule — Gemini kept adding `due_at` to `missing_fields` for a fully-specified email action with a real recipient and no deadline mentioned at all ("email sarah@example.com and tell her the delivery will be late" → `missing_fields=["due_at"]`, wrongly triggering a clarifying question about a date nobody implied). The explicit "if the message implies no deadline at all... do NOT add due_at to missing_fields" sentence above fixed it, reconfirmed on two more real calls: the same complete case now correctly returns `missing_fields=[]`, and a name-only case ("email Sarah about the delay", no real address) correctly returns `missing_fields=["email_recipient"]` alone, not also `due_at`. The ambiguous-date case wasn't separately re-verified after this fix (a later scratch call hung on a real Vertex AI quota limit and was killed) — not treated as a gap, since that branch just falls through to the pre-existing, already-proven "ambiguous date → missing_fields" rule (verified for real back in step 4) rather than adding new logic of its own.

**Why no recipient lookup/contacts feature:** out of scope for a hackathon stretch, and more importantly a real safety boundary — this system already refuses to guess a `due_at`; guessing an email recipient from a bare name is the same category of mistake with a worse failure mode (a real message sent to a real stranger, not just a wrong calendar time). The user must include the actual address in their text.

**Why `missing_fields` and not a hard rejection:** matches the existing precedent exactly — `due_at` already uses `missing_fields` + the clarification loop rather than rejecting an incomplete obligation outright. `email_recipient` is now the second field this mechanism handles (§3.2 below).

**Resolved gap: does an email action need a `due_at` at all?** No — unlike a calendar obligation, where `due_at` is the entire point (it's what gets scheduled), an email action's `due_at` (if any) only describes context *inside* the draft, not a send time — `committer-svc` sends synchronously when the item commits, so there's no "scheduled for later" concept to support. `due_at` stays legitimately nullable for `action_type == "email"`; the completeness check (`state-machine.md` §1.2) only ever required it because `missing_fields` said so, and the extractor now simply doesn't add `due_at` to `missing_fields` when there's genuinely no deadline to ask about.

### 2.2 Chat detection — Phase G step B

Before this step, every message was forced into `type="obligation"` or `type="latent"` — a plain "hello" produced a fake obligation ("Respond to greeting") and a nonsensical commit prompt. `ExtractedItemMessage` gains a leading triage flag, extended in the same extraction call:

```python
is_actionable: bool = True
chat_reply: str | None = None   # set only when is_actionable is False
# type/title/summary/due_at/effort_minutes/confidence are now
# Optional — null when is_actionable is False, since there's nothing to
# extract; missing_fields defaults to an empty list.
```

**System prompt addition:** decide `is_actionable` first. `false` for pure chat (banter/greeting/reaction/question, nothing to remember or schedule) — leave every extraction field null and write `chat_reply`: a short, casual, in-voice reply reacting to what the user actually said. `true` for a real obligation or idea — leave `chat_reply` null and fill every field exactly as before this step.

**Verified empirically against real Vertex AI before writing any production code** (this project's established pattern — see §2's earlier "Resolved gap" notes, both found the same way): a bool field plus a mix of required/now-nullable fields plus a new string field, all in the same schema the extraction call already uses, validated correctly on the first attempt — no repeat of §3.2's `dict[str, Any]` failure, since this changes field *optionality*, not an open-key/`Any`-typed shape. Six real calls confirmed correct classification: three chat-only messages ("hello", "yo wsg bro", "you there?") each got `is_actionable=false` and a natural, distinct in-voice reply; three actionable messages (a dated obligation, an ambiguous-deadline obligation, a no-deadline latent) each got `is_actionable=true` with the full extraction unchanged from pre-step-B behavior.

**Where the reply actually gets sent:** `extractor-svc` only *generates* `chat_reply` — it still has zero Twilio/DB access (ADR 0003). `resolver-svc`'s `/pubsub/push` handler short-circuits on `is_actionable=False` before the dedupe check, sends `chat_reply` via its existing `_send_sms`, and writes the item straight to a new terminal state, `CHATTED` (`items_state_check` migration `0006`) — not `CANCELLED` (implies a rejected real candidate) or `NEEDS_REVIEW` (implies an incomplete obligation), matching `state-machine.md`'s existing refusal to fold different failure/outcome semantics into one state. A `conversations` row is still written (empty `resolved_fields`) purely to satisfy the existing redelivery-idempotency guard (`data-model.md` §2.4/§2.7's "conversations row = the one true completion signal"), not because a chat item has any back-and-forth to track.

**Verified for real** via a signed webhook straight to the deployed `ingest-svc`: a real "hello" produced a real `is_actionable=false` extraction, a real "hey! what's up?" SMS delivered in ~11s (down from the original ~2 minute, fake-obligation-card response), and `items.state='CHATTED'` with `type`/`title` left null. A follow-up real obligation message in the same session confirmed the actionable path still works.

---

## 3. Resolver contract

**Phase G step C note:** every inbound and outbound SMS is now durably logged in `messages` (`migrations/0007_messages_table.sql`, `direction`/`body`/`created_at`, `obligation_engine_shared.log_message()`) — `ingest-svc` logs inbound, `resolver-svc`/`dispatcher-svc` log their own outbound sends. This is the tone-mirroring context §3.5 below reads.

**Phase G step D landed — §3.2 below is history, not current behavior.** It keeps only the useful implementation lessons from the retired due-at-only clarification call, fixed templates, and strict keyword matching. Read §3.5 for what's actually deployed.

**§3.1 below is also history now, not current behavior — a same-session follow-up to step D.** Step D deliberately left the dedupe question alone; that was never "this is fine forever," just "not what that redesign was about." A user hit the fixed Y/N script live against the deployed demo and objected to it directly. §3.5 now covers the dedupe question too.

### 3.1 Dedupe question — history (superseded, see §3.5)

Per `state-machine.md` §1.1. Was a fixed template, filled from the existing matched item's `title`:
```
Is this the same as "{existing_title}"?
Reply Y to merge, N if it's different.
```
Kept here because the underlying dedupe *mechanism* below (hash/embedding matching, the resolved gaps found building it) is unaffected — only the question's own wording and reply classification moved into `converse()` (§3.5).

**Resolved gap, found in step 12's real testing: `text-embedding-004`'s location behavior differs from `gemini-3.5-flash`'s.** Step 4 found Gemini 3.5 Flash 404s on every regional Vertex AI endpoint and only works via `global`. Verified empirically before writing `resolver-svc/dedupe.py` (not assumed to be the same): `text-embedding-004` works identically at both `us-central1` and `global`, returns exactly 768 dimensions matching `item_embeddings.embedding`'s column type, and produces sane cosine similarities on a real call (a close paraphrase of the same fact scored 0.92, clearly separated from an unrelated sentence at 0.29) — no new env var needed, it reuses whichever `GOOGLE_CLOUD_LOCATION` `resolver-svc` is already deployed with.

**Implementation note:** no `pgvector` Python package/adapter is used — `dedupe.py`'s `vector_literal()` formats the embedding as pgvector's plain text literal (`[v1,v2,...]`) and the SQL casts it with `::vector` server-side. A write-only, single-column use like this doesn't need the adapter package's registration machinery.

### 3.2 Retired resolver templates — historical note

The older resolver design had a separate clarification call, deterministic confirmation cards, and a shared strict keyword classifier. That machinery is retired for the resolver path; §3.5 is the current behavior.

The useful lessons that remain:
- Vertex AI structured output did not reliably fill open `dict[str, Any]` fields, so the current schema stays concrete.
- Only `due_at` and `email_recipient` are clarifiable fields today. Missing values are asked for; never guessed.
- Values that do not live on `items` (`due_at`, `email_recipient`, `email_draft`, `action_type`) stage in `conversations.resolved_fields` until `ConfirmedItemMessage` carries them to `committer-svc`.
- `render_needs_review` remains a deterministic terminal template for exchange exhaustion. Dedupe and normal resolver replies are now generated by `converse()`.

### 3.5 The unified conversation call — current Resolver behavior

Replaces §3.2's old narrow clarification loop, the deleted fixed commit prompts, and the strict shared keyword classifier for the resolver path. One Gemini call, `resolver_svc/conversation.py`'s `converse()`, is used to merge replies into missing fields, ask natural follow-up questions, generate the final "locked in" acknowledgment once complete, and handle the dedupe yes/no turn in the same voice.

```python
class ConversationTurnResult(BaseModel):
    relates_to_item: bool = True       # false = this reply is about something else entirely
    due_at_filled: bool = False
    due_at: str | None = None          # naive local ISO 8601, no UTC offset
    email_recipient_filled: bool = False
    email_recipient: str | None = None
    still_missing: list[str] = []      # subset of {"due_at", "email_recipient"} only
    intent: Literal["AFFIRM", "DENY", "OTHER"] | None = None
    reply_text: str                    # the actual outbound SMS, in the user's mirrored voice
```

**Inputs:** the item (title/type/summary/effort/known fields), which of `due_at`/`email_recipient` are still missing, a pending dedupe candidate when applicable, the user's last 10 messages (`_recent_history`, §above) as a live style reference, and the latest reply text (null on the first turn).

**Verified empirically against real Vertex AI before writing this module**, per this project's established pattern: a schema mixing a concrete field-merge output, a narrow `Literal` intent classification, and a free-text voice-matched field — a combination never tried before (§3.2's `dict[str, Any]` rejection was about a different failure mode, an open-key/`Any`-typed dict) — validated correctly before production wiring.

**Real bug, found during the required live verification, not by a test:** the model once added `"title"` to `still_missing` on its own initiative — a field name outside the only two the pipeline has merge logic for. Fixed both in the prompt (explicit "only these two names, ever") and defensively in `converse()` itself, which filters `still_missing` to `{"due_at", "email_recipient"}` regardless of what the model returns — the prompt fix alone wasn't trusted as sufficient.

**Pipeline code, not the model, still decides every transition (ADR 0001/0003, unchanged):**
- `still_missing` non-empty → stays `CLARIFYING` (or starts there), sends `reply_text` as the next question, `exchange_count` capped at 3 exactly as before (§3.2's original design, unchanged) — exhaustion still falls through to the templated `NEEDS_REVIEW` message above, not an LLM-generated one.
- `still_missing` empty → `resolver-svc` publishes `items.confirmed` immediately, flips `items.state` to `CONFIRMED`, and sends `reply_text` as a natural acknowledgment. There is no separate `AWAITING_CONFIRMATION` turn and no affirmative reply requirement.
- During `DUPLICATE_SUSPECTED`, `intent == "AFFIRM"` means merge; `intent == "DENY"` means treat the item as distinct and resume the normal completeness check; `OTHER` sends `reply_text` as a natural re-ask. This is the only resolver path where `intent` is meaningful now.

**Current v1 behavior:** a complete first extraction auto-commits; a clarification reply that fills the last required field auto-commits; a duplicate negative reply falls through to the same auto-commit path if the item is otherwise complete. This is the user-directed replacement for the old confirmation-card/Y/N round trip.

**`relates_to_item` (Phase G follow-up, same session as step D) — the conversation-continuity fix.** `ingest-svc` routes any inbound SMS to whichever item a user has open purely by *state* (`DUPLICATE_SUSPECTED`/`CLARIFYING`, plus legacy `AWAITING_CONFIRMATION` rows if any still exist) — it has no way to know whether the message's *content* has anything to do with that item. Before this field existed, every reply while an item was open got force-fed to `converse()` as if it must be about that item; found as a real bug live-testing step D (a stuck test item was absorbing unrelated follow-ups). Deliberately not fixed with a timeout: SMS threads persist on the user's own screen, so a reply an hour or a day later is often still genuinely about the same item — only the reply's *content* can tell related from unrelated, never elapsed time. When the model sets `relates_to_item` false (default true; forced true whenever there's no reply yet to judge, i.e. the first turn), every other field is left at its default and `reply_text` is empty — `resolver-svc`'s `_route_as_new_item()` leaves the open item **completely untouched** (no state change, nothing lost, still there waiting for whenever the user comes back to it) and spins up a **brand-new item** for the text via a new shared `create_raw_item()` helper (`obligation_engine_shared/db.py`) + an `items-raw` publish — the exact same path a first-contact message takes, so it gets real extraction/dedupe/clarification, not a stub reply. Wired into the CLARIFYING and dedupe reply handlers.

Verified against real Vertex AI before wiring in (same pattern as above): 9/9 real scenarios classified correctly, including a vague-but-related reply ("hmm not sure") correctly staying `True`, and three genuinely unrelated replies (a new obligation, idle chat, an off-topic question) all correctly landing `False`. Two real infra gaps surfaced only by live-testing the actual deploy, neither a code bug: `sa-resolver` had never needed `INSERT` on `items` before (fixed, `migrations/0008_grant_resolver_items_insert.sql`) or `pubsub.publisher` on `items-raw` (fixed, `infra/pubsub.tf`'s `resolver_publishes_raw` Terraform resource) — both 500'd on the first two live attempts, both invisible to any existing test since every test mocks the DB connection and `publish()` directly. Deployed and verified for real via live-SMS flows covering clarification and dedupe, each confirmed against real DB state — the open item untouched, the unrelated text landing as its own independent item, and a dormant item correctly resumable once the newer one resolved.

**Dedupe question folded in too (same-session follow-up, direct user report against the live demo).** §3.1's fixed `Is this the same as "X"?\nReply Y to merge, N if it's different.` script was step D's one deliberate exception — a user hit it live and objected forcefully. Two new inputs join the schema's context: `dedupe_candidate_title: str | None` and `awaiting_dedupe_reply: bool`. A new prompt step, checked before the normal field-merging steps: on the very first turn (`dedupe_candidate_title` set, no reply yet), `reply_text` is a natural question asking whether this is the same as the named existing item — no fixed format. When `awaiting_dedupe_reply` is true, `AFFIRM`/`DENY` are repurposed for this one turn to mean "yes, merge" / "no, keep separate." A `DENY` falls through to the pre-existing second `converse()` call (unchanged) that resumes the normal completeness/commit flow for the now-independent item. `relates_to_item` applies here too — a reply that's neither an answer to the dedupe question nor a duplicate's actual content still gets routed to its own new item via `_route_as_new_item()`, same mechanism as above.

Verified against real Vertex AI before wiring in: 7/7 scenarios correct — the initial natural question, AFFIRM/DENY via both natural phrasing and plain "y"/"n", a genuinely ambiguous reply landing `OTHER` with a real clarifying question, and an unrelated reply correctly landing `relates_to_item=False`. `templates.py`'s `render_dedupe_question`/`render_merged` and `resolver-svc`'s import of `reply_classifier.classify_reply` are gone — `render_needs_review` is the only resolver template left, a deliberate, unrelated exhaustion terminal message. Deployed and verified for real, reproducing the exact reported scenario: created and committed a "Renew passport" obligation, sent a near-duplicate, got a real natural dedupe question with no script, replied "yeah same one" (not "Y") and got merged with a natural acknowledgment; separately, replying "nah this is for my kid, different passport" to a second duplicate correctly acknowledged the distinction *and* naturally continued into asking for the still-missing due date in the same message.

---

## 4. Dispatcher contract

### 4.1 Deadline reminder — one stage, at the time-of

```
⏰ last call, {title} is due {relative_due_description}, {formatted_due_at}.
```
```
⏰ {title} is starting now, {formatted_due_at}.
```
(the second form for `is_scheduled_event` obligations — worded as starting, not due). `{relative_due_description}` is `"today"`, `"tomorrow"`, or `"in {N} days"`, computed from `obligations.due_at`, not the LLM.

**V1 simplification, user-directed (this section previously described an effort-derived two-stage design — migrations/0013's `reminder_1_at`/`reminder_2_at` — that a still-earlier same-session pass had already flattened to a fixed 30-minute-before/at-due pair before this doc was ever updated to match; this pass removes the earlier stage entirely rather than just correcting the drift):** exactly one SMS reminder now, fired by dispatcher-svc at `obligations.reminder_at`, which resolver-svc sets to `due_at` itself when publishing `items.confirmed` (`resolver_svc/main.py::_compute_reminder_time`) — no offset math left in the SMS pipeline at all. The 30-minute-before lead a user might still want lives only in the real Calendar event's own native popup reminder now, set explicitly at creation time by committer-svc (`CALENDAR_REMINDER_OVERRIDE = {"useDefault": False, "overrides": [{"method": "popup", "minutes": 30}]}`, applied to both a real obligation event and an idea's placeholder event, replacing what used to be an implicit `useDefault`) — a Calendar-side notification, not a second text. `migrations/0022` drops `reminder_1_at`/`reminder_1_sent_at` and renames `reminder_2_at`/`reminder_2_sent_at` to `reminder_at`/`reminder_sent_at`, fired by dispatcher-svc against the same `reminder_sent_at IS NULL` idempotency shape as before.

Since the resolver acknowledgment already states the due/start time in the same breath ("it's due at 6pm" / "it starts at 3pm"), and the one remaining reminder always fires at exactly that instant, `converse()` does not separately state a reminder time — restating it would just repeat what was already said, not add information. `effort_minutes` stays conversationally askable (extractor-svc guesses a bucket only when the message gives real signal; resolver-svc's `converse()` asks otherwise), but purely for Calendar event sizing now — reminder timing doesn't depend on it.

### 4.2 Fire-time suggestion — LLM-generated (ADR 0009 fire mechanism unchanged, the text and reply-parsing are new)

**V1 simplification, user-directed (this section previously specified a fixed `render_fire_suggestion` template and a keyword-matched `Y`/`N`/`Later` reply — both retired):** the fire-time nudge and the reply to it are both handled by one new LLM call site, `dispatcher_svc/conversation.py::converse_suggestion` (§0 above). Two real problems drove this, on top of the reply-parsing rigidity itself: the old template stated the *free interval's* size ("you have 1h free"), not the item's own `effort_minutes` — misleading whenever the block happened to be bigger than the task; and a keyword-only reply classifier is a bad fit for a bot that otherwise texts like a real person.

```python
class SuggestionTurnResult(BaseModel):
    message_text: str | None = None   # set on the nudge turn
    intent: Literal["ACCEPT", "DECLINE", "SNOOZE", "OTHER"] | None = None  # set on the reply turn
    reply_text: str | None = None     # set only on OTHER, a natural re-ask
```

- **Nudge turn** (`latest_reply=None`): given only `title`/`effort_minutes` — deliberately terse inputs, same "the moment itself is the pitch" reasoning the old template already used (no time-of-day clause, no "lightest day" evidence line). States the real `effort_minutes`, not the block size, in one natural sentence — e.g. `"yo u have 15 minutes to apply to that tesla job?"` for a 15-minute item, no title quoted verbatim, no `Y / N / Later` footer.
- **Reply turn** (`latest_reply` given): classifies the free-text reply as `ACCEPT`/`DECLINE`/`SNOOZE`/`OTHER`. Deliberately does **not** generate the acknowledgment text for `ACCEPT`/`DECLINE`/`SNOOZE` — those state real, just-computed facts (the actual committed time, the actual rescheduled day) the model was never given and must never guess, the same ADR 0003 boundary the conversation call site's own locked-in message already respects. `reply_text` is set only on `OTHER`, a genuine natural re-ask — real, adjacent bug fixed in the same pass: an ambiguous reply used to get **zero** SMS response at all.

**`ACCEPT`** — `dispatcher-svc` re-verifies real current availability and publishes `ConfirmedItemMessage` exactly as before (§4.4). **`DECLINE`, first dismissal** and **`SNOOZE`** — the placeholder moves or clears (capacity-engine.md §5.4), acknowledged by the existing deterministic templates, unchanged:

```
np, i'll text you again {new_next_fit_start weekday}.
```
or, if nothing in the 7-day window fits:
```
np, i'll keep an eye out for room.
```
`render_deferred(next_fit_start, tz)`. `render_dismissed()` ("Got it, I won't suggest that again for a while.") is reached **only** on the second `DECLINE` (30d dormancy) — its "for a while" wording would be actively wrong for a first decline, which reschedules within the same reply. `render_snoozed()` ("OK, I'll check back in about a week.") — `SNOOZE`'s 7d framing is always accurate.

### 4.3 Accepted-suggestion → `ConfirmedItemMessage`

Per `state-machine.md` §2.3: on an inferred `ACCEPT`, `dispatcher-svc` constructs a `ConfirmedItemMessage` directly (`type="obligation"`, `due_at` = the computed slot start time, `action_type="calendar"`) and publishes it — the fields are already fully known from the `items`/`latents` rows involved (`capacity_snapshots` is no longer part of this path, ADR 0009 — `suggestions.scheduled_for` carries the slot instead, `migrations/0020`).

**ADR 0009 addition — placeholder promotion, inside `committer-svc`:** on consuming this message, `committer-svc` checks `latents.placeholder_event_id` for the item first. If set (true for every latent that reached `Y` via the fire-time flow), it `PATCH`es that same Calendar event in place — `[idea] ` stripped from the title, the real `summary`/`due_at` set — instead of `POST`ing a new event, then clears `placeholder_event_id`/`next_fit_start`. Falls back to a fresh `POST` if the `PATCH` 404s (the user deleted the placeholder by hand).

---

## 5. Open items for sibling docs

- ~~ADK project/location/model-version environment configuration, and the Vertex AI service account each of `extractor-svc`/`resolver-svc` runs under~~ → done, see `infrastructure.md` §3.
- Gemini call retry/timeout policy (a Gemini 5xx or timeout during extraction or clarification is a **technical failure**, per `state-machine.md` §3 — routed to the normal Pub/Sub retry/DLQ path, not a special case) → nothing further needed here, just confirming it's covered by the existing failure-handling doc, not a new mechanism.
