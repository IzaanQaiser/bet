"""docs/engineering/test-plan.md step 7 — no I/O, every test here is a
pure computation. capacity-engine.md §6's worked example gives exact
expected numbers; reproduced to 3 decimal places, not approximated.

ADR 0009: revival_score/REVIVAL_THRESHOLD/select_suggestion/is_eligible/
Candidate/fit_score/load_fit are removed along with the batch-scoring
engine — see main.py's _recompute_and_reschedule for what replaced it."""

from datetime import date, time

import pytest
from dispatcher_svc.capacity_engine import (
    Event,
    Interval,
    LatentCandidate,
    block_fit,
    booked_minutes,
    fragmentation_index,
    free_intervals,
    free_minutes,
    largest_contiguous_block,
    load_delta,
)

WH_START = time(9, 0)
WH_END = time(18, 0)
A_DAY = date(2026, 8, 27)  # Thursday, matches capacity-engine.md §6


def _latent(
    effort_minutes=120,
    dismissal_count=0,
    dormant_until=None,
    last_surfaced_at=None,
    has_open_suggestion=False,
    next_fit_start=None,
    placeholder_event_id=None,
):
    return LatentCandidate(
        item_id="x",
        title="Test idea",
        effort_minutes=effort_minutes,
        dismissal_count=dismissal_count,
        dormant_until=dormant_until,
        last_surfaced_at=last_surfaced_at,
        has_open_suggestion=has_open_suggestion,
        next_fit_start=next_fit_start,
        placeholder_event_id=placeholder_event_id,
    )


def test_free_intervals_merges_back_to_back_events():
    events = [
        Event(start=time(9, 0), end=time(10, 0)),
        Event(start=time(10, 0), end=time(11, 0)),  # touches the first — one block, not two
    ]
    intervals = free_intervals(A_DAY, events, WH_START, WH_END)
    assert intervals == [Interval(start=time(11, 0), end=WH_END)]


def test_free_intervals_all_day_event_blocks_whole_day():
    events = [Event(start=time(0, 0), end=time(23, 59), all_day=True)]
    assert free_intervals(A_DAY, events, WH_START, WH_END) == []


def test_free_intervals_excludes_declined_and_transparent():
    events = [
        Event(start=time(9, 0), end=time(10, 0), declined=True),
        Event(start=time(10, 0), end=time(11, 0), transparency="transparent"),
    ]
    intervals = free_intervals(A_DAY, events, WH_START, WH_END)
    assert intervals == [Interval(start=WH_START, end=WH_END)]  # neither event blocks anything


def test_free_intervals_excludes_own_placeholder_by_event_id():
    """capacity-engine.md §5's self-exclusion note: an item's own
    [idea]-tagged placeholder must not count as busy when recomputing
    that same item's own next_fit_start, or it would evict itself from a
    perfectly good slot every time."""
    events = [
        Event(start=time(9, 0), end=time(10, 0), google_event_id="mine"),
        Event(start=time(14, 0), end=time(15, 0), google_event_id="someone-elses"),
    ]
    intervals = free_intervals(A_DAY, events, WH_START, WH_END, exclude_event_id="mine")
    # "mine" excluded (its 9-10 block is free again); the other event still counts as busy.
    assert intervals == [
        Interval(start=WH_START, end=time(14, 0)),
        Interval(start=time(15, 0), end=WH_END),
    ]


def test_free_intervals_still_counts_every_other_events_placeholder():
    """The flip side: every *other* item's placeholder is a real Calendar
    event and stays busy — this is the whole mechanism behind a declined
    idea landing after every already-scheduled one, no cascade needed."""
    events = [Event(start=time(9, 0), end=time(18, 0), google_event_id="someone-elses")]
    intervals = free_intervals(A_DAY, events, WH_START, WH_END, exclude_event_id="mine")
    assert intervals == []


def test_block_fit_one_universal_rule_no_margin():
    # User-directed, v1: "deep work" (focus_depth, depth_fit, the deep
    # margin) removed entirely — one rule for every idea, no distinction.
    assert block_fit(largest_block=120, effort_minutes=120) == 1
    assert block_fit(largest_block=119, effort_minutes=120) == 0


def test_load_delta_cold_start_below_3_days():
    assert load_delta(booked_today=200, trailing_booked_minutes=[100, 200]) is None


def test_load_delta_zero_rolling_mean_empty_baseline_is_neutral():
    """A genuinely empty trailing calendar (rolling_mean=0) would divide
    by zero unhandled — found for real on step 8's first live /dispatch
    run against the demo account's empty Calendar history."""
    assert load_delta(booked_today=0, trailing_booked_minutes=[0] * 14) == 0.0


def test_load_delta_zero_rolling_mean_any_booking_reads_as_maximally_busy():
    assert load_delta(booked_today=30, trailing_booked_minutes=[0] * 14) == 1.0


def test_booked_minutes():
    assert booked_minutes(WH_START, WH_END, free_min=330) == 210  # 540 - 330


def test_worked_example_snapshot_matches_doc_exactly():
    """capacity-engine.md §6's setup, reproduced from raw events."""
    events = [
        Event(start=time(9, 0), end=time(12, 0)),
        Event(start=time(15, 0), end=time(15, 30)),
    ]
    intervals = free_intervals(A_DAY, events, WH_START, WH_END)
    assert free_minutes(intervals) == 330
    assert largest_contiguous_block(intervals) == 180
    assert fragmentation_index(intervals) == 0.0

    booked = booked_minutes(WH_START, WH_END, free_minutes(intervals))
    assert booked == 210
    delta = load_delta(booked, trailing_booked_minutes=[300] * 14)
    assert delta == pytest.approx(-0.30)


def test_latent_candidate_carries_placeholder_state():
    item = _latent(next_fit_start=None, placeholder_event_id="evt-1")
    assert item.placeholder_event_id == "evt-1"
    assert item.title == "Test idea"
