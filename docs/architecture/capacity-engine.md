# Capacity Engine

Fourth doc in the architecture set — see `overview.md` §0. This is the differentiator (PRD §1, §6) — the thing that makes a suggestion read as "the system actually looked" rather than a random nudge. Every number here must be reproducible by hand from a `capacity_snapshots` row, per ADR [0005](../decisions/0005-vector-search-scope.md)'s point that resurfacing is arithmetic, not a black-box similarity search.

Owned here: how a `capacity_snapshots` row gets computed from raw Calendar data, and how `dispatcher-svc` turns that plus the `latents` backlog into at most one suggestion per run. Formulas are stated exactly, including the constants the PRD left as prose ("~0.5", "~1.0") — a coding agent implementing this should not have to invent a curve.

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

## 4. Fit score

```
fit_score = block_fit × depth_fit × load_fit
```

### 4.1 `block_fit` — hard gate, 0 or 1

**Resolved inconsistency with the PRD:** PRD §6.2 states the shallow gate as `free_minutes ≥ effort_minutes` (summed across all gaps). That's wrong given `state-machine.md` §2.3, which places an accepted item at the *start of a single contiguous block* — three 10-minute gaps summing to 30 free minutes cannot host one 30-minute event. Both branches below gate on a single contiguous block; only the required margin differs.

```python
def block_fit(largest_contiguous_block: int, effort_minutes: int, focus_depth: str) -> int:
    if focus_depth == "deep":
        return 1 if largest_contiguous_block >= effort_minutes * 1.25 else 0
    else:  # shallow
        return 1 if largest_contiguous_block >= effort_minutes else 0
```

