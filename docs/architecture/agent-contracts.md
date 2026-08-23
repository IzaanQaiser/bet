# Agent Contracts

Fifth doc in the architecture set — see `overview.md` §0. This closes out every "exact schema / exact rendering" item deferred by `overview.md`, `state-machine.md`, `data-model.md`, and `capacity-engine.md`. Pydantic models shown here are the spec; `shared/obligation_engine_shared/schemas.py` (`docs/engineering/conventions.md`) is the literal code — if they ever disagree, the code is buggy, not this doc.

---

## 0. Where Gemini is actually called

**Resolved inconsistency:** `overview.md`'s diagram originally labeled `dispatcher-svc` `(Cloud Run, ADK)`. Corrected — it isn't. Neither the PRD nor `capacity-engine.md` gives Dispatcher a Gemini call: every number it produces is arithmetic (§capacity-engine.md), and the suggestion/reminder text is deterministically templated from those numbers (§4 below). Giving Dispatcher an LLM call would only introduce unpredictability into the one output that most needs to be exactly reproducible on camera. `overview.md`'s diagram has been fixed to drop `ADK` from that node.

That leaves exactly two LLM call sites in the whole system:

| Call site | Service | Purpose | Structured output |
|---|---|---|---|
| Extraction | `extractor-svc` | Raw input → structured item | Yes, strict schema |
| Clarification | `resolver-svc` | Merge a reply into missing fields + write the next question | Yes, strict schema |

Everything else user-facing — the dedupe question, the confirmation card, the thread-attach offer, terminal messages (`CANCELLED`/`MERGED`/`NEEDS_REVIEW`), reminders, and suggestions — is a deterministic Python template. This is worth stating explicitly in the demo: the LLM surface area is two narrow, schema-constrained calls, not "an agent writes all your texts."

