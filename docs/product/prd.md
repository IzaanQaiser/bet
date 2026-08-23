# PRD — Capacity-Aware Obligation Engine

**Hackathon:** All Things Agentic (Google/Devpost) · Track: **Taskmaster** · Deadline: **31 Aug 2026, 5:00pm PDT**
**Build mode:** solo, implementation via coding agents. This document is the source of truth. Read it at the start of every session.

---

## 1. Thesis

> Your calendar has holes. Your head has a backlog. The agent watches both and closes the gap.

This is **not** an inbox-triage bot. Capture is table stakes and every competing submission has it. The product is **dispatch**: the system decides, unprompted, that Thursday afternoon has three uninterrupted hours and that the idea you dumped in week two is the right shape to fill it.

Every design decision below serves that thesis. If a feature does not make the dispatcher better or the dispatcher's output more trustworthy, it is a cut candidate.

**Friction being solved (BYOF mandate):** obligations and ideas arrive through four channels (email, text, screenshots, verbal). Obligations get handled late or twice. Ideas get captured somewhere and never resurface, because resurfacing has no natural trigger — you only remember an idea when you're too busy to act on it.

**Notification fatigue, addressed directly:** calendar notifications get ignored because there are too many of them and none are prioritized. The dispatcher does not add another notification stream — it replaces the ones you already ignore with the smaller number you'd actually act on: a deadline reminder sent as SMS at a deliberately chosen moment (not "15 minutes before" spam), and at most one proactive suggestion per run. Restraint is what makes the channel trustworthy.

---

## 2. Scope

### In scope (MVP)
1. Multimodal SMS ingest — image, PDF, text
2. Structured extraction with confidence + missing-field detection
3. Clarification loop over SMS (bounded)
4. Explicit confirmation before any write
5. Semantic dedupe on ingest
6. Calendar write for obligations + **SMS deadline reminders** (the alarm-clock-fatigue fix — see §1)
7. Capacity computation from the user's calendar
8. **Capacity-triggered resurfacing of latent items** ← the headline
9. Dismissal feedback loop that adjusts future scoring
10. Dead-letter queue + failure handling throughout

### Stretch (post-core-loop, agreed in session)
11. **Email draft + send action.** For actionable items where the obligation *is* an email, the system drafts the email, sends the draft back over SMS for review, and — on explicit confirmation — sends it via Gmail API. This is **not** a new subsystem: it reuses the existing Extractor → Resolver → confirm → commit pipeline with a second write target (Gmail send) alongside the existing one (Calendar write). The confirm-gate is identical to the calendar path — no autonomous send.

