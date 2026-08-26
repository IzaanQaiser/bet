# Capacity Engine

Fourth doc in the architecture set — see `overview.md` §0. This is the differentiator (PRD §1, §6) — the thing that makes a suggestion read as "the system actually looked" rather than a random nudge. Every number here must be reproducible by hand from a `capacity_snapshots` row, per ADR [0005](../decisions/0005-vector-search-scope.md)'s point that resurfacing is arithmetic, not a black-box similarity search.

Owned here: how a `capacity_snapshots` row gets computed from raw Calendar data, and how `dispatcher-svc` turns that plus the `latents` backlog into a real, tagged placeholder event and a fire-time text for every eligible idea (ADR [0009](../decisions/0009-tentative-placeholder-write-before-confirm.md), §5) — not, as an earlier revision of this doc had it, a scored "at most one suggestion per run" pick. Formulas are stated exactly, including the constants the PRD left as prose ("~0.5", "~1.0") where they're still load-bearing (§3's `load_delta`) — a coding agent implementing this should not have to invent a curve.

---

## 1. Inputs, per dispatcher run

- The next 7 days of the user's Google Calendar (for scoring candidate days).
- The trailing 14 calendar days of the user's Calendar, ending today (for the `load_delta` baseline — see §3).
- `users.working_hours_start` / `working_hours_end` / `timezone`.
- Every row in `latents` joined to `items` where `items.type = 'latent'` and `items.state = 'COMMITTED'` (candidates — filtered further in §5).

All time arithmetic happens in the user's local timezone, converted at the Calendar API boundary. A day is always the user's local calendar day, not UTC.

**Resolved gap, found in step 8: are "next 7 days" and "trailing 14 days" inclusive of today?** Neither phrase says. Decided: both windows include today — the forward window is `[today, today+6]`, the trailing window is `[today-13, today]`. Today is a legitimate scoring candidate (the dispatcher runs at both 7am, before the working day starts, and 1pm, mid-day — a live demo trigger at any hour should still be able to surface a same-day suggestion), and "ending today" in the trailing-window phrasing reads most literally as today being the last of the 14 days, not the 14 days strictly before it. One consequence, accepted rather than engineered around: when today itself is being scored as a forward candidate, its own `booked_minutes` is also one of the 14 values feeding its `load_delta` baseline — a minor self-reference, not worth a special case for a single-digit percentage effect on one metric.

---

## 2. Building free/busy intervals from Calendar events

Before any metric is computed, each candidate day (and each of the 14 baseline days) is reduced to a list of **free intervals** within `[working_hours_start, working_hours_end]`.

1. Fetch events overlapping that day's working-hours window.
2. **Exclude**: declined events (`attendee.responseStatus == 'declined'` for the user), events with `transparency: 'transparent'` (explicitly marked "free" — the user chose not to have this block time).
3. **All-day events block the entire working-hours window for that day** (`free_minutes = 0`, no further computation needed). Decision made here, not in the PRD: an all-day event is far more often "PTO" / "out of office" than a harmless reminder, and suggesting deep work into a declared day off is a worse failure than occasionally missing a valid resurfacing day. No attempt is made to distinguish all-day event *types* — out of scope for the 9-day build.
4. Clip remaining events to the working-hours window (an event starting before `working_hours_start` or ending after `working_hours_end` is truncated at the boundary).
5. Sort the clipped busy intervals by start time and **merge** any that overlap or touch (`start_i <= end_{i-1}`) into a single busy interval — back-to-back meetings are one block, not two blocks with a zero-length gap between them.
6. **Free intervals** = the complement of the merged busy intervals within `[working_hours_start, working_hours_end]`.

```python
def free_intervals(day: date, events: list[Event], wh_start: time, wh_end: time) -> list[Interval]:
    if any(e.all_day for e in events):
        return []
    busy = sorted(
        clip(e, wh_start, wh_end)
        for e in events
        if not e.declined and e.transparency != "transparent"
    )
    merged = merge_overlapping(busy)          # standard interval-merge
    return complement(merged, wh_start, wh_end)
```

No buffer/transition time is subtracted around meetings (e.g. a 10-minute pad before/after). Deliberately not built — a real UX nicety, but not load-bearing for the demo or the scoring model; noted in §7 as a cut item, not silently missing.

