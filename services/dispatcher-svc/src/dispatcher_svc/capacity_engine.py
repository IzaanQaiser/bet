"""Capacity engine — pure functions only, no I/O (docs/architecture/
capacity-engine.md, PRD §6). Every formula here is transcribed exactly
from that doc; if this module and the doc ever disagree, this module is
buggy, not the doc.

dispatcher-svc is the only caller. Nothing in this file talks to
Calendar, Postgres, or Pub/Sub — that's deliberate.

revival_score/REVIVAL_THRESHOLD/select_suggestion/is_eligible/Candidate
(the batch-scoring "at most one suggestion per run" engine) and
fit_score/load_fit (only ever consumed by that engine) are removed —
user-directed, replaced by the auto-scheduled-placeholder model
(capacity-engine.md §5, ADR 0009): every committed latent gets its own
next_fit_start-triggered text, not a single scored pick per sweep."""

from dataclasses import dataclass
from datetime import date, datetime, time


@dataclass(frozen=True)
class Event:
    start: time
    end: time
    all_day: bool = False
    declined: bool = False
    transparency: str = "opaque"  # "opaque" | "transparent"
    google_event_id: str | None = None


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
    title: str
    effort_minutes: int
    dismissal_count: int
    dormant_until: date | None
    last_surfaced_at: date | None
    has_open_suggestion: bool
    next_fit_start: datetime | None
    placeholder_event_id: str | None


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


def free_intervals(
    day: date,
    events: list[Event],
    wh_start: time,
    wh_end: time,
    exclude_event_id: str | None = None,
) -> list[Interval]:
    """capacity-engine.md §2. `day` isn't used in the computation itself —
    the caller has already filtered `events` down to that day; kept as a
    parameter for signature fidelity with the doc and so the real per-day
    Calendar fetch has an obvious place to pass it.

    exclude_event_id (capacity-engine.md §5's self-exclusion note):
    recomputing one latent's own next_fit_start must not see that same
    latent's own [idea]-tagged placeholder as busy — otherwise it would
    evict itself from a perfectly good slot every time. Every *other*
    latent's placeholder is a real Calendar event and is deliberately
    left in `events`, so it's still counted as busy — that's the whole
    mechanism behind a declined idea naturally landing after every
    already-scheduled one, no separate reflow needed."""
    if exclude_event_id is not None:
        events = [e for e in events if e.google_event_id != exclude_event_id]
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
    than 3 days of trailing history. A second, distinct edge case handled
    here: a rolling_mean of exactly 0 (a genuinely empty trailing
    calendar, not just short history) would otherwise divide by zero —
    see capacity-engine.md §3's "Resolved gap" note."""
    if len(trailing_booked_minutes) < 3:
        return None
    rolling_mean = sum(trailing_booked_minutes) / len(trailing_booked_minutes)
    if rolling_mean == 0:
        return 0.0 if booked_today == 0 else 1.0
    return (booked_today - rolling_mean) / rolling_mean


def block_fit(largest_block: int, effort_minutes: int) -> int:
    # User-directed, v1: one universal rule for every idea, no deep/
    # shallow distinction — a block is a fit if it's at least as long as
    # the estimate, full stop. depth_fit and the deep-work margin
    # (capacity-engine.md §4.1/§4.2) are removed entirely as unnecessary
    # complexity, along with focus_depth itself.
    return 1 if largest_block >= effort_minutes else 0