### Explicitly out of scope
- Browser automation
- Email/Slack ingest (SMS only — one channel, done well)
- Multi-user / teams
- Mobile app or rich web UI (SMS is the interface; a thin read-only dashboard is optional)
- Task completion tracking beyond accept/dismiss
- **Generalized agentic task execution** (pay a bill, "do x task" on the user's behalf, arbitrary computer use). Excluded permanently, not just deferred: it contradicts the core trust principle (confirm before any write), each action category is its own integration surface with no shared pipeline to reuse, and irreversible financial actions are the wrong place to spend a 9-day build's risk budget. State this explicitly in the write-up as a deliberate scope boundary, not a gap.

### Cut order under time pressure
Cut from the bottom. **Never cut the dispatcher — without it this is a forwarding bot.**

1. Lyria audio
2. Veo cold-open
3. Read-only dashboard
4. Email draft + send action
5. Thread attachment (keep dedupe)
6. Clarification loop → degrade to "reply with the missing field"
7. — hard floor —
8. Capacity engine (DO NOT CUT)
9. Ingest→confirm→commit path (DO NOT CUT)

---

## 3. Hackathon compliance (Stage One is pass/fail)

| Requirement | Satisfied by |
|---|---|
| Gemini 3.5+ via Gemini API or Vertex AI | Extractor + Resolver on Gemini 3.5 Flash via Vertex AI |
| ≥1 Google agent framework | Google ADK |
| ≥1 GCP infra service | Cloud Run, Pub/Sub, Cloud SQL, Cloud Scheduler, Secret Manager |
| Public repo | GitHub, public |
| README spin-up instructions | Section 12 |
| Architecture diagram | Section 11 |
| ≤4 min demo video, public on YouTube | Section 13 |
| Visible proof of GCP deployment in video | Cloud Run logs on screen during live run |

**Bonus (up to +0.8):** blog post (+0.2), social post with `#AllThingsAgenticHackathon` (+0.2), Veo (+0.2), Lyria (+0.2).

**Rubric risk to preempt explicitly in the demo/write-up:** Taskmaster judging asks whether the agent "completes a multi-step background workflow without human intervention." Our confirm-gate means nothing commits without a reply. Frame this correctly: autonomy lives in the *dispatcher deciding, unprompted, that now is the moment* — not in skipping consent on an irreversible write. State this distinction on camera; don't let a judge read the confirm-gate as incompleteness.

---

## 4. Core concepts

**Item** — anything the user sends. Classified at extraction into:
- **Obligation** — has or implies a deadline. Goes to the calendar.
- **Latent** — an idea, project, or intention with no deadline. Goes to the backlog and waits for capacity.

**Capacity snapshot** — a scored description of a future day derived from the user's calendar.

**Revival score** — how strongly a latent item wants to be surfaced right now, given a specific capacity snapshot.

**The core loop:** capture → classify → (obligation → calendar + reminders) or (latent → backlog) → scheduler computes capacity → matches backlog to capacity → surfaces one suggestion → outcome feeds back into scoring.

---

## 5. Agents

Three agents, strictly separated. **LLMs decide content; the state machine decides control flow.** There is no orchestrator agent — this is a deliberate architectural decision and should be stated explicitly in the demo and write-up.

### 5.1 Extractor
**Role:** raw multimodal input → structured JSON.
**Model:** Gemini 3.5 Flash (Vertex AI), strict response schema.
**Write access:** **none.** This is the security story — the agent that touches untrusted user input cannot write to anything.

Input: media bytes + MIME type + message text.
Output:
```json
{
  "type": "obligation" | "latent",
  "title": "string",
  "summary": "string",
  "due_at": "ISO8601 | null",
  "effort_minutes": 15 | 30 | 60 | 120 | 240,
  "focus_depth": "shallow" | "deep",
  "confidence": 0.0-1.0,
  "missing_fields": ["due_at", ...],
  "reasoning": "string"
}
```

Rules:
- Never invent a `due_at`. If a date is implied but ambiguous ("next week"), put it in `missing_fields`.
- `effort_minutes` is a bucket, not a free number — keeps matching tractable.
- `focus_depth: deep` means the task needs a contiguous block; `shallow` can fit in gaps.

### 5.2 Resolver
**Role:** owns the human loop. Turns a low-confidence or incomplete extraction into a confirmed record.
**Model:** Gemini 3.5 Flash for question generation only.
**Write access:** writes to `items` in state `CLARIFYING`/`CONFIRMED`. No calendar access, no email-send access.

Behaviour:
1. **Dedupe check.** Embed the item, cosine search `item_embeddings`.
   - `similarity ≥ 0.92` → ask the user: "Is this the same as *[existing title]*?" Never merge silently.
   - `0.82 ≤ similarity < 0.92` and existing item is a latent → offer thread attachment.
2. **Clarification.** If `missing_fields` non-empty or `confidence < 0.75`, ask for the missing fields. **Max 3 exchanges**, batching multiple missing fields into one message where natural. On exhaustion → park the item as `NEEDS_REVIEW`, do not guess.
3. **Confirmation.** Always. Render a compact card and require an explicit affirmative before emitting `commit`.

> **Design principle (non-negotiable):** the system is only as good as it is accurate. An extra tap is an acceptable price for certainty. Never write to the calendar (or send an email) on inference alone.

Confirmation format:
```
📅 Visa appointment
Thu 4 Sep, 2:00 PM · 60 min
Reply Y to confirm, N to cancel, or send a correction.
```

### 5.3 Dispatcher
**Role:** the agent that runs when the user isn't there. This is the product.
**Trigger:** Cloud Scheduler, daily (07:00 user-local) + a light midday pass.
**Write access:** reads calendar, writes `suggestions`, sends SMS. Cannot mutate items other than surfacing metadata.

Per run:
1. Pull the next 7 days from Google Calendar.
2. Compute a `capacity_snapshot` per day (Section 6).
3. Fire SMS deadline reminders for obligations due within the reminder window.
4. Score every eligible latent against the best snapshot.
5. If `max(revival_score) > threshold` → send **at most one** suggestion.
6. Record the suggestion; wait for outcome.

**One suggestion per run, maximum.** A system that surfaces five ideas is a notification spammer. Restraint is the feature.

Suggestion format:
```
Thursday looks open — 3h clear in the afternoon,
lightest day you've had in two weeks.

💡 "Rewrite the ingest pipeline in Rust"
   (you mentioned this 18 days ago)

Want it on the calendar? Y / N / Later
```

Naming the *evidence* ("3h clear", "lightest day in two weeks") is what makes this read as intelligence rather than a random reminder. Always include it.

---

## 6. The capacity engine

This is the differentiator. It must be a real scoring system, not a heuristic. Build it **second**, immediately after the ingest path works end to end.

### 6.1 Snapshot metrics
Computed per candidate day over the user's configured working hours:

| Metric | Definition | Why it matters |
|---|---|---|
| `free_minutes` | Total unbooked minutes in working hours | Baseline availability |
| `largest_contiguous_block` | Longest single unbooked stretch | The number that actually matters for deep work |
| `fragmentation_index` | gaps < 45 min ÷ total gaps | A day of six 20-min gaps is not a free day |
| `load_delta` | (day's booked minutes − 14-day rolling mean) ÷ mean | Makes "light" relative to *this user*, not absolute |

### 6.2 Fit
```
fit_score = block_fit × depth_fit × load_fit
```
- `block_fit` — for `focus_depth: deep`, requires `largest_contiguous_block ≥ effort_minutes × 1.25`; returns 0 below that. For `shallow`, requires `free_minutes ≥ effort_minutes`.
- `depth_fit` — deep items are penalised on days with `fragmentation_index > 0.5`; shallow items are *rewarded* on fragmented days (they fill gaps).
- `load_fit` — scales with how far below the rolling mean the day sits. A day at the mean scores ~0.5; a day 40% below scores ~1.0.

### 6.3 Revival score
```
revival_score = recency_decay × dismissal_penalty × fit_score

recency_decay     = 1 − exp(−days_since_capture / 14)     # ideas need to breathe before resurfacing
dismissal_penalty = 1 / (1 + dismissal_count)
```

Rules:
- Items younger than 3 days are never surfaced.
- `dismissal_count ≥ 2` → dormant for 30 days.
- `Later` response → snooze 7 days, no dismissal penalty.
- An item surfaced in the last 10 days is not eligible.

### 6.4 Feedback loop
Every suggestion outcome is written to `suggestions`. Dismissals adjust `dismissal_count`; acceptances are the ground truth signal that the fit model works. This is ~20 lines of code and it is the difference between "heuristic" and "adaptive system" in the write-up. Do not skip it.

---

## 7. Orchestration — state machine

No orchestrator agent. Explicit states, explicit transitions, one terminal failure state.

```
RECEIVED
   ↓ extractor
EXTRACTED
   ↓ resolver: dedupe check
   ├→ DUPLICATE_SUSPECTED → (user confirms merge) → MERGED
   ↓ resolver: completeness check
   ├→ CLARIFYING ⟲ (max 3 exchanges) → NEEDS_REVIEW (on exhaustion)
   ↓
AWAITING_CONFIRMATION
   ├→ (N) → CANCELLED
   ↓ (Y)
CONFIRMED
   ↓ committer
COMMITTED
```

Any stage can transition to `FAILED` → dead-letter queue.

Latent lifecycle, separately:
```
COMMITTED(latent) → ELIGIBLE ⟷ SURFACED
                       ├→ ACCEPTED → converted to obligation → calendar
                       ├→ SNOOZED (7d) → ELIGIBLE
                       └→ DISMISSED ×2 → DORMANT (30d) → ELIGIBLE
```

State transitions are persisted. The current state of every item is queryable — this is what makes the system debuggable on camera.

---

## 8. Data model

**Cloud SQL for PostgreSQL + pgvector.** One instance, two concerns, no second system to operate. AlloyDB is better tech and worse for a 9-day build.

```sql
users(
  id, phone_e164, google_refresh_token_ref, timezone,
  working_hours_start, working_hours_end, created_at
)

items(
  id, user_id, raw_channel, raw_media_uri, ingested_at,
  type,                    -- obligation | latent
  state,                   -- see §7
  title, summary,
  effort_minutes, focus_depth, confidence,
  dedupe_hash,             -- cheap exact-match prefilter
  parent_item_id,          -- thread attachment
  created_at, updated_at
)

obligations(
  item_id PK FK, due_at, calendar_event_id,
  reminder_sent_at, reminder_window_hours,
  action_type,              -- calendar | email
  email_draft, email_sent_at
)

latents(
  item_id PK FK, last_surfaced_at, surface_count,
  dismissal_count, dormant_until
)

item_embeddings(
  item_id PK FK, embedding vector(768)
)
-- HNSW index, cosine distance

capacity_snapshots(
  id, user_id, date, free_minutes, largest_contiguous_block,
  fragmentation_index, load_delta, computed_at
)

suggestions(
  id, item_id FK, snapshot_id FK, sent_at,
  outcome,                 -- accepted | dismissed | snoozed | no_response
  responded_at
)

conversations(
  id, user_id, item_id FK, state, exchange_count,
  last_message_at, pending_fields
)

dead_letters(
  id, item_id, stage, payload_ref, error, retry_count, created_at
)
```

**Embeddings:** `text-embedding-004` on Vertex AI, over `title + summary`.

**Why a vector column at all:** the embedding earns its keep on **write**, not read. A screenshot of an email about a visa deadline and a text about the same thing have near-zero lexical overlap and near-identical embeddings — hash dedupe misses this entirely. Resurfacing is *not* a semantic query: the dispatcher holds a shape (150 contiguous minutes, deep focus), not a query string, so it's a SQL filter over structured columns. Do not build resurfacing on vector search.

---

## 9. Infrastructure

```
Twilio SMS webhook
   ↓
Cloud Run: ingest-svc          (validates, stores media to GCS, publishes)
   ↓ Pub/Sub topic: items.raw
Cloud Run: extractor-svc       (Gemini 3.5 Flash, ADK)
   ↓ Pub/Sub topic: items.extracted
Cloud Run: resolver-svc        (dedupe, clarify, confirm — holds conversation state)
   ↓ Pub/Sub topic: items.confirmed
Cloud Run: committer-svc       (Calendar API write, Gmail API send, Postgres write)

Cloud Scheduler (daily + midday)
   ↓
Cloud Run: dispatcher-svc      (capacity, scoring, suggestion send)
```

- Every Pub/Sub subscription has a **dead-letter topic** after 3 delivery attempts.
- **Per-service service accounts, minimum scopes.** `extractor-svc` has no Calendar scope, no Gmail scope, and no DB write role — enforced by IAM, not convention.
- Secrets (Twilio, Google OAuth refresh tokens) in **Secret Manager**.
- Media in GCS with a lifecycle rule (30-day delete).
- Structured JSON logging with `item_id` as a correlation field across all services — this is what you show on camera.

**Cost control:** min instances 0 everywhere, Cloud SQL smallest tier, stop the instance after the demo is recorded. Submission does not require a live deployment, only proof of one.

---

## 10. Onboarding / auth

1. User texts the number.
2. Bot replies with a one-time OAuth link (Google Calendar scope; Gmail send scope requested at the same time, used only if the email-action stretch ships).
3. User grants access; refresh token stored in Secret Manager, reference in `users`.
4. Bot asks for timezone + working hours (two messages).
5. Ready.

Calendar access is **required** — the dispatcher cannot function without it. State this at onboarding, not later.

---

## 11. Architecture diagram

Mermaid, committed to the repo, exported to PNG for Devpost. Must show:
- SMS entry point and media path
- All five Cloud Run services and the Pub/Sub topics between them
- Cloud Scheduler → dispatcher
- Gemini/Vertex calls, labelled with what each agent is allowed to touch
- Cloud SQL split into relational tables and the vector column
- Google Calendar and Gmail as external write targets
- **Dead-letter paths drawn explicitly** — most submissions omit these and it's free credibility on Architectural Discipline

*(Lives in `docs/architecture/` as the canonical, implementation-grade version; this section anchors the Devpost-facing export.)*

---

## 12. README requirements

Judged on reproducibility. Must contain:
1. One-paragraph thesis (Section 1, verbatim)
2. Architecture diagram inline
3. Prerequisites: GCP project, billing, Twilio number, enabled APIs
4. `terraform apply` or a scripted `gcloud` sequence — no manual console steps
5. Schema migration command
6. Environment variables table
7. Local dev instructions (Pub/Sub emulator)
8. How to trigger the dispatcher manually (judges will want to see the capacity engine without waiting a day)
9. Disclosure: built with AI coding assistants (required by rules)

---

## 13. Demo video (4 min hard cap)

30% of the score. Judging language: *"unedited, live execution"* and *"visible proof of Google Cloud deployment."* A polished, obviously-edited film scores worse than a live terminal.

**Structure:**

| Time | Content |
|---|---|
| 0:00–0:30 | **Cold open — the dispatcher fires unprompted.** Phone on the desk buzzes. The suggestion appears. No setup, no narration yet. Lead with the twist. |
| 0:30–1:00 | The friction, stated plainly. Obligations arrive in four channels; ideas die in notes apps because resurfacing has no trigger. |
| 1:00–1:30 | Architecture walkthrough on the diagram. Say the line: *"LLMs decide content, the state machine decides control flow."* Point at the DLQ. |
| 1:30–3:00 | **Live, unedited run.** Send a screenshot → clarification exchange → confirmation → calendar event appears. Cloud Run logs streaming in a visible pane throughout. Show the Cloud Run dashboard and the `.run.app` URL. |
| 3:00–3:40 | **Manually trigger the dispatcher.** Show the capacity snapshot in the logs — the actual numbers, the contiguous block, the load delta. Suggestion arrives. Accept it. Calendar updates. |
| 3:40–4:00 | Dismissal loop in one line, close. |

**Non-negotiable:** the split-screen of phone + streaming Cloud Run logs during the live segment. That single shot answers *Proof of Action* and *proof of GCP deployment* simultaneously.

**Demo data note:** the "you mentioned this 18 days ago, lightest day in two weeks" narrative (§5.3, `capacity-engine.md` §6) needs a latent that's actually old and a real 14-day calendar history to compute `load_delta` against — neither exists organically this early in a 9-day build. Don't leave this to chance: build a small seed script (`scripts/seed-demo-data.sh` or similar, alongside build order step 8) that inserts a demo latent with a backdated `created_at` and, if needed, synthetic `capacity_snapshots` history, so the demo suggestion is reproducible and controllable rather than dependent on having organically used the bot for weeks beforehand.

If a Veo cold-open is used, it must be clearly stylised and confined to 0:00–0:15, so no judge mistakes the live segment for edited footage.

---

## 14. Build order

Strict. Do not work ahead.

1. **Skeleton** — Twilio → Cloud Run → echo. Proves the loop.
2. **Ingest → extract → commit**, text only, no clarification, no dedupe. First calendar event written.
3. **Capacity engine + dispatcher.** ← the thing that must exist
4. Multimodal (image, PDF)
5. Confirmation + clarification loop
6. Dedupe via embeddings
7. DLQ + error handling
8. Feedback loop / dismissal scoring
9. Email draft + send action (stretch)
10. Record demo
11. README, diagram, write-up
12. Bonus: blog, social, Veo, Lyria

**Record the demo before the last day.** A broken demo on 31 Aug with working code is a zero.

---

## 15. Non-negotiables

- Never write to the calendar, or send an email, without explicit user confirmation.
- Never merge a suspected duplicate silently.
- Never invent a date the user didn't supply.
- Maximum one proactive suggestion per dispatcher run.
- The extractor never holds write credentials.
- Suggestions always name their evidence ("3h clear, lightest day in two weeks").
- Generalized agentic execution (bill pay, arbitrary tasks) is out of scope permanently, not deferred.
- The dispatcher ships. Everything else is negotiable.
