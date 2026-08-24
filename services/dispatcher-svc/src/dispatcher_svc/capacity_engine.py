"""Capacity engine — pure functions only, no I/O (docs/architecture/
capacity-engine.md, PRD §6). Every formula here is transcribed exactly
from that doc; if this module and the doc ever disagree, this module is
buggy, not the doc.

dispatcher-svc (step 8) is the only caller: it fetches real Calendar
events and latents, builds the dataclasses below, and calls
select_suggestion() once per run. Nothing in this file talks to Calendar,
Postgres, or Pub/Sub — that's deliberate (capacity-engine.md §0, "no
service, no deploy" for this step).
"""

from dataclasses import dataclass
from datetime import date, time
from math import exp

REVIVAL_THRESHOLD = 0.4  # tunable — capacity-engine.md §5's "on the threshold constant"


@dataclass(frozen=True)
class Event:
    start: time
    end: time
    all_day: bool = False
    declined: bool = False
    transparency: str = "opaque"  # "opaque" | "transparent"


@dataclass(frozen=True)
class Interval:
    start: time
    end: time

    @property
    def duration_minutes(self) -> int:
        return _to_minutes(self.end) - _to_minutes(self.start)


@dataclass(frozen=True)
class CapacitySnapshot:
    date: date
    free_minutes: int
    largest_contiguous_block: int
    fragmentation_index: float
    load_delta: float | None  # None = cold start, capacity-engine.md §3


@dataclass(frozen=True)
class LatentCandidate:
    item_id: str
    created_at: date  # "days_since_capture" is measured from here, §5
    effort_minutes: int
    focus_depth: str
    dismissal_count: int
    dormant_until: date | None
    last_surfaced_at: date | None
    has_open_suggestion: bool


@dataclass(frozen=True)
class Candidate:
    item: LatentCandidate
    snapshot: CapacitySnapshot
    score: float


def _to_minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def _clip(event: Event, wh_start: time, wh_end: time) -> Interval:
    return Interval(start=max(event.start, wh_start), end=min(event.end, wh_end))


def _merge_overlapping(intervals: list[Interval]) -> list[Interval]:
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda i: _to_minutes(i.start))
    merged = [ordered[0]]
    for current in ordered[1:]:
        last = merged[-1]
        if _to_minutes(current.start) <= _to_minutes(last.end):
            if _to_minutes(current.end) > _to_minutes(last.end):
                merged[-1] = Interval(start=last.start, end=current.end)
        else:
            merged.append(current)
    return merged


def _complement(merged: list[Interval], wh_start: time, wh_end: time) -> list[Interval]:
    free = []
    cursor = wh_start
    for busy in merged:
        if _to_minutes(busy.start) > _to_minutes(cursor):
            free.append(Interval(start=cursor, end=busy.start))
        if _to_minutes(busy.end) > _to_minutes(cursor):
            cursor = busy.end
    if _to_minutes(cursor) < _to_minutes(wh_end):
        free.append(Interval(start=cursor, end=wh_end))
    return free


def free_intervals(day: date, events: list[Event], wh_start: time, wh_end: time) -> list[Interval]:
    """capacity-engine.md §2. `day` isn't used in the computation itself —
    the caller has already filtered `events` down to that day; kept as a
    parameter for signature fidelity with the doc and so step 8's real
    per-day Calendar fetch has an obvious place to pass it."""
    if any(e.all_day for e in events):
        return []
    busy = [
        _clip(e, wh_start, wh_end)
        for e in events
        if not e.declined and e.transparency != "transparent"
    ]
    # An event entirely outside working hours clips to a zero/negative-length
    # interval — not addressed in the doc's simplified pseudocode, drop it
    # rather than let it corrupt the merge/complement step.
    busy = [i for i in busy if i.duration_minutes > 0]
    return _complement(_merge_overlapping(busy), wh_start, wh_end)


