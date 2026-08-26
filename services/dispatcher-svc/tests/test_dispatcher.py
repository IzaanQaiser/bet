"""docs/engineering/test-plan.md step 8 — DB and Twilio mocked, one
helper function at a time. Suggestion/reminder *text* is covered in
test_templates.py (split out since it's pure and unrelated to the DB/SMS
orchestration tested here) — test-plan.md names those tests under this
file's plan, reorganized for a cleaner separation of concerns."""

from datetime import UTC, date, datetime, time
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from dispatcher_svc.capacity_engine import CapacitySnapshot, Event, Interval, LatentCandidate
from dispatcher_svc.main import (
    DayComputation,
    _buffered_wh_start,
    _compute_day,
    _eligible_latents,
    _next_fitting_slot,
    _send_reminders,
    _send_suggestion,
    _update_next_fit_slots,
)

TZ = ZoneInfo("America/Toronto")
A_DAY = date(2026, 8, 27)
WH_START = time(9, 0)
WH_END = time(18, 0)


def _mock_connection(fetchall_result=None, fetchone_result=None):
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = fetchall_result or []
    conn.execute.return_value.fetchone.return_value = fetchone_result
    return conn


def _mock_reminder_connection(early_rows=None, final_rows=None):
    """Two independent SELECTs now, not one (reminder_1_at/reminder_2_at
    replaced the old single reminder_window_hours/reminder_sent_at) — a
    uniform fetchall_result can't distinguish them, so this keys off which
    reminder slot each query's SQL text mentions, same SQL-aware side_effect
    pattern resolver-svc's own tests already use for a multi-query function."""

    def execute_side_effect(sql, params=None):
        result = MagicMock()
        if "SELECT" in sql and "reminder_1" in sql:
            result.fetchall.return_value = early_rows or []
        elif "SELECT" in sql and "reminder_2" in sql:
            result.fetchall.return_value = final_rows or []
        return result

    conn = MagicMock()
    conn.execute.side_effect = execute_side_effect
    return conn


def test_reminder_not_resent_if_already_sent():
    """The SQL's own `reminder_N_sent_at IS NULL` clause is what enforces
    this for real (verified against live Postgres in the integration
    test) — here, an empty result set (what that clause produces once a
    reminder slot has already fired) must send nothing for that slot."""
    conn = _mock_reminder_connection(early_rows=[], final_rows=[])
    with patch("dispatcher_svc.main._send_sms") as mock_sms:
        sent = _send_reminders(conn, "user-1", "+15551234567", datetime.now(UTC), TZ)
    assert sent == 0
    mock_sms.assert_not_called()


def test_early_reminder_sent_marks_reminder_1_sent_at():
    due_at = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    conn = _mock_reminder_connection(early_rows=[("item-1", "Pay rent", due_at, False)])
    with patch("dispatcher_svc.main._send_sms") as mock_sms:
        sent = _send_reminders(conn, "user-1", "+15551234567", datetime.now(UTC), TZ)
    assert sent == 1
    mock_sms.assert_called_once()
    call_kwargs = mock_sms.call_args.kwargs
    assert call_kwargs["to"] == "+15551234567"
    assert "Pay rent" in call_kwargs["body"]
    assert "heads up" in call_kwargs["body"]

    update_calls = [c for c in conn.execute.call_args_list if "UPDATE obligations" in c.args[0]]
    assert len(update_calls) == 1
    assert "reminder_1_sent_at" in update_calls[0].args[0]
    assert update_calls[0].args[1] == ("item-1",)


def test_final_reminder_sent_marks_reminder_2_sent_at():
    due_at = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    conn = _mock_reminder_connection(final_rows=[("item-1", "Pay rent", due_at, False)])
    with patch("dispatcher_svc.main._send_sms") as mock_sms:
        sent = _send_reminders(conn, "user-1", "+15551234567", datetime.now(UTC), TZ)
    assert sent == 1
    mock_sms.assert_called_once()
    call_kwargs = mock_sms.call_args.kwargs
    assert "last call" in call_kwargs["body"]

    update_calls = [c for c in conn.execute.call_args_list if "UPDATE obligations" in c.args[0]]
    assert len(update_calls) == 1
    assert "reminder_2_sent_at" in update_calls[0].args[0]
    assert update_calls[0].args[1] == ("item-1",)


def test_scheduled_event_gets_event_templates_not_task_templates():
    """Real bug, found live: a meeting used the task-shaped templates
    ("last call... start now") and never got reminded at its own start
    time. is_scheduled_event routes both reminder slots to the event
    templates instead."""
    due_at = datetime(2026, 8, 25, 20, 39, tzinfo=UTC)
    conn = _mock_reminder_connection(
        early_rows=[("item-1", "Meeting", due_at, True)],
        final_rows=[("item-1", "Meeting", due_at, True)],
    )
    with patch("dispatcher_svc.main._send_sms") as mock_sms:
        sent = _send_reminders(conn, "user-1", "+15551234567", datetime.now(UTC), TZ)
    assert sent == 2
    bodies = [c.kwargs["body"] for c in mock_sms.call_args_list]
    assert any("starts" in b for b in bodies)
    assert any("starting now" in b for b in bodies)
    assert not any("last call" in b or "Block off" in b for b in bodies)


