"""docs/engineering/test-plan.md step 7 — no I/O, every test here is a
pure computation. capacity-engine.md §6's worked example gives exact
expected numbers; reproduced to 3 decimal places, not approximated."""

from datetime import date, time

import pytest
from dispatcher_svc.capacity_engine import (
    REVIVAL_THRESHOLD,
    CapacitySnapshot,
    Event,
    Interval,
    LatentCandidate,
    block_fit,
    booked_minutes,
    depth_fit,
    fit_score,
    fragmentation_index,
    free_intervals,
    free_minutes,
    is_eligible,
    largest_contiguous_block,
    load_delta,
    load_fit,
    revival_score,
    select_suggestion,
)

WH_START = time(9, 0)
WH_END = time(18, 0)
A_DAY = date(2026, 8, 27)  # Thursday, matches capacity-engine.md §6


def _latent(
    created_at,
    effort_minutes=120,
    focus_depth="deep",
    dismissal_count=0,
    dormant_until=None,
    last_surfaced_at=None,
    has_open_suggestion=False,
):
    return LatentCandidate(
        item_id="x",
        created_at=created_at,
        effort_minutes=effort_minutes,
        focus_depth=focus_depth,
        dismissal_count=dismissal_count,
        dormant_until=dormant_until,
        last_surfaced_at=last_surfaced_at,
        has_open_suggestion=has_open_suggestion,
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


def test_block_fit_deep_requires_125_percent_margin():
    assert block_fit(largest_block=150, effort_minutes=120, focus_depth="deep") == 1
    assert block_fit(largest_block=149, effort_minutes=120, focus_depth="deep") == 0


def test_block_fit_shallow_no_margin_required():
    assert block_fit(largest_block=120, effort_minutes=120, focus_depth="shallow") == 1
    assert block_fit(largest_block=119, effort_minutes=120, focus_depth="shallow") == 0


def test_depth_fit_deep_flat_below_threshold():
    assert depth_fit(frag_index=0.0, focus_depth="deep") == 1.0
    assert depth_fit(frag_index=0.5, focus_depth="deep") == 1.0


def test_depth_fit_deep_falls_off_above_threshold():
    assert depth_fit(frag_index=1.0, focus_depth="deep") == pytest.approx(0.3)
    # halfway between 0.5 and 1.0 → halfway between 1.0 and the 0.3 floor
    assert depth_fit(frag_index=0.75, focus_depth="deep") == pytest.approx(0.65)


def test_depth_fit_shallow_rewards_fragmentation():
    assert depth_fit(frag_index=0.0, focus_depth="shallow") == 1.0
    assert depth_fit(frag_index=1.0, focus_depth="shallow") == pytest.approx(1.2)


def test_load_fit_at_mean_is_half():
    assert load_fit(0.0) == pytest.approx(0.5)


def test_load_fit_40_percent_below_is_one():
    assert load_fit(-0.4) == pytest.approx(1.0)


def test_load_fit_clips_at_bounds():
    assert load_fit(-1.0) == 1.0  # would exceed 1.0 unclipped
    assert load_fit(1.0) == 0.0  # would go negative unclipped


def test_load_fit_cold_start_is_neutral():
    assert load_fit(None) == 0.5


def test_load_delta_cold_start_below_3_days():
    assert load_delta(booked_today=200, trailing_booked_minutes=[100, 200]) is None


def test_load_delta_zero_rolling_mean_empty_baseline_is_neutral():
    """A genuinely empty trailing calendar (rolling_mean=0) would divide
    by zero unhandled — found for real on step 8's first live /dispatch
    run against the demo account's empty Calendar history."""
    assert load_delta(booked_today=0, trailing_booked_minutes=[0] * 14) == 0.0


def test_load_delta_zero_rolling_mean_any_booking_reads_as_maximally_busy():
    delta = load_delta(booked_today=30, trailing_booked_minutes=[0] * 14)
    assert delta == 1.0
    assert load_fit(delta) == 0.0


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


def test_fit_score_worked_example_reproduces_0_875():
    snapshot = CapacitySnapshot(
        date=A_DAY,
        free_minutes=330,
        largest_contiguous_block=180,
        fragmentation_index=0.0,
        load_delta=-0.30,
    )
    fit = fit_score(snapshot, effort_minutes=120, focus_depth="deep")
    assert round(fit, 3) == 0.875


def test_revival_score_worked_example():
    item = _latent(created_at=date(2026, 8, 9))  # 18 days before A_DAY
    snapshot = CapacitySnapshot(
        date=A_DAY,
        free_minutes=330,
        largest_contiguous_block=180,
        fragmentation_index=0.0,
        load_delta=-0.30,
    )
    fit = fit_score(snapshot, item.effort_minutes, item.focus_depth)
    score = revival_score(item, today=A_DAY, fit=fit)
    assert round(score, 3) == 0.633


def test_contrast_example_insufficient_block_scores_zero():
    """capacity-engine.md §6's contrast: 240min deep task can't fit an
    180min block even before considering depth_fit/load_fit."""
    snapshot = CapacitySnapshot(
        date=A_DAY,
        free_minutes=330,
        largest_contiguous_block=180,
        fragmentation_index=0.0,
        load_delta=-0.30,
    )
    fit = fit_score(snapshot, effort_minutes=240, focus_depth="deep")
    assert fit == 0.0


def test_eligibility_excludes_young_items():
    item = _latent(created_at=A_DAY)  # captured today
    assert is_eligible(item, today=A_DAY) is False


def test_eligibility_excludes_dormant():
    item = _latent(created_at=date(2026, 8, 1), dormant_until=date(2026, 9, 1))
    assert is_eligible(item, today=A_DAY) is False


def test_eligibility_excludes_recently_surfaced():
    item = _latent(created_at=date(2026, 8, 1), last_surfaced_at=date(2026, 8, 20))  # 7 days ago
    assert is_eligible(item, today=A_DAY) is False


def test_eligibility_excludes_open_suggestion():
    item = _latent(created_at=date(2026, 8, 1), has_open_suggestion=True)
    assert is_eligible(item, today=A_DAY) is False


def test_selection_picks_best_day_per_latent_then_argmax_across_latents():
    strong_snapshot = CapacitySnapshot(
        date=A_DAY,
        free_minutes=330,
        largest_contiguous_block=180,
        fragmentation_index=0.0,
        load_delta=-0.30,
    )
    weak_snapshot = CapacitySnapshot(
        date=date(2026, 8, 28),
        free_minutes=60,
        largest_contiguous_block=60,
        fragmentation_index=1.0,
        load_delta=0.5,
    )
    strong_candidate = _latent(created_at=date(2026, 8, 9))  # 18 days old, matches worked example
    weak_candidate = _latent(created_at=date(2026, 8, 20), effort_minutes=15, focus_depth="shallow")

    result = select_suggestion(
        [strong_candidate, weak_candidate], [strong_snapshot, weak_snapshot], today=A_DAY
    )
    assert result is not None
    assert result.item is strong_candidate
    assert result.snapshot is strong_snapshot
    assert round(result.score, 3) == 0.633


def test_selection_respects_threshold():
    barely_eligible = _latent(created_at=date(2026, 8, 24))  # 3 days old — passes eligibility...
    weak_snapshot = CapacitySnapshot(
        date=A_DAY,
        free_minutes=200,
        largest_contiguous_block=150,
        fragmentation_index=0.5,
        load_delta=0.0,
    )
    # ...but low recency_decay at 3 days keeps the score under REVIVAL_THRESHOLD
    result = select_suggestion([barely_eligible], [weak_snapshot], today=A_DAY)
    assert result is None


def test_revival_threshold_constant_is_0_4():
    assert REVIVAL_THRESHOLD == 0.4