def free_minutes(intervals: list[Interval]) -> int:
    return sum(i.duration_minutes for i in intervals)


def largest_contiguous_block(intervals: list[Interval]) -> int:
    return max((i.duration_minutes for i in intervals), default=0)


def fragmentation_index(intervals: list[Interval]) -> float:
    if not intervals:
        return 0.0
    fragmented = sum(1 for i in intervals if i.duration_minutes < 45)
    return fragmented / len(intervals)


def booked_minutes(wh_start: time, wh_end: time, free_min: int) -> int:
    return (_to_minutes(wh_end) - _to_minutes(wh_start)) - free_min


def load_delta(booked_today: int, trailing_booked_minutes: list[int]) -> float | None:
    """capacity-engine.md §3's cold-start rule: undefined (None) with fewer
    than 3 days of trailing history."""
    if len(trailing_booked_minutes) < 3:
        return None
    rolling_mean = sum(trailing_booked_minutes) / len(trailing_booked_minutes)
    return (booked_today - rolling_mean) / rolling_mean


def block_fit(largest_block: int, effort_minutes: int, focus_depth: str) -> int:
    if focus_depth == "deep":
        return 1 if largest_block >= effort_minutes * 1.25 else 0
    return 1 if largest_block >= effort_minutes else 0


def depth_fit(frag_index: float, focus_depth: str) -> float:
    if focus_depth == "deep":
        if frag_index <= 0.5:
            return 1.0
        return max(0.3, 1.0 - (frag_index - 0.5) / 0.5 * 0.7)
    return min(1.2, 1.0 + frag_index * 0.2)


def load_fit(delta: float | None) -> float:
    if delta is None:
        return 0.5  # cold start, capacity-engine.md §3 — neutral, not a penalty
    return min(1.0, max(0.0, 0.5 - delta * 1.25))


def fit_score(snapshot: CapacitySnapshot, effort_minutes: int, focus_depth: str) -> float:
    return (
        block_fit(snapshot.largest_contiguous_block, effort_minutes, focus_depth)
        * depth_fit(snapshot.fragmentation_index, focus_depth)
        * load_fit(snapshot.load_delta)
    )


def revival_score(item: LatentCandidate, today: date, fit: float) -> float:
    days_since_capture = (today - item.created_at).days
    recency_decay = 1 - exp(-days_since_capture / 14)
    dismissal_penalty = 1 / (1 + item.dismissal_count)
    return recency_decay * dismissal_penalty * fit


def is_eligible(item: LatentCandidate, today: date) -> bool:
    """capacity-engine.md §5's eligibility filter, applied before scoring."""
    days_since_capture = (today - item.created_at).days
    if days_since_capture < 3:
        return False
    if item.dormant_until is not None and item.dormant_until > today:
        return False
    if item.last_surfaced_at is not None and (today - item.last_surfaced_at).days < 10:
        return False
    if item.has_open_suggestion:
        return False
    return True


def select_suggestion(
    latents: list[LatentCandidate], snapshots: list[CapacitySnapshot], today: date
) -> Candidate | None:
    """capacity-engine.md §5: for each eligible latent, find its own best
    day among the given snapshots; then take the single highest-scoring
    (latent, day) pair across the whole backlog, if it clears
    REVIVAL_THRESHOLD. At most one suggestion per call, matching one
    dispatcher run producing at most one suggestion."""
    best: Candidate | None = None
    for item in latents:
        if not is_eligible(item, today):
            continue
        item_best: Candidate | None = None
        for snapshot in snapshots:
            fit = fit_score(snapshot, item.effort_minutes, item.focus_depth)
            score = revival_score(item, today, fit)
            if item_best is None or score > item_best.score:
                item_best = Candidate(item=item, snapshot=snapshot, score=score)
        if item_best is not None and (best is None or item_best.score > best.score):
            best = item_best
    if best is not None and best.score > REVIVAL_THRESHOLD:
        return best
    return None