def test_both_reminders_can_fire_in_the_same_run():
    """An obligation due soon enough can have both thresholds already
    passed by the time a /dispatch run finds it — same forgiving "better
    late than never" semantics the old single reminder always had."""
    due_at = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    conn = _mock_reminder_connection(
        early_rows=[("item-1", "Pay rent", due_at, False)],
        final_rows=[("item-1", "Pay rent", due_at, False)],
    )
    with patch("dispatcher_svc.main._send_sms") as mock_sms:
        sent = _send_reminders(conn, "user-1", "+15551234567", datetime.now(UTC), TZ)
    assert sent == 2
    assert mock_sms.call_count == 2


def test_compute_day_reproduces_worked_example():
    events = [
        Event(start=time(9, 0), end=time(12, 0)),
        Event(start=time(15, 0), end=time(15, 30)),
    ]
    computation = _compute_day(events, A_DAY, WH_START, WH_END, [300] * 14)
    assert computation.booked == 210
    assert round(computation.snapshot.load_delta, 2) == -0.30
    assert computation.largest_interval == Interval(start=time(12, 0), end=time(15, 0))


# --- SUGGESTION_LEAD buffer (v1, user-directed) ---------------------------
# Never suggest starting an idea in the past or with less than 30 minutes'
# notice — applied by clipping today's effective working-hours start up
# to (now + 30min), which naturally no-ops for every day after today.


def test_buffered_wh_start_noop_when_plenty_of_runway():
    now_local = datetime(2026, 8, 27, 8, 0, tzinfo=TZ)  # 8am, wh_start is 9am
    assert _buffered_wh_start(WH_START, WH_END, now_local) == WH_START


def test_buffered_wh_start_pushes_start_forward_mid_day():
    now_local = datetime(2026, 8, 27, 9, 15, tzinfo=TZ)  # 9:15am + 30min = 9:45am
    assert _buffered_wh_start(WH_START, WH_END, now_local) == time(9, 45)


def test_buffered_wh_start_clamps_to_wh_end_late_in_the_day():
    # 5:45pm + 30min = 6:15pm, past wh_end (6pm) — clamped, not left invalid.
    now_local = datetime(2026, 8, 27, 17, 45, tzinfo=TZ)
    assert _buffered_wh_start(WH_START, WH_END, now_local) == WH_END


def test_compute_day_with_buffered_start_shrinks_todays_block():
    """Same worked-example calendar as test_compute_day_reproduces_worked_example,
    but scored from 9:30am — the 30-minute buffer pushes the effective
    start to 10am, so the free block starting at 9am is unreachable and
    the 12:00-15:00 block is now the only (and still the largest) one."""
    events = [
        Event(start=time(9, 0), end=time(9, 15)),
        Event(start=time(15, 0), end=time(15, 30)),
    ]
    now_local = datetime(2026, 8, 27, 9, 30, tzinfo=TZ)
    buffered_start = _buffered_wh_start(WH_START, WH_END, now_local)
    assert buffered_start == time(10, 0)
    computation = _compute_day(events, A_DAY, buffered_start, WH_END, [300] * 14)
    assert computation.largest_interval == Interval(start=time(10, 0), end=time(15, 0))


# --- next_fit_start dashboard preview (v1, user-directed) ------------------
# The earliest day an idea could physically fit, not the revival_score-
# weighted "best" day — and computed for every committed latent, not just
# ones the proactive-suggestion eligibility gates would currently allow
# texting about (this is a dashboard preview, not a send decision).


def _day_computation(largest_start, largest_end, block_minutes) -> DayComputation:
    return DayComputation(
        booked=0,
        snapshot=CapacitySnapshot(
            date=A_DAY,
            free_minutes=block_minutes,
            largest_contiguous_block=block_minutes,
            fragmentation_index=0.0,
            load_delta=0.0,
        ),
        largest_interval=Interval(start=largest_start, end=largest_end),
    )


def test_next_fitting_slot_picks_earliest_day_that_physically_fits():
    day1 = date(2026, 8, 27)  # too small a block — 60min, needs 120
    day2 = date(2026, 8, 28)  # fits — 180min block
    day3 = date(2026, 8, 29)  # also fits, but later — must not be picked
    day_context = {
        day1: _day_computation(time(9, 0), time(10, 0), 60),
        day2: _day_computation(time(13, 0), time(16, 0), 180),
        day3: _day_computation(time(9, 0), time(15, 0), 360),
    }
    result = _next_fitting_slot(day_context, TZ, effort_minutes=120)
    assert result == datetime(2026, 8, 28, 13, 0, tzinfo=TZ)