Both call sites use Google ADK with a Gemini 3.5 Flash model and a JSON response schema (ADK's structured-output mode) — no free-text parsing of a model response anywhere in this system.

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
    focus_depth: Literal["shallow", "deep"]
    confidence: float                 # 0.0-1.0
    missing_fields: list[str]
    reasoning: str                    # log-only, never shown to the user

# items.confirmed — published by resolver-svc (normal path) or
#                    dispatcher-svc (accepted-suggestion path, state-machine.md §2.3)
class ConfirmedItemMessage(BaseModel):
    item_id: UUID
    user_id: UUID
    type: Literal["obligation", "latent"]   # a surfaced latent arrives here already flipped to "obligation"
    title: str
    summary: str
    due_at: datetime                        # required here — by this point ambiguity must be resolved
    effort_minutes: Literal[15, 30, 60, 120, 240]
    action_type: Literal["calendar", "email"]
    email_draft: str | None                 # required if action_type == "email"
```

`ExtractedItemMessage` carries the *entire* extraction result, not just `item_id` — `extractor-svc` has no DB write role (ADR 0003), so `resolver-svc` is the first service to actually persist these fields into `items`.

---

## 2. Extractor contract

**Input:** media bytes (if any) + MIME type + SMS text, from `RawItemMessage`.
**Output:** `ExtractedItemMessage` fields (minus `item_id`/`user_id`, which the service adds).

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
- focus_depth is "deep" if the task needs one uninterrupted stretch of
  concentration (writing, coding, focused analysis); "shallow" if it can be
  done in short pieces or is administrative/low-cognitive-load (a phone
  call, filling a form, paying a bill).
- confidence reflects your overall certainty about the classification and
  fields, not any single field in isolation.
- reasoning is one sentence explaining the classification, for logs only —
  it is never shown to the user.

Output must conform exactly to the provided schema. No text outside it.
```

If a message plausibly contains **more than one item** (e.g. a screenshot with two separate deadlines), the extractor still emits exactly one — the most salient one, by its own judgment — and folds the rest into `summary` as context. Splitting one message into multiple items is out of scope; noted here so it isn't rediscovered as a bug later, it's a boundary.

---

## 3. Resolver contract

### 3.1 Dedupe question — deterministic, no LLM

Per `state-machine.md` §1.1. Fixed template, filled from the existing matched item's `title`:
```
Is this the same as "{existing_title}"?
Reply Y to merge, N if it's different.
```

### 3.2 Clarification loop — the second (and last) Gemini call

**Resolved ambiguity:** PRD §5.2 describes this as "Gemini 3.5 Flash for question generation only." Read literally that undersells it — you cannot write a sensible next question without first interpreting what the user's last reply actually answered. This call does both in one pass: merge the reply into the item, then decide the next question (or that none is needed).

```python
class ClarificationRequest(BaseModel):
    title: str
    summary: str
    type: Literal["obligation", "latent"]
    known_fields: dict[str, Any]      # already-filled fields, for context
    missing_fields: list[str]         # from conversations.pending_fields
    latest_reply: str | None          # null on the first turn (no reply yet)

class ClarificationResponse(BaseModel):
    filled_fields: dict[str, Any]     # subset of missing_fields this reply resolved
    still_missing: list[str]          # missing_fields minus filled_fields' keys
    question: str | None              # null iff still_missing is empty
```

**System prompt:**
```
You are the clarification stage. You have a partially-structured item and
a list of fields still missing. Given the user's latest reply (absent on
the first turn), do two things:

1. Fill in any of the missing fields the reply actually answers.
2. If fields remain missing after that, write ONE short question — SMS
   length, under 160 characters where possible — that asks for all of them
   together in one natural sentence. Never itemize as a list, never ask
   about a field that isn't in missing_fields.

Rules:
- Never invent a value the reply didn't provide or clearly imply.
- If the reply is ambiguous for a field, leave that field in still_missing
  rather than guessing — an extra question is cheaper than a wrong write.
- On the first turn (latest_reply is null), filled_fields is empty and
  question asks for everything in missing_fields at once.

Output must conform exactly to the provided schema. No text outside it.
```

`resolver-svc` applies `filled_fields` to the `items` row, sets `conversations.pending_fields = still_missing`, and either sends `question` (incrementing `exchange_count`, per `state-machine.md` §1.2) or transitions to `AWAITING_CONFIRMATION` if `still_missing` is empty.

**A correction during `AWAITING_CONFIRMATION`** (`state-machine.md` §1.4) is handled by the same call: `known_fields` includes the current (possibly wrong) value, `missing_fields` is set to just the field the correction plausibly targets — inferred by a cheap heuristic (does the reply contain a time/date pattern → `due_at`; a duration pattern → `effort_minutes`; otherwise → whichever field is most recently confirmed and shortest, defaulting to `title`) — and `latest_reply` is the correction text. This reuses one schema instead of building a second "correction interpreter."

### 3.3 Confirmation card — deterministic, no LLM

Exact format, from PRD §5.2, with the optional thread-attach suffix (`state-machine.md` §1.1.3) appended only when applicable:
```
{icon} {title}
{formatted_due_at} · {effort_minutes} min
Reply Y to confirm, N to cancel, or send a correction.
```
`{icon}` is `📅` for `action_type == "calendar"`, `✉️` for `"email"`. `{formatted_due_at}` is rendered in the user's timezone as `Ddd D Mon, H:MM AM/PM` (e.g. `Thu 4 Sep, 2:00 PM`) — matches the PRD's literal example exactly.

Thread-attach suffix, appended as its own paragraph when a `0.82 ≤ similarity < 0.92` latent match exists:
```

Also similar to "{matched_latent_title}" — reply A to attach as a follow-up.
```

### 3.4 Terminal messages — deterministic, no LLM

Not specified in the PRD; added here because a conversation that just stops with no closing message reads as broken, not restrained.

| Terminal state | Message |
|---|---|
| `MERGED` | `Got it — that's the same as "{existing_title}". Nothing new added.` |
| `CANCELLED` | `Cancelled.` |
| `NEEDS_REVIEW` | `I couldn't get all the details for "{title}" — I've saved what I have. Send it again with more detail if you'd like me to try again.` |

---

## 4. Dispatcher contract — all deterministic templates

### 4.1 Deadline reminder
```
⏰ {title} is due {relative_due_description}.
{formatted_due_at}
```
`{relative_due_description}` is `"today"`, `"tomorrow"`, or `"in {N} days"` — computed from `obligations.due_at` and `obligations.reminder_window_hours`, not from the LLM.

### 4.2 Suggestion — exact rendering, ties to `capacity-engine.md` §6

```
{Day name} looks open — {block_hours} clear {time_of_day_phrase},
{evidence_line}.

💡 "{item_title}"
   (you mentioned this {days_since_capture} days ago)

Want it on the calendar? Y / N / Later
```

- `{block_hours}`: `largest_contiguous_block` formatted as `"Nh"` or `"Nh Mmin"`.
- `{time_of_day_phrase}`: `"in the morning"` / `"in the afternoon"` / `"in the evening"`, derived from whether the block's start falls before 12:00, before 17:00, or after, in the user's local time.
- `{evidence_line}`: per `capacity-engine.md` §6's two-tier rule — `"lightest day you've had in two weeks"` if today's `booked_minutes` is the minimum of the trailing 14 daily values, else `"lighter than usual"` if `load_delta < -0.15`, else omitted entirely (the paragraph reads `"{Day} looks open — {block_hours} clear {time_of_day_phrase}."` with no evidence clause) rather than forcing a weak claim. **Decision made here:** never state evidence that isn't actually true — an omitted evidence line is better than a generic one that undercuts the "the system actually looked" premise.

### 4.3 Reply parsing — shared, deterministic classifier

Used identically by `resolver-svc` (confirmation `Y`/`N`) and `dispatcher-svc` (suggestion `Y`/`N`/`Later`). Lives in `shared/obligation_engine_shared` per `docs/engineering/conventions.md` — one implementation, not two copies that could drift.

```python
AFFIRMATIVE = {"y", "yes", "yeah", "yep", "confirm", "ok", "okay", "sure"}
NEGATIVE    = {"n", "no", "nope", "cancel", "nah"}
SNOOZE      = {"later", "snooze", "not now", "l"}
ATTACH      = {"a", "attach"}

def classify_reply(text: str) -> Literal["Y", "N", "LATER", "ATTACH", "OTHER"]:
    normalized = text.strip().lower()
    if normalized in AFFIRMATIVE: return "Y"
    if normalized in NEGATIVE: return "N"
    if normalized in SNOOZE: return "LATER"
    if normalized in ATTACH: return "ATTACH"
    return "OTHER"
```

`"OTHER"` is a correction (in `AWAITING_CONFIRMATION`) or a clarification answer (in `CLARIFYING`) — routed to the Gemini call in §3.2, never guessed at deterministically. `"LATER"` is only meaningful when replying to a suggestion; if `classify_reply` returns `"LATER"` outside that context it's treated as `"OTHER"` (an odd correction attempt) by whichever service received it.

### 4.4 Accepted-suggestion → `ConfirmedItemMessage`

Per `state-machine.md` §2.3: on `"Y"` to a suggestion, `dispatcher-svc` constructs a `ConfirmedItemMessage` directly (`type="obligation"`, `due_at` = the computed slot start time, `action_type="calendar"`) and publishes it — no Gemini call, the fields are already fully known from the `items`/`latents`/`capacity_snapshots` rows involved.

---

## 5. Open items for sibling docs

- ~~ADK project/location/model-version environment configuration, and the Vertex AI service account each of `extractor-svc`/`resolver-svc` runs under~~ → done, see `infrastructure.md` §3.
- Gemini call retry/timeout policy (a Gemini 5xx or timeout during extraction or clarification is a **technical failure**, per `state-machine.md` §3 — routed to the normal Pub/Sub retry/DLQ path, not a special case) → nothing further needed here, just confirming it's covered by the existing failure-handling doc, not a new mechanism.