**Added, v1, user-directed: today's effective `working_hours_start` carries a 30-minute suggestion lead.** A different concern from the meeting-padding buffer above (that one's about spacing around events; this one's about not suggesting a start time that's already gone, or too soon to react to). For the candidate day equal to *today* only, `working_hours_start` is replaced with `min(max(working_hours_start, now + 30min), working_hours_end)` before free/busy computation runs — every day after today is already entirely in the future, so this can never bind for them, and no separate code path is needed. If the buffered start would land past `working_hours_end` (e.g. it's 5:45pm and the working day ends at 6pm), the clamp collapses the day to zero free minutes rather than producing an invalid `start > end` pair — correctly, since there's no real remaining window to suggest into. Applied both to the original per-run scoring pass (`/dispatch`) and to the accept-path's real-current-availability re-check (§5's own note on why that re-check exists) — a reply arriving late enough that the originally-suggested slot has already passed must not schedule into the past either.

---

## 3. Snapshot metrics

Computed once per candidate day, written to one `capacity_snapshots` row (`data-model.md` §2).

| Metric | Computation |
|---|---|
| `free_minutes` | `sum(duration(i) for i in free_intervals)` |
| `largest_contiguous_block` | `max(duration(i) for i in free_intervals, default=0)` |
| `fragmentation_index` | `count(i in free_intervals if duration(i) < 45) / count(free_intervals)`, or `0` if `free_intervals` is empty |
| `load_delta` | `(booked_minutes(day) - rolling_mean) / rolling_mean` |

Where `booked_minutes(day) = (working_hours_end - working_hours_start in minutes) - free_minutes`, and:

```python
rolling_mean = mean(booked_minutes(d) for d in trailing_14_days)
```

**Cold start:** if fewer than 3 days of Calendar history exist (brand-new account, Calendar API returns nothing further back), `load_delta` is undefined and `load_fit` (§4) defaults to `0.5` (neutral — neither rewards nor penalizes) until 3+ days of history accumulate. 3 was chosen, not 14, because Calendar history predates the user texting the bot at all — real accounts will almost never hit this path; it exists only to keep the very first run well-defined.

**Resolved gap, found in step 8's real deploy: `rolling_mean = 0`.** A distinct edge case from cold start — the trailing 14 days exist and were read, they're just genuinely empty (a brand-new demo Calendar with zero events yet), so `rolling_mean` is exactly `0` and the formula's division is undefined. Hit for real on the very first live `/dispatch` run, not a hypothetical. Decided: `load_delta = 0.0` if today is also empty (`booked_today = 0` — matches the baseline exactly, neutral, same as any other `load_delta = 0` day), otherwise `load_delta = 1.0` (any booked time against a fully-empty baseline reads as maximally busier-than-usual, which `load_fit` already clips to its `0.0` floor for any `load_delta ≥ 0.4` — so `1.0` isn't an arbitrary sentinel, it's just comfortably past that clip point).

---

## 4. Block fit — the whole scoring model, v1