def test_next_fitting_slot_none_when_nothing_fits():
    day_context = {A_DAY: _day_computation(time(9, 0), time(10, 0), 60)}
    result = _next_fitting_slot(day_context, TZ, effort_minutes=120)
    assert result is None


def test_update_next_fit_slots_writes_a_row_per_latent():
    latents = [
        LatentCandidate(
            item_id="item-1", created_at=A_DAY, effort_minutes=60,
            dismissal_count=0, dormant_until=None, last_surfaced_at=None,
            has_open_suggestion=False,
        ),
        LatentCandidate(
            item_id="item-2", created_at=A_DAY, effort_minutes=999,
            dismissal_count=0, dormant_until=None, last_surfaced_at=None,
            has_open_suggestion=False,
        ),
    ]
    day_context = {A_DAY: _day_computation(time(9, 0), time(10, 0), 60)}
    conn = MagicMock()
    _update_next_fit_slots(conn, latents, day_context, TZ)

    assert conn.execute.call_count == 2
    calls = {c.args[1][1]: c.args[1][0] for c in conn.execute.call_args_list}
    assert calls["item-1"] == datetime(2026, 8, 27, 9, 0, tzinfo=TZ)  # fits
    assert calls["item-2"] is None  # 999min never fits anywhere


def test_eligible_latents_maps_rows_to_local_dates():
    created_at = datetime(2026, 8, 9, 3, 0, tzinfo=UTC)  # 23:00 local Aug 8 in America/Toronto
    conn = _mock_connection(
        fetchall_result=[("item-1", created_at, 120, "deep", 0, None, None, False)]
    )
    latents = _eligible_latents(conn, "user-1", TZ)
    assert len(latents) == 1
    assert latents[0].created_at == date(2026, 8, 8)  # local date, not the UTC date


def test_send_suggestion_returns_false_when_no_candidate_clears_threshold():
    conn = _mock_connection()
    day_context = {
        A_DAY: DayComputation(
            booked=210,
            snapshot=CapacitySnapshot(
                date=A_DAY,
                free_minutes=330,
                largest_contiguous_block=180,
                fragmentation_index=0.0,
                load_delta=-0.30,
            ),
            largest_interval=Interval(start=time(12, 0), end=time(15, 0)),
            snapshot_id="snap-1",
        )
    }
    young_latent = LatentCandidate(
        item_id="x",
        created_at=A_DAY,  # captured today — fails the days_since_capture >= 3 gate
        effort_minutes=120,
        dismissal_count=0,
        dormant_until=None,
        last_surfaced_at=None,
        has_open_suggestion=False,
    )
    with patch("dispatcher_svc.main._send_sms") as mock_sms:
        result = _send_suggestion(
            conn, "user-1", "+15551234567", TZ, day_context, A_DAY, [young_latent]
        )
    assert result is False
    mock_sms.assert_not_called()
    insert_calls = [
        c for c in conn.execute.call_args_list if "INSERT INTO suggestions" in c.args[0]
    ]
    assert insert_calls == []


def test_send_suggestion_sends_exactly_one_and_writes_rows():
    conn = _mock_connection(fetchone_result=("Rewrite the ingest pipeline in Rust",))
    day_context = {
        A_DAY: DayComputation(
            booked=210,
            snapshot=CapacitySnapshot(
                date=A_DAY,
                free_minutes=330,
                largest_contiguous_block=180,
                fragmentation_index=0.0,
                load_delta=-0.30,
            ),
            largest_interval=Interval(start=time(12, 0), end=time(15, 0)),
            snapshot_id="snap-1",
        )
    }
    old_latent = LatentCandidate(
        item_id="rust-item",
        created_at=date(2026, 8, 9),  # 18 days old — matches capacity-engine.md §6
        effort_minutes=120,
        dismissal_count=0,
        dormant_until=None,
        last_surfaced_at=None,
        has_open_suggestion=False,
    )
    with patch("dispatcher_svc.main._send_sms") as mock_sms:
        result = _send_suggestion(
            conn, "user-1", "+15551234567", TZ, day_context, A_DAY, [old_latent]
        )

    assert result is True
    mock_sms.assert_called_once()
    assert "Rewrite the ingest pipeline in Rust" in mock_sms.call_args.kwargs["body"]

    insert_calls = [
        c for c in conn.execute.call_args_list if "INSERT INTO suggestions" in c.args[0]
    ]
    assert len(insert_calls) == 1
    assert insert_calls[0].args[1] == ("rust-item", "user-1", "snap-1")

    update_calls = [c for c in conn.execute.call_args_list if "UPDATE latents" in c.args[0]]
    assert len(update_calls) == 1
    assert update_calls[0].args[1] == ("rust-item",)