Deep work keeps the 25% headroom margin from the PRD (a 2-hour deep task wants a block noticeably bigger than 2 hours, so it doesn't feel wedged in). Shallow work needs no headroom — it's sized to fit exactly.

### 4.2 `depth_fit` — reward/penalty curve over `fragmentation_index`

```python
def depth_fit(fragmentation_index: float, focus_depth: str) -> float:
    if focus_depth == "deep":
        if fragmentation_index <= 0.5:
            return 1.0
        return max(0.3, 1.0 - (fragmentation_index - 0.5) / 0.5 * 0.7)
    else:  # shallow
        return min(1.2, 1.0 + fragmentation_index * 0.2)
```

Deep work is flat at `1.0` up to moderate fragmentation, then falls linearly to a floor of `0.3` at maximum fragmentation (never `0` — `block_fit` already gated out days where the block is physically too small; a day that clears the gate but is choppy elsewhere is still usable, just less ideal). Shallow work gets a mild reward, up to `1.2`, for landing on a more fragmented day — it's better use of a day that's bad for anything else.

### 4.3 `load_fit` — distance below the personal baseline

PRD §6.2 gives two anchor points in prose: a day at the mean scores `~0.5`, a day 40% below scores `~1.0`. Solved exactly for a line through both points:

```python
def load_fit(load_delta: float) -> float:
    return clip(0.5 - load_delta * 1.25, 0.0, 1.0)
```

Check: `load_delta = 0` → `0.5` ✓. `load_delta = -0.4` → `0.5 + 0.5 = 1.0` ✓. A day 40% *busier* than the user's own baseline (`load_delta = +0.4`) scores `0.0` — effectively excluded without a separate rule, which is correct: "busier than usual" is never the day to add something new, regardless of what the raw calendar looks like.

---

## 5. Revival score and selection

Per PRD §6.3, exactly:

```python
def revival_score(item: Latent, snapshot: CapacitySnapshot, fit: float) -> float:
    days_since_capture = (today - item.created_at).days
    recency_decay = 1 - exp(-days_since_capture / 14)
    dismissal_penalty = 1 / (1 + item.dismissal_count)
    return recency_decay * dismissal_penalty * fit
```

**Eligibility filter**, applied before scoring (state-machine.md §2.1 — repeated here since this is where it's enforced):
- `days_since_capture < 3` → excluded.
- `latents.dormant_until` in the future → excluded.
- `latents.last_surfaced_at` within the last 10 days → excluded.
- an open (`outcome IS NULL`) `suggestions` row already exists for this item → excluded (it's already `SURFACED`, can't be re-surfaced).

**Selection, once per dispatcher run:**

```python
REVIVAL_THRESHOLD = 0.4   # tunable — see rationale below

best = None
for item in eligible_latents:
    for snapshot in next_7_days_snapshots:
        fit = fit_score(snapshot, item.effort_minutes, item.focus_depth)
        score = revival_score(item, snapshot, fit)
        if best is None or score > best.score:
            best = Candidate(item, snapshot, score)

if best is not None and best.score > REVIVAL_THRESHOLD:
    send_suggestion(best.item, best.snapshot)
```

Read literally: for each latent, find *its* best day among the next 7 (this is what PRD §5.3 step 4's "score every eligible latent against the best snapshot" means precisely — "best" is per-latent, not a single day chosen up front). Then take the single highest-scoring `(latent, day)` pair across the whole backlog. If it clears the threshold, that's the one suggestion this run sends — everything else, however close, is silent this cycle.

**On the threshold constant:** `0.4` is a starting point, not a derived value — flagged explicitly as tunable. Reasoning: `recency_decay` alone doesn't cross `~0.63` until an item is 18 days old (see the worked example, §6), so `0.4` requires either meaningful age *or* a very strong fit, not just one or the other. Tune this against real demo data before recording — it is the single knob most likely to need adjustment once you see actual suggestions fire (or fail to).

---

## 6. Worked example

Reproduces the exact suggestion text from PRD §5.3, with real numbers, so this is checkable against Cloud Logging output on camera.

**Setup:** Thursday, working hours 09:00–18:00 (540 min). Calendar: a meeting 09:00–12:00 (merged 3h block) and a meeting 15:00–15:30. Trailing 14-day rolling mean: 300 booked min/day.

**Snapshot computation:**
- Free intervals: `[12:00–15:00]` (180 min), `[15:30–18:00]` (150 min).
- `free_minutes = 330`
- `largest_contiguous_block = 180` (the 12:00–15:00 block)
- `fragmentation_index = 0 / 2 = 0.0` (neither gap is under 45 min)
- `booked_minutes = 540 - 330 = 210`
- `load_delta = (210 - 300) / 300 = -0.30`

**Candidate latent:** "Rewrite the ingest pipeline in Rust" — captured 18 days ago, `effort_minutes = 120`, `focus_depth = deep`, `dismissal_count = 0`.

**Fit:**
- `block_fit`: deep needs `120 × 1.25 = 150` ≤ `180` → `1`
- `depth_fit`: `fragmentation_index = 0.0 ≤ 0.5` → `1.0`
- `load_fit`: `0.5 - (-0.30 × 1.25) = 0.5 + 0.375 = 0.875`
- `fit_score = 1 × 1.0 × 0.875 = 0.875`

**Revival:**
- `recency_decay = 1 - exp(-18/14) = 1 - 0.2765 = 0.7235`
- `dismissal_penalty = 1 / (1 + 0) = 1.0`
- `revival_score = 0.7235 × 1.0 × 0.875 ≈ 0.633`

`0.633 > 0.4` → suggestion fires, into the `12:00–15:00` block on Thursday.

**Evidence line generation** ("lightest day you've had in two weeks"): a separate check from `load_fit` itself — is this day's `booked_minutes` (210) the minimum among the trailing 14 daily values? If yes, use that superlative; otherwise fall back to a plainer phrasing ("Thursday looks lighter than usual"). Two different uses of the same 14-day window: `load_fit` wants a smooth distance-from-mean, the suggestion copy wants a discrete "is this the best one" fact. `agent-contracts.md` owns the exact phrasing rules.

**Contrast — a candidate that doesn't clear the gate:** same day, a latent "Read the Rust book" captured 4 days ago, `effort_minutes = 240`, `deep`. `block_fit`: needs `300`, block is `180` → `0`. `fit_score = 0` regardless of the other factors — excluded from consideration for this day entirely, correctly, since there's nowhere to put 4 hours of deep work today.

---

## 7. Deliberately not built

- Buffer/transition time around meetings (§2).
- Any cross-day lookahead beyond "each latent's best day in the next 7" — e.g. holding a very strong candidate for a slightly better day two weeks out. The 7-day window and one-shot-per-run selection (PRD §5.3) is the whole model; no queueing of near-misses.
- Per-latent custom thresholds. One global `REVIVAL_THRESHOLD` for all items and all users.

## 8. Open items for sibling docs

- ~~Exact SMS rendering of the suggestion and evidence line (including the "lightest day" fallback wording from §6)~~ → done, see `agent-contracts.md` §4.2.
- ~~Which service account runs the Calendar read... `infrastructure.md` should confirm the Calendar API quota this implies~~ → done, see `infrastructure.md` §4 (default quota comfortably covers this).