**Removed, user-directed, twice over:**
- `focus_depth` and everything derived from it (separate margins, `depth_fit`'s reward/penalty curve over `fragmentation_index`) — judged too verbose for v1, stripped from every layer.
- `revival_score`/`REVIVAL_THRESHOLD`/`select_suggestion`/`load_fit`/`fit_score` — the entire batch-scoring, "at most one suggestion per run" engine (formerly this section) is gone, replaced by ADR [0009](../decisions/0009-tentative-placeholder-write-before-confirm.md)'s auto-scheduled-placeholder model, §5 below. `fragmentation_index` and `load_delta` are still computed and persisted to `capacity_snapshots` (§3) — nothing else in this codebase reads them anymore, but they're kept as-is rather than removed, since `capacity_snapshots` remains a real per-day record of the user's calendar shape, independent of whatever consumes it.

What's left is a single hard gate, unchanged from before:

```python
def block_fit(largest_contiguous_block: int, effort_minutes: int) -> int:
    return 1 if largest_contiguous_block >= effort_minutes else 0
```

**Resolved inconsistency with the PRD:** PRD §6.2 originally stated the gate as `free_minutes ≥ effort_minutes` (summed across all gaps). That's wrong given `state-machine.md` §2.3, which places an item at the *start of a single contiguous block* — three 10-minute gaps summing to 30 free minutes cannot host one 30-minute event. The gate above is on a single contiguous block, with no margin — one universal rule for every item, v1.

---

## 5. Auto-scheduled placeholders (ADR 0009)

Every committed, non-dormant latent gets a real, tagged `[idea] {title}` event written to the user's **main** Google Calendar at its own `next_fit_start` — the earliest day/time in the next 7 whose `largest_contiguous_block` clears `block_fit` for that item's `effort_minutes`. At the exact instant that slot arrives, the user is texted; **Y** promotes the same placeholder event in place (tag removed, real event); **N**/**Later** clears it and reschedules to the next available slot. There is no scoring, no threshold, no "at most one per run" — every eligible idea gets its own slot and its own text, independently.

### 5.1 `_next_fitting_slot` — per-item, self-excluding, earliest-fitting

```python
def _next_fitting_slot(forward_events, tz, wh_start, wh_end, now_local, today,
                        effort_minutes, exclude_event_id) -> datetime | None:
    for d in sorted(forward_events):
        day_wh_start = buffered_wh_start(wh_start, wh_end, now_local) if d == today else wh_start
        intervals = free_intervals(d, forward_events[d], day_wh_start, wh_end, exclude_event_id)
        for interval in intervals:  # already chronological — first fit wins
            if block_fit(interval_duration, effort_minutes):
                return datetime.combine(d, interval.start, tzinfo=tz)
    return None
```

`exclude_event_id` is the self-exclusion fix: an item's own existing placeholder is a real Calendar event, so without excluding it by id, recomputing that same item's slot would see its own placeholder as busy and needlessly evict itself every time it's recomputed. Every *other* item's placeholder is deliberately left in `forward_events` and still counts as busy — this is the entire mechanism behind a declined idea landing after every already-scheduled one (§5.3), with no cross-item cascade or reflow bookkeeping required.

**Real bug, found live, days after this shipped:** the first version picked the day's *largest* free interval (`max(intervals, key=duration)`), not its *earliest fitting* one. A user's real 2-hour idea got scheduled into a 3-hour gap at 8pm instead of the 2h36m gap at 5pm that already comfortably fit it, purely because 8pm's gap happened to be bigger. `free_intervals` already returns intervals in chronological order (built by walking busy time forward), so "earliest fitting" is just "first interval in the list whose duration clears `block_fit`" — no re-sort needed, and no reason it should ever have picked anything else. `_accept_suggestion` (§5.4) had the identical bug on the Y-path, which was worse: it could silently commit the real obligation to a different time than what the fire-time text actually said, if some other part of the day had a bigger gap than the one just accepted. Fixed there too — try the earliest interval that fits the *original* `effort_minutes` first; only fall back to "whatever's biggest, capped down" (the pre-existing, still-correct "never refuse an explicit Y" behavior) when nothing on the day fully fits.

### 5.2 The write-boundary problem, and how it's resolved

Writing a placeholder happens **before** user confirmation — a deliberate, narrow departure from the "never write on inference alone" invariant (ADR 0003), recorded in ADR 0009 rather than made silently. The invariant that actually matters — exactly one service ever calls the Calendar write API — is preserved: `committer-svc` stays the sole writer. `dispatcher-svc` (which owns all Calendar *reads* and all `next_fit_start` computation) requests the write via a new synchronous call, `PUT /latents/{item_id}/placeholder` / `DELETE /latents/{item_id}/placeholder` on committer-svc — the second synchronous cross-service asymmetry in this codebase (the first being `ingest-svc`'s direct forward to resolver-svc/dispatcher-svc, `overview.md` §2). A synchronous call, not Pub/Sub, because dispatcher-svc needs the real Calendar event id back immediately, to persist into `latents.placeholder_event_id`.

`PUT` is a single upsert: `existing_event_id=None` creates; a real id tries to `PATCH` that event in place, falling back to a fresh create if it 404s (the user deleted it by hand). Committer-svc's Calendar-write helpers (`_create_placeholder_event`/`_move_placeholder_event`) are siblings of the existing `_write_calendar_event`, tagging the summary `[idea] {title}` and a fixed description ("Auto-scheduled — you'll get a text when it's time.").

### 5.3 `_recompute_and_reschedule` — the one writer of `next_fit_start`

Every writer of `latents.next_fit_start`/`placeholder_event_id` (initial commit, a working-hours change, the twice-daily sweep, a post-decline reschedule) goes through one function. It diffs the freshly computed slot against the value already stored and only touches the Calendar/Cloud Tasks queue when it actually changed:

- **Unchanged** → pure no-op, no Calendar call, no Cloud Task.
- **A new slot found** → `PUT .../placeholder` (upsert, §5.2), persist the returned event id + slot, then enqueue a Cloud Task for `POST /latents/{item_id}/fire` at exactly that instant (reusing the `reminders` queue — already multi-purpose — not a new one).
- **No slot fits anywhere in the 7-day window** → `DELETE .../placeholder` if one existed, both columns go `NULL`.

This bounds Cloud Tasks volume to "once per real slot change" and guarantees at most one *live* fire-task per latent at any time. An old, superseded task is never actively cancelled — Cloud Tasks has no clean primitive for that here — it's instead a guaranteed no-op when it fires: `/latents/{item_id}/fire` compares the `scheduled_for` it was enqueued with against the item's *current* `next_fit_start`, and skips silently (`stale_task_skipped`) on any mismatch.

### 5.4 Firing, and the Y/N/Later outcomes

`POST /latents/{item_id}/fire` (dispatcher-svc), on a real Cloud Task at the scheduled instant:
1. No-op if the item is no longer a committed latent, or the task is stale (5.3).
2. No-op if an open `suggestions` row already exists for it (never re-text mid-conversation).
3. **Re-verify** the block is still actually free right now — a single-day Calendar re-fetch, same "don't trust a possibly-stale value" pattern the accept path already used before ADR 0009. If the slot's gone (a real meeting landed on it since), silently recompute + reschedule via §5.3 — no SMS, since nothing's been asked yet.
4. Otherwise: send the text (`render_fire_suggestion`, agent-contracts.md §4.2), open a `suggestions` row (`snapshot_id` left `NULL` — no batch-scoring snapshot exists for this path anymore, `scheduled_for` carries the instant instead — `migrations/0020`).

From there, the **existing** Y/N/Later `/reply` machinery is reused almost entirely unchanged (`state-machine.md` §2.2/§2.3):
- **Y** — re-verifies real current availability (excluding the item's own placeholder, §5.1), publishes `ConfirmedItemMessage` to `items.confirmed` exactly as before. committer-svc's `_commit_obligation` now checks for an existing `placeholder_event_id` first and promotes that same event via `PATCH` (tag/description stripped, real title/time set) instead of `POST`ing a duplicate — the placeholder columns are cleared in the same transaction.
- **N, first dismissal** (`dismissal_count` about to become `< 2`) — **reschedules immediately** via §5.3, user-directed: a single decline isn't a "don't ask again for a while" signal on its own. Reply text (`render_deferred`) names the new day, or apologizes if nothing fit.
- **N, second dismissal** (`dismissal_count` reaches `2`) — unchanged 30-day `dormant_until`, but now also clears the placeholder (`DELETE`) rather than leaving a stale tagged event sitting on the calendar for a month.
- **Later** — unchanged 7-day `dormant_until`, no dismissal penalty, placeholder cleared the same way.

### 5.5 Eligibility, reconsidered without a scorer

`_eligible_latents` (dispatcher-svc/main.py) now returns every committed latent that isn't currently dormant — the SQL filters `dormant_until IS NULL OR dormant_until <= now()` directly, since there's no scorer left to gate anything downstream of that. Two of the old scoring-era gates are gone entirely, because they'd now be actively wrong:
- `days_since_capture < 3` — removed. There's no scoring left to protect from a too-young item; suppressing a fresh idea's own first slot would directly contradict "schedule it for the next eligible slot."
- `last_surfaced_at` within 10 days — removed. A first-dismissal reschedule is very often *inside* that window by design (§5.4) — suppressing it there would break the reschedule this whole model exists to provide.

`has_open_suggestion` stays, unchanged — never recompute or re-text something already mid-conversation. `dormant_until` stays too, but its role changed: instead of being checked at scoring time, a dormant item is simply absent from `_eligible_latents`'s result set, so no sweep ever touches it — no floor-clipping logic needed in the slot search itself. It reappears, and gets a real recompute, the moment `dormant_until` passes (a plain timestamp comparison, no job).

---

## 6. Deliberately not built

- Buffer/transition time around meetings (§2).
- Any cross-day lookahead beyond "the next fitting slot in 7 days" — e.g. holding a very strong candidate for a slightly better day two weeks out.
- Any global rate limit on how many ideas can be pre-scheduled/texted across a day. Restraint was the deliberate design of the pre-ADR-0009 engine (`REVIVAL_THRESHOLD`, one suggestion per run); ADR 0009 explicitly replaces that with "every eligible idea gets its own slot, independently" — a user with many committed ideas will get proportionally many placeholder events and, over time, many texts, one per idea's own slot. Not engineered around; noted here so it isn't mistaken for an oversight.

## 7. Open items for sibling docs

- ~~Exact SMS rendering of the fire-time suggestion~~ → done, see `agent-contracts.md` §4.2.
- ~~Which service account runs the Calendar read... `infrastructure.md` should confirm the Calendar API quota this implies~~ → done, see `infrastructure.md` §4 (default quota comfortably covers this).
- ~~How dispatcher-svc gets a placeholder written given only committer-svc holds Calendar write credentials~~ → done, see ADR 0009 and §5.2 above.
