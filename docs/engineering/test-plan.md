# Test Plan

One section per `docs/product/prd.md` §14 build-order step. Where that doc's "Done when" is a one-line signal, this is the checklist and the actual test names behind it. Read only the section for the step you're working — that's the point of keeping these separate from the PRD.

Three categories per step, not all always present:
- **Unit tests** — pure logic, no I/O, no emulator. Fast, required, per `docs/engineering/conventions.md`.
- **Automated/integration tests** — against the Pub/Sub emulator and a local Postgres (via Cloud SQL proxy), per `conventions.md`'s local dev setup. Cover the critical path, not everything.
- **Manual verification** — real Twilio/Calendar/Gmail. Not automated, on purpose (`conventions.md`: "Not building: end-to-end tests against real Twilio/Calendar/Gmail. Verified manually during the live demo instead"). Listed explicitly so nothing gets skipped silently.

Test files live in each service's `tests/` directory, or `shared/tests/` for anything in the shared package, per `conventions.md`'s repo layout.

---

## Step 1 — Infra skeleton

**Acceptance criteria**
- `terraform plan` is clean; `terraform apply` succeeds; re-applying immediately is a no-op.
- Every resource in `infrastructure.md` §1 exists: 6 Pub/Sub topics, Cloud SQL instance running, GCS bucket with 30-day lifecycle rule, Artifact Registry repo, 5 service accounts, the two static Secret Manager entries (placeholder values acceptable here).
- Every Pub/Sub IAM binding is resource-scoped, not project-wide (`infrastructure.md` §2.1's non-negotiable) — spot-check via `gcloud pubsub topics get-iam-policy` on `items.confirmed`: `sa-extractor` must not appear.

**Tests:** none (infra-only, nothing to unit test). Optional `scripts/verify-infra.sh` asserting resource existence via `gcloud`, run manually — not required.

**Manual verification:** review `gcloud pubsub topics list` / `gcloud sql instances list` / `gcloud iam service-accounts list` against `infrastructure.md` §1's table.

---

## Step 2 — DB schema + shared package

**Acceptance criteria**
- `scripts/migrate.sh` applies `migrations/0001_init.sql` cleanly.
- Every table/column/index/constraint in `data-model.md` §2 exists exactly, including the `resolved_fields` column and the `hnsw` index.
- Every Pydantic model in `agent-contracts.md` §1 exists verbatim in `shared/obligation_engine_shared/schemas.py`.

**Unit tests** (`shared/tests/test_schemas.py`)
- `test_raw_item_message_valid` / `test_extracted_item_message_valid` / `test_confirmed_item_message_valid` — construct each with valid fixture data, no error.
- `test_extracted_item_message_rejects_invalid_effort_minutes` — `effort_minutes=45` raises `ValidationError` (it's a `Literal`, not an int).
- `test_confirmed_item_message_due_at_optional_for_latent` — `type="latent", due_at=None` is valid (regression guard for the bug fixed in the cohesiveness pass).
- `test_message_roundtrip` — `Model.model_validate_json(instance.model_dump_json())` equals the original, for all three models.

**Integration tests** (`shared/tests/test_migration.py`)
- `test_migration_applies_cleanly` — against a scratch Postgres, run the migration, assert it exits 0.
- `test_all_tables_exist` — query `information_schema.tables`, assert all nine tables from `data-model.md` §2 are present.
- `test_pgvector_extension_enabled`.

---

## Step 3 — `ingest-svc` + real Twilio number

**Acceptance criteria**
- Valid, correctly-signed webhook POST → exactly one `items` row (`state='RECEIVED'`) and exactly one `items.raw` message, same `item_id`.
- Invalid/missing signature → 4xx, zero rows, zero messages (bad input is rejected inline per `state-machine.md` §3, never touches the pipeline).
- Real Twilio number wired to the deployed `.run.app` URL.

**Unit tests** (`services/ingest-svc/tests/test_webhook.py`)
- `test_valid_signature_accepted` / `test_tampered_signature_rejected` / `test_missing_signature_rejected`.
- `test_parses_text_only_payload` — Twilio form body → correct `text`, `media_uri=None`.

**Integration tests** (`services/ingest-svc/tests/test_ingest_integration.py`, Pub/Sub emulator + local Postgres)
- `test_valid_webhook_creates_item_and_publishes` — synthetic valid payload → one `items` row + one `items.raw` message, matching `item_id`.
- `test_invalid_webhook_creates_nothing` — synthetic bad-signature payload → zero rows, zero messages.

**Manual verification:** one real SMS to the real number; confirm via Cloud Logging filtered on the resulting `item_id`.

---

## Step 4 — `extractor-svc`

**Acceptance criteria**
- Consuming a text-only `RawItemMessage` → exactly one schema-valid `ExtractedItemMessage` published.
- Ambiguous date → `due_at: null` + `"due_at"` in `missing_fields`, never a guessed date.
- `effort_minutes` always one of the five buckets.
- No-deadline input → `type: "latent"`.
- `sa-extractor` has no Cloud SQL connectivity at all — an infra check, not a code one (confirm no `DATABASE_URL`/proxy sidecar configured for this service).

**Unit tests** (`services/extractor-svc/tests/test_pubsub_push.py` + `test_extraction.py`, Gemini/ADK mocked)
- `test_valid_envelope_extracts_and_publishes` — mocked extraction result published to `items-extracted` with `effort_minutes` cast from the wire string (`"15"`) to `int` (`15`).
- `test_malformed_envelope_returns_500_for_retry` / `test_extraction_failure_returns_500_for_retry` / `test_publish_failure_returns_500_for_retry` — each failure mode is caught before a bad publish and surfaces as a 500 so Pub/Sub retries (a schema-invalid model response fails inside `_extract`'s `model_validate` the same way).
- `test_extract_parses_final_event_text` — the ADK `Runner`'s event stream is mocked; asserts `_extract` correctly reads `event.content.parts[-1].text` (not `event.output`, which is `None` even with `output_schema` set — see the "Resolved gap" note in `agent-contracts.md` §2) and that an ambiguous date stays `due_at: null` with `"due_at"` in `missing_fields`.
- `test_extract_raises_if_no_final_response` — no final event ever arrives → `RuntimeError`, not a silent no-op.

**Integration test** (`test_extractor_integration.py`, real Pub/Sub emulator, Gemini mocked)
- `test_raw_to_extracted_end_to_end` — POST a real push envelope to `/pubsub/push` with `_extract` mocked → publish goes through the real emulator client → pulled back on a throwaway subscription and asserted, not just checked via a mock call.

**Manual verification (real Gemini, not mocked):** send 3–5 representative real messages — clear obligation, ambiguous date, no-deadline idea, a garbled/low-confidence one — and eyeball extraction quality. This is model-output quality, not wiring; it cannot be meaningfully asserted in an automated test.

---

## Step 5 — `resolver-svc` stub (temporary)

**Acceptance criteria**
- Any `ExtractedItemMessage` with **empty** `missing_fields` → immediately publishes a matching `items.confirmed`, no DB writes beyond progress, no SMS.
- An item with **non-empty** `missing_fields` is left in `EXTRACTED` and logged, not force-confirmed — this step proves the happy path only, it doesn't pretend to solve incomplete items.
- Every auto-confirm is logged distinctly (e.g. `"AUTO-CONFIRMED (stub, no gate) item_id=..."`) so it's unmistakable in Cloud Logging that this is temporary scaffolding, not the real gate.

**Unit tests** (`services/resolver-svc/tests/test_stub.py`, DB + Pub/Sub mocked)
- `test_complete_item_auto_confirms` — extracted fields written to `items` with `state='CONFIRMED'`, `ConfirmedItemMessage` published with `action_type="calendar"`.
- `test_latent_confirms_with_no_action_type` — a `type="latent"` item publishes with `action_type=None`, per `agent-contracts.md` §1.
- `test_incomplete_item_left_in_extracted` — `missing_fields` non-empty → fields still written (the "progress" update) but `state='EXTRACTED'`, no publish.
- `test_malformed_envelope_returns_500_for_retry` / `test_db_write_failure_returns_500_for_retry` / `test_publish_failure_returns_500_for_retry` — each failure mode surfaces as a 500 so Pub/Sub retries.

**Integration test** (`test_resolver_integration.py`, real Pub/Sub emulator + real dev Postgres via the Cloud SQL Auth Proxy) — `test_extracted_to_confirmed_stub`: publish a complete `ExtractedItemMessage`, assert the `items` row is `CONFIRMED` with the right title, and `ConfirmedItemMessage` is pulled back on `items.confirmed`.

---

## Step 6 — `committer-svc`

**Acceptance criteria**
- `type="obligation"` → one real Calendar event (verified by reading it back via the Calendar API, not just "no error"), one `obligations` row with matching `calendar_event_id`, `items.state='COMMITTED'`.
- `type="latent"` → zero external calls, one `latents` row (`surface_count=0`, `dismissal_count=0`, `dormant_until=null`), `items.state='COMMITTED'`.
- A Calendar API failure does not mark `COMMITTED` — item stays recoverable, error logged with `item_id`.

**Unit tests** (`services/committer-svc/tests/test_committer.py`, DB + Secret Manager + Calendar client all mocked)
- `test_obligation_branch_calls_calendar_write` — mock asserts calendar-write called with correct args (event spans `due_at` to `due_at + effort_minutes`, per `agent-contracts.md` §1's "Resolved gap" note), `obligations` row inserted with the returned `calendar_event_id`, `items.state='COMMITTED'`.
- `test_latent_branch_does_not_call_calendar` — mock asserts calendar-write **not** called, `latents` row inserted, `items.state='COMMITTED'`.
- `test_calendar_failure_does_not_mark_committed` — only the credentials lookup happens; the `obligations` INSERT / `items` UPDATE are never reached.
- `test_no_linked_google_account_fails_without_writing` — a user with no `google_refresh_token_ref` fails loudly rather than silently skipping the write.
- `test_email_action_type_not_implemented` — `action_type="email"` fails loudly (step 15 stretch, not built yet) rather than being silently treated as a calendar write.
- `test_malformed_envelope_returns_500_for_retry`.

**Integration tests** (`test_committer_integration.py`, real dev Postgres via Cloud SQL Auth Proxy; Calendar + Secret Manager mocked — no Pub/Sub emulator needed, committer-svc never publishes)
- `test_confirmed_obligation_full_cycle` / `test_confirmed_latent_full_cycle` — real `items`/`obligations`/`latents` rows verified against the live DB.

**Manual verification:** one real obligation end-to-end — actual Google Calendar event appears. Required once against the real API; mocks can't fully validate OAuth/API behavior. Needs the one-time manual OAuth bootstrap (`infrastructure.md` §4) done first.

---

## Step 7 — Capacity engine, pure functions

The cleanest step to test — no I/O, and `capacity-engine.md` §6 already hands you exact expected numbers.

**Acceptance criteria**
- `free_intervals`, `block_fit`, `depth_fit`, `load_fit`, `revival_score` implemented exactly per `capacity-engine.md`'s formulas.
- The worked example (§6) reproduces exactly: `fit_score = 0.875`, `revival_score ≈ 0.633` (assert to 3 decimal places).
- The contrast example (§6, insufficient block) reproduces `fit_score = 0`.

**Unit tests** (`services/dispatcher-svc/tests/test_capacity_engine.py` — the module lives in `services/dispatcher-svc/src/dispatcher_svc/capacity_engine.py`, not `shared/`, since dispatcher-svc is its only caller; `dispatcher-svc` scaffolded early with just this pure module, same pattern as `extractor-svc`/`resolver-svc` before their own build steps)
- `test_free_intervals_merges_back_to_back_events`
- `test_free_intervals_all_day_event_blocks_whole_day`
- `test_free_intervals_excludes_declined_and_transparent`
- `test_block_fit_deep_requires_125_percent_margin`
- `test_block_fit_shallow_no_margin_required`
- `test_depth_fit_deep_flat_below_threshold` / `test_depth_fit_deep_falls_off_above_threshold` (floor 0.3)
- `test_depth_fit_shallow_rewards_fragmentation` (cap 1.2)
- `test_load_fit_at_mean_is_half` (`load_delta=0` → `0.5`) / `test_load_fit_40_percent_below_is_one` (`load_delta=-0.4` → `1.0`)
- `test_load_fit_clips_at_bounds` / `test_load_fit_cold_start_is_neutral` (`None` → `0.5`)
- `test_load_delta_cold_start_below_3_days`
- `test_worked_example_snapshot_matches_doc_exactly` — §6's raw events reproduce `free_minutes=330`, `largest_contiguous_block=180`, `fragmentation_index=0.0`, `load_delta=-0.30`
- `test_fit_score_worked_example_reproduces_0_875`
- `test_revival_score_worked_example` — the full §6 scenario, asserting `0.633` to 3dp
- `test_contrast_example_insufficient_block_scores_zero`
- `test_eligibility_excludes_young_items` (`days_since_capture < 3`) / `test_eligibility_excludes_dormant` / `test_eligibility_excludes_recently_surfaced` (within 10 days) / `test_eligibility_excludes_open_suggestion`
- `test_selection_picks_best_day_per_latent_then_argmax_across_latents`
- `test_selection_respects_threshold` (below `0.4` → no suggestion)

**Integration/manual:** none needed — this step is explicitly I/O-free.

---

## Step 8 — `dispatcher-svc`

Deliberately NOT built here: parsing a `Y`/`N`/`Later` reply to a sent suggestion, or any latent-lifecycle transition out of `SURFACED` (state-machine.md §2.2) — that's a different concern from *sending* the suggestion, and belongs with the feedback-loop step. A sent suggestion just sits as a `suggestions` row with `outcome IS NULL` until that step exists. Two window/trigger decisions this step had to make that weren't specified anywhere — see `capacity-engine.md` §1's "Resolved gap" note (the 7-day/14-day windows are both inclusive of today) and `agent-contracts.md` §4.1's "Resolved gap" note (exactly when a reminder fires).

**Acceptance criteria**
- `/dispatch`, invoked manually, computes and persists exactly 7 `capacity_snapshots` rows (one per day) from real Calendar reads.
- Reminders are idempotent: an obligation already reminded (`reminder_sent_at` set) is never reminded twice, even if `/dispatch` runs twice in a row.
- Never more than one suggestion sent per run, even with multiple latents clearing the threshold.
- Suggestion text matches `agent-contracts.md` §4.2 exactly, including all three evidence-line branches (superlative / "lighter than usual" / omitted).
- Exactly two Calendar API reads per user per run (`infrastructure.md` §4's quota assumption) — not one per day.

**Unit tests** (`services/dispatcher-svc/tests/test_templates.py` — pure template rendering, split out from the DB/SMS orchestration since it has no dependencies on either)
- `test_render_reminder_exact_format`.
- `test_suggestion_text_superlative_branch` / `test_suggestion_text_lighter_than_usual_branch` / `test_suggestion_text_omitted_branch` / `test_render_suggestion_full_worked_example` — fixed fixtures, assert exact rendered string for each.

**Unit tests** (`services/dispatcher-svc/tests/test_dispatcher.py` — DB and Twilio mocked)
- `test_reminder_not_resent_if_already_sent` / `test_reminder_sent_when_due_marks_reminder_sent_at`.
- `test_compute_day_reproduces_worked_example` — real Calendar events in, exact §6 numbers out.
- `test_eligible_latents_maps_rows_to_local_dates` — a `created_at` near a local-midnight boundary must map to the correct local calendar day, not the UTC one.
- `test_send_suggestion_returns_false_when_no_candidate_clears_threshold` / `test_send_suggestion_sends_exactly_one_and_writes_rows`.

**Integration tests** (`test_dispatcher_integration.py`, real dev Postgres via Cloud SQL Auth Proxy; Calendar + Twilio mocked — no Pub/Sub emulator needed, dispatcher-svc doesn't publish in this step's scope)
- `test_dispatch_run_produces_7_snapshots`.
- `test_dispatch_run_sends_at_most_one_suggestion` — two latents that would each independently clear threshold, assert exactly one suggestion SMS and one `suggestions` row.
- `test_dispatch_run_sends_reminder_and_marks_idempotent` — runs `/dispatch` twice against the same due obligation, second run sends nothing.

**Manual verification:** real `/dispatch` trigger against a real calendar (`scripts/dispatch-now.sh`) — this is the literal PRD §13 demo segment ("manually trigger the dispatcher").

---

## Step 9 — Real `resolver-svc` — confirmation

This step's scope turned out to include a hard prerequisite not named in its one-line build-order description: the inbound-SMS routing check (state-machine.md §4) in `ingest-svc`, since there's no way to do a real SMS confirm/cancel round trip — this step's own required manual verification — without it. Built here, not deferred: `ingest-svc` now checks for an open conversation (an item in `CLARIFYING`/`AWAITING_CONFIRMATION`) before treating an inbound message as new, and forwards to `resolver-svc`'s new `POST /reply` via a synchronous, authenticated (ID-token) service-to-service call. The second branch of that routing table — forwarding a suggestion reply to `dispatcher-svc` — is still not built; `dispatcher-svc` has no accept-path endpoint until the feedback-loop step, so nothing exists yet for that branch to call.

Also gates on confidence, not just `missing_fields`: `EXTRACTED --> CLARIFYING: resolver, missing_fields or confidence < 0.75` (state-machine.md §1.2) — a complete-but-low-confidence item is left in `EXTRACTED` exactly like an incomplete one (step 10 still owns clarification for both).

**Acceptance criteria**
- Complete/confident extraction → correct confirmation card variant (obligation vs. latent, `agent-contracts.md` §3.3) instead of auto-confirming.
- `AFFIRMATIVE`-set reply → `items.confirmed` published. `NEGATIVE`-set reply → `CANCELLED`, terminal message sent, no publish.
- A reply outside `Y`/`N` doesn't crash and is logged distinctly (full correction-handling completeness is step 10's job, since it reuses the clarification call).
- `classify_reply` (shared) is unit-tested once and reused identically here and by `dispatcher-svc` — no duplicated logic.
- An inbound SMS with an open conversation is forwarded to `resolver-svc`, not treated as a new item (`ingest-svc`).

**Unit tests**
- `shared/tests/test_classify_reply.py` — every string in each set maps correctly, case-insensitive/whitespace-trimmed; arbitrary strings map to `"OTHER"`.
- `services/resolver-svc/tests/test_confirmation_card.py` — obligation variant includes date/duration; latent variant has no date line. (Thread-attach suffix not tested — needs the embedding search dedupe isn't built until step 12.)
- `services/resolver-svc/tests/test_resolver.py` — `/pubsub/push`'s complete+confident/incomplete/low-confidence branches, and `/reply`'s `Y`/`N`/`OTHER`/unknown-item branches, DB and Twilio mocked.
- `services/ingest-svc/tests/test_webhook.py` — `test_no_open_conversation_creates_new_item` / `test_open_conversation_routes_to_resolver_not_new_item`.

**Integration tests** (`services/resolver-svc/tests/test_resolver_integration.py`, real dev Postgres + real Pub/Sub emulator; Twilio mocked)
- `test_extracted_to_awaiting_confirmation`.
- `test_y_reply_publishes_confirmed`.
- `test_n_reply_cancels_no_publish`.

**Manual verification:** one real SMS confirm/cancel round trip. Done via a signed webhook request straight to the deployed `ingest-svc` (same signature scheme a real inbound SMS carries, so it exercises the identical code path) rather than physically texting from a phone — the full real chain (routing → forward → resolver → publish → committer → real Calendar write) was verified for both `Y` and `N`, see `docs/product/status.md`.

---

## Step 10 — Real `resolver-svc` — clarification loop

`agent-contracts.md` §3.2's generic `filled_fields: dict[str, Any]` design doesn't work on Vertex AI's structured output (verified empirically — see its "Resolved gap" note); the real implementation uses a concrete `due_at`-only schema instead, since `due_at` is the only field the extractor's contract ever adds to `missing_fields`. "Multiple missing fields batch into one question" is consequently not a live scenario today — noted as a design narrowing, not a missed test.

Still not built, despite step 9's comment: a correction reply during `AWAITING_CONFIRMATION` (agent-contracts.md §3.2's "cheap heuristic" for field-targeting) — it doesn't reuse this step's due_at-only clarification model cleanly and needs its own pass. Deferred again, explicitly (`resolver_svc/main.py`'s module docstring).

**Acceptance criteria**
- `exchange_count` increments only on outbound questions, never inbound replies (`state-machine.md` §1.2).
- After the 3rd unresolved exchange → `NEEDS_REVIEW`, correct terminal message, no 4th question sent.
- `conversations` row created unconditionally at `EXTRACTED` consumption — verified even for an item that never enters `CLARIFYING` (the zero-clarification path still gets a row, for `due_at` staging).
- Resolved `due_at` lands in `conversations.resolved_fields`, never attempted against an `items` column.

**Unit tests** (`services/resolver-svc/tests/test_clarification.py`, Gemini mocked via a stateful fake DB connection tracking one item across a real multi-call sequence — a plain `MagicMock` can't track state between calls)
- `test_exchange_counting_table` — a full 3-exchange exhaustion sequence, asserting `exchange_count` and `items.state` after every single step, matching `state-machine.md` §1.2 exactly: increments only on sent questions, never on the reply that finally exhausts the budget.
- `test_single_exchange_resolves_to_awaiting_confirmation`.
- `test_due_at_lands_only_in_conversations_never_an_items_column` — asserts directly against every `UPDATE items` call's SQL text across the whole exchange, not just final state.

Also extended (`test_resolver.py`): `test_missing_fields_starts_clarification_not_left_stalled` — step 10 replaces step 9's "left in `EXTRACTED`, do nothing" for incomplete items.

**Integration tests** (`test_resolver_integration.py`, real dev Postgres + real Pub/Sub emulator; Gemini mocked)
- `test_conversations_row_created_on_zero_clarification_path`.
- `test_three_exchange_exhaustion_reaches_needs_review`.
- `test_single_exchange_resolves_to_awaiting_confirmation`.

**Manual verification:** one real multi-turn SMS clarification exchange.

---

## Step 11 — Multimodal ingest

Test files across `ingest-svc`/`extractor-svc` are named `test_ingest_*`/`test_extractor_*` (not the plain `test_media.py`/`test_media_integration.py` this section originally named) — pytest's module-identity check collides on identical basenames across services when the whole suite runs from the repo root in one invocation (neither service's `tests/` has an `__init__.py`, matching this project's convention), found running the full suite after adding these files.

**Acceptance criteria**
- MMS with image/PDF: media persisted to GCS at the correct `raw_media_uri`, correct MIME type, `extractor-svc` passes bytes to Gemini and produces a valid extraction.
- An unsupported attachment type is rejected with a clear error, not silently dropped.
- Text-only path (steps 3–4) has no regression.

**Unit tests**
- `services/ingest-svc/tests/test_ingest_media.py` — supported types (jpeg/png/gif/webp/pdf) processed and stored; unsupported type → 400, no DB write, no download attempt; text-only has no media, no GCS call; a download failure surfaces as 500 before any items row exists (media is fetched *before* the INSERT, not after — a failed download leaves nothing behind for Twilio's retry to duplicate).
- `services/extractor-svc/tests/test_extractor_media.py` — a `media_uri` gets downloaded and passed as a second `Part` alongside the text `Part`; text-only sends a single `Part` with no GCS call; a non-`gs://` URI is rejected outright.

**Integration tests** (real Pub/Sub emulator + the real GCS media bucket; only the Twilio media fetch and the Gemini call are mocked — everything else is genuinely live)
- `services/ingest-svc/tests/test_ingest_media_integration.py::test_mms_stores_media_in_real_gcs_and_publishes` — the uploaded object is read back from real GCS afterward to confirm the bytes actually match, not just that the client method was called.
- `services/extractor-svc/tests/test_extractor_media_integration.py::test_downloads_real_gcs_object_and_extracts` — a real fixture image uploaded directly to GCS, downloaded for real by `extractor-svc`'s own code path.
- Text-only regression: covered by rerunning the full `services/` suite, not a separate test — steps 3/4's existing tests are unmodified and still pass.

**Manual verification:** one real photographed note, one real screenshot, both via actual MMS.

---

## Step 12 — Dedupe via embeddings

Also required, found while building this step for real (not named in the step's one-line description, same category as step 9's ingest-svc routing prerequisite): `ingest-svc`'s open-conversation routing check only listed `CLARIFYING`/`AWAITING_CONFIRMATION`, missing `DUPLICATE_SUSPECTED` — a real gap, since a reply to "is this the same as X?" would otherwise have been misrouted as a brand-new item. Fixed in `ingest-svc/main.py`'s `_open_conversation_item_id()`.

**Acceptance criteria**
- `dedupe_hash` exact-match catches identical resends **without** an embedding API call (assert the mocked embedding client is not invoked in that case).
- `similarity ≥ 0.92` → `DUPLICATE_SUSPECTED`; `Y` → `MERGED`, no new `obligations`/`latents` row; `N` → proceeds as if no match (straight to `AWAITING_CONFIRMATION`, or back into the clarification loop if fields are still missing).
- `0.82–0.92` against an existing latent → thread-attach offer, non-blocking, rides on the eventual confirmation card; `A` reply sets `items.parent_item_id`.
- Below `0.82` → no dedupe action (regression against false positives).

**Unit tests** (`services/resolver-svc/tests/test_dedupe.py` — pure, no DB/embedding API)
- `test_dedupe_hash_normalizes_case_and_whitespace` / `test_dedupe_hash_differs_for_different_content`.
- `test_similarity_boundary_at_0_92` / `test_similarity_boundary_at_0_82` — `classify_match()` at and just past each threshold.
- `test_thread_attach_band_ignored_for_obligation_match`, `test_below_thread_attach_threshold_no_action`, plus `cosine_similarity()` sanity checks (identical/orthogonal/near-duplicate fixture vectors).
- `services/resolver-svc/tests/test_resolver.py` also covers the full routing integration with the DB/Twilio mocked: a duplicate short-circuits before the completeness check even for an item with `missing_fields` set; `Y`/`N` during `DUPLICATE_SUSPECTED`; the `A` (attach) reply during `AWAITING_CONFIRMATION`, with and without a candidate on record.

**Integration tests** (`services/resolver-svc/tests/test_dedupe_integration.py`, real Postgres + pgvector; the embedding call itself is mocked to a controlled fixture vector, matching how `clarify()` is mocked elsewhere)
- `test_exact_hash_match_skips_embedding_call` — a real `dedupe_hash` lookup against a real seeded row.
- `test_near_duplicate_caught` / `test_dissimilar_item_not_caught` — real pgvector `<=>` cosine search against a real seeded `item_embeddings` row.

**Manual verification:** a real near-duplicate obligation, sent as text via a real signed webhook to the deployed `ingest-svc` (matching the established real-infra-verification pattern from steps 9-10 — see status.md for what was actually run and its result).

---

## Step 13 — DLQ + error handling

**Real finding, changes the whole approach to this step's integration tests:** the local Pub/Sub emulator does not implement push redelivery or dead-letter forwarding at all — confirmed empirically (a message left un-acked for 90s against a 5s ack deadline was delivered exactly once, nothing ever forwarded to the `.dlq` topic). It only enforces `max_delivery_attempts>=5` at subscription-*creation* time, which is a different, narrower thing than actually implementing the retry/forward behavior. Verified the real behavior against actual GCP Pub/Sub instead (a throwaway topic, pull-mode to avoid needing a public push endpoint): real Pub/Sub forwards after exactly 5 delivery attempts, the forwarded message's `data` is byte-identical to the original, and it carries `CloudPubSubDeadLetterSourceDeliveryCount` as a string attribute — the real retry count. This is why the integration tests below hit `committer-svc`'s `/pubsub/dlq` endpoint directly with a hand-crafted envelope matching that confirmed real shape, rather than trying to trigger the emulator's (nonexistent) forwarding — and why the full real chain is proven in the manual verification instead.

**Known real finding, addressed here** (found during step 11's live MMS verification, not a step 13 regression): concurrent Pub/Sub redelivery of the same `items-extracted` message during a slow cold start raced `resolver-svc`'s `_start_clarification` and hit `AlreadyExistsError`/500 on ADK's session id collision — harmless there purely by luck. Fixed with a real idempotency guard, not a narrow patch to that one crash.

**Second real finding, found verifying the idempotency guard's own first draft:** a guard keyed on `items.state != 'RECEIVED'` looked right but wasn't — `resolver-svc`'s `_write_item()` commits the state transition in its own transaction, separate from the later `conversations` INSERT. A forced failure between those two writes (a deliberately-invalid `user_id` reference) left a real item stuck at `AWAITING_CONFIRMATION` with zero `conversations` rows and zero `dead_letters` rows — the state-only guard swallowed every redelivery as "already done" forever, so the message never reached 5 delivery attempts at all. Fixed by keying the guard on whether a `conversations` row exists instead (the row `data-model.md` §2.4 already documents as created unconditionally, the instant a success path finishes) — see `state-machine.md` §3 for the full writeup, including the accepted limitation this doesn't close (a Calendar write succeeding before a DB failure in `committer-svc` still isn't idempotency-guarded, since the external call can't be made atomic with the local commit).

**Acceptance criteria**
- Every subscription has a dead-letter policy (`max_delivery_attempts=5` — Pub/Sub's actual minimum, found empirically in step 4; the API rejects anything below 5) pointing at the correct `.dlq` topic (`gcloud pubsub subscriptions describe`).
- A forced technical failure → exactly 5 attempts → `.dlq` message → exactly one `dead_letters` row with correct `item_id`/`stage`/`error`/`retry_count`.
- Bad-input rejections (malformed payload) never appear in `dead_letters` — trivially true by construction, not a new mechanism: the only two "bad input" cases in this system (`ingest-svc`'s Twilio webhook, `resolver-svc`'s `/reply`) are synchronous HTTP endpoints with no Pub/Sub subscription behind them at all, so there's no delivery-attempt count or dead-letter policy in play for either regardless of what they reject. A malformed *internal* Pub/Sub envelope (all three consumers already decode-and-500 on this, since step 4) is deliberately treated as a technical failure, not bad input — internal corruption is a real signal something's broken, not untrusted external data to just drop (`state-machine.md` §3).
- Manual replay (republish stored `payload` to the correct topic) re-enters the pipeline at the failed stage, not from `RECEIVED` — `scripts/replay_dead_letter.py <dead_letter_id>`.
- Idempotency: a redelivered message that already fully completed processing is a no-op, not a reprocess or a duplicate side effect.

**Unit tests:** already satisfied by existing pre-step-13 coverage for the bad-input-vs-technical-failure classification (`ingest-svc/tests/test_webhook.py`'s signature-rejection tests, `resolver-svc/tests/test_resolver.py`'s OTHER-reply test) — see the "real finding" above for why there's no new dedicated mechanism to test. New for this step: `committer-svc/tests/test_committer.py`'s `/pubsub/dlq` tests (writes the row correctly; a malformed dead-letter envelope is acked, not retried) and both services' idempotency-guard tests (including the second real bug's regression test: a stuck state with no conversation row must *not* be swallowed).

**Integration tests** — the emulator can't exercise real forwarding (see above), so these hit `/pubsub/dlq` directly with the real confirmed envelope shape:
- `test_dlq_writes_dead_letter_row_and_marks_failed` (`services/committer-svc/tests/test_committer.py`).
- `test_dlq_malformed_envelope_acked_not_retried`.

**Manual verification:** a real forced technical failure (a deliberately-invalid `user_id` foreign key), published straight to the live `items-extracted` topic, confirmed to reach `items.state='FAILED'` and a real `dead_letters` row after Pub/Sub's real 5-attempt retry against the actually-deployed `resolver-svc`/`committer-svc` — see `status.md` for the two real bugs this surfaced and the final passing result.

---

## Step 14 — Feedback loop / dismissal scoring

**Acceptance criteria**
- `N` reply: `dismissal_count` increments; `<2` → back to `ELIGIBLE`; `==2` → `dormant_until = now() + 30d`.
- `Later` reply: `dormant_until = now() + 7d`, `dismissal_count` unchanged.
- No reply within 24h: `outcome='no_response'`, no penalty, `dormant_until` unchanged, resolved **before** the same run scores any new suggestion.
- `Y` reply: `outcome='accepted'`, converts to obligation via the minimal `items.confirmed` publish (`state-machine.md` §2.3), picked up correctly by step 6's committer.

**Unit tests** (`services/dispatcher-svc/tests/test_feedback.py`)
- `test_outcome_table` — table-driven over every `SURFACED` branch, matching `state-machine.md` §2's diagram exactly.
- `test_24h_timeout_resolves_before_new_scoring`.

**Integration tests**
- `test_accept_path_full_cycle` — `SURFACED` → `Y` → routed via `ingest-svc` → `items.type` flips to `obligation`, `due_at` computed as block-start capped at block length, `items.confirmed` published, `committer-svc` commits it.

---

## Step 15 — Email draft + send action (stretch)

**Not yet specifiable.** The drafting mechanism itself is an open design gap (`agent-contracts.md` §3.2's flagged note) — write the spec first, following the same doc-before-code pattern as everything else, then fill in this section's acceptance criteria and test names. Do not invent tests against an unspecified mechanism.

What's already fixed regardless of the mechanism: the same confirm-gate applies (no send without `Y`), and `email_sent_at` is set exactly once — no duplicate sends on retry or DLQ replay.

---

## Step 16 — Seed demo data script

**Acceptance criteria**
- Running it against a clean dev DB produces: one demo user, one latent backdated to ~18 days old, a full 14-day `capacity_snapshots` history shaped to reproduce the worked-example-style suggestion.
- Refuses to run outside an explicit demo/dev guard (e.g. `ENVIRONMENT=demo`) — never touches real user data.

**Integration test:** `test_seed_then_dispatch_produces_suggestion` — run the script against a scratch DB, run `/dispatch`, assert a suggestion is actually produced. This is the confidence check tying steps 7/8/14 together against realistic data.

---

## Step 17 — Record demo

Not a software step — a pre-flight checklist instead of tests: every "Manual verification" bullet in steps 3–16 has been performed at least once against the real deployed system before recording, so nothing is discovered live on camera.

## Step 18 — README, diagram export, write-up

Checklist, not tests: README spin-up instructions followed once from a genuinely clean environment; diagram matches the actual deployed topology, not an earlier draft.

## Step 19 — Bonus

No acceptance criteria — optional, cut freely.
