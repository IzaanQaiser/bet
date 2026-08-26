"""docs/engineering/test-plan.md step 8 — DB and Twilio mocked, one
helper function at a time. Suggestion/reminder *text* is covered in
test_templates.py (split out since it's pure and unrelated to the DB/SMS
orchestration tested here) — test-plan.md names those tests under this
file's plan, reorganized for a cleaner separation of concerns."""

from datetime import UTC, date, datetime, time
from unittest.mock import MagicMock, patch
from uuid import UUID
from zoneinfo import ZoneInfo

from dispatcher_svc.capacity_engine import Event, Interval, LatentCandidate
from dispatcher_svc.main import (
    _buffered_wh_start,
    _clear_placeholder,
    _compute_day,
    _eligible_latents,
    _next_fitting_slot,
    _recompute_and_reschedule,
    _send_reminders,
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


# --- next_fit_start / auto-scheduled placeholders (ADR 0009) ---------------
# The earliest day an idea could physically fit, excluding that same
# item's own placeholder from what counts as busy — every *other* item's
# placeholder stays counted as real busy time (that's the whole mechanism
# behind a declined idea landing after every already-scheduled one).


ITEM_UUID = "11111111-1111-1111-1111-111111111111"


def _latent(
    item_id=ITEM_UUID, effort_minutes=120, next_fit_start=None, placeholder_event_id=None,
    dismissal_count=0, dormant_until=None, last_surfaced_at=None, has_open_suggestion=False,
):
    return LatentCandidate(
        item_id=item_id, title="Some idea", effort_minutes=effort_minutes,
        dismissal_count=dismissal_count, dormant_until=dormant_until,
        last_surfaced_at=last_surfaced_at, has_open_suggestion=has_open_suggestion,
        next_fit_start=next_fit_start, placeholder_event_id=placeholder_event_id,
    )


def test_next_fitting_slot_picks_earliest_day_that_physically_fits():
    day1 = date(2026, 8, 27)  # too small a block — 60min, needs 120
    day2 = date(2026, 8, 28)  # fits — 180min block
    day3 = date(2026, 8, 29)  # also fits, but later — must not be picked
    forward_events = {
        day1: [Event(start=time(9, 15), end=time(18, 0))],  # only 9:00-9:15 free, too small
        day2: [],  # fully free
        day3: [],
    }
    now_local = datetime(2026, 8, 27, 8, 0, tzinfo=TZ)
    result = _next_fitting_slot(
        forward_events, TZ, WH_START, WH_END, now_local, today=day1, effort_minutes=120,
        exclude_event_id=None,
    )
    assert result == datetime(2026, 8, 28, 9, 0, tzinfo=TZ)


def test_next_fitting_slot_none_when_nothing_fits():
    forward_events = {A_DAY: [Event(start=time(9, 15), end=time(18, 0))]}  # 15min free only
    now_local = datetime(2026, 8, 27, 8, 0, tzinfo=TZ)
    result = _next_fitting_slot(
        forward_events, TZ, WH_START, WH_END, now_local, today=A_DAY, effort_minutes=120,
        exclude_event_id=None,
    )
    assert result is None


def test_next_fitting_slot_excludes_own_placeholder():
    """The self-exclusion case: a 9-10am block tagged as this item's own
    existing placeholder must not block it from being offered that exact
    slot again."""
    forward_events = {A_DAY: [Event(start=time(9, 0), end=time(10, 0), google_event_id="mine")]}
    now_local = datetime(2026, 8, 27, 8, 0, tzinfo=TZ)
    result = _next_fitting_slot(
        forward_events, TZ, WH_START, WH_END, now_local, today=A_DAY, effort_minutes=480,
        exclude_event_id="mine",
    )
    assert result == datetime(2026, 8, 27, 9, 0, tzinfo=TZ)  # the whole day, "mine" excluded


def test_eligible_latents_excludes_dormant_and_maps_columns():
    conn = _mock_connection(
        fetchall_result=[
            ("item-1", "Learn pottery", 120, 0, None, None, False, None, None),
        ]
    )
    latents = _eligible_latents(conn, "user-1", TZ)
    assert len(latents) == 1
    assert latents[0].title == "Learn pottery"
    assert latents[0].effort_minutes == 120
    sql = conn.execute.call_args.args[0]
    assert "dormant_until IS NULL OR l.dormant_until <= now()" in sql


def test_recompute_unchanged_slot_is_a_full_noop():
    """Diffing against the currently stored value is what bounds Cloud
    Tasks/Calendar churn to "once per real slot change" — an unchanged
    recompute must not touch either."""
    existing = datetime(2026, 8, 27, 9, 0, tzinfo=TZ)
    item = _latent(effort_minutes=480, next_fit_start=existing, placeholder_event_id="evt-1")
    forward_events = {A_DAY: []}  # fully free — would recompute to the same 9am slot
    now_local = datetime(2026, 8, 27, 8, 0, tzinfo=TZ)
    conn = MagicMock()
    with (
        patch("dispatcher_svc.main.committer_client") as mock_committer,
        patch("dispatcher_svc.main.tasks_client") as mock_tasks,
    ):
        _recompute_and_reschedule(
            conn, "user-1", TZ, WH_START, WH_END, now_local, A_DAY, forward_events, item
        )
    mock_committer.upsert_placeholder.assert_not_called()
    mock_tasks.enqueue_fire_task.assert_not_called()
    conn.execute.assert_not_called()


def test_recompute_changed_slot_upserts_placeholder_and_enqueues_task():
    item = _latent(effort_minutes=120, next_fit_start=None, placeholder_event_id=None)
    forward_events = {A_DAY: []}
    now_local = datetime(2026, 8, 27, 8, 0, tzinfo=TZ)
    conn = MagicMock()
    with (
        patch("dispatcher_svc.main.committer_client") as mock_committer,
        patch("dispatcher_svc.main.tasks_client") as mock_tasks,
    ):
        mock_committer.upsert_placeholder.return_value = "new-evt-id"
        _recompute_and_reschedule(
            conn, "user-1", TZ, WH_START, WH_END, now_local, A_DAY, forward_events, item
        )

    mock_committer.upsert_placeholder.assert_called_once()
    mock_tasks.enqueue_fire_task.assert_called_once()
    update_calls = [c for c in conn.execute.call_args_list if "UPDATE latents" in c.args[0]]
    assert len(update_calls) == 1
    assert update_calls[0].args[1][1] == "new-evt-id"


def test_recompute_no_fit_clears_existing_placeholder():
    item = _latent(
        effort_minutes=999, next_fit_start=datetime(2026, 8, 27, 9, 0, tzinfo=TZ),
        placeholder_event_id="evt-1",
    )
    forward_events = {A_DAY: []}  # a whole free day still can't fit 999 minutes
    now_local = datetime(2026, 8, 27, 8, 0, tzinfo=TZ)
    conn = MagicMock()
    with patch("dispatcher_svc.main.committer_client") as mock_committer:
        _recompute_and_reschedule(
            conn, "user-1", TZ, WH_START, WH_END, now_local, A_DAY, forward_events, item
        )

    mock_committer.delete_placeholder.assert_called_once()
    update_calls = [c for c in conn.execute.call_args_list if "UPDATE latents" in c.args[0]]
    assert len(update_calls) == 1
    assert "next_fit_start = NULL" in update_calls[0].args[0]


def test_clear_placeholder_deletes_and_nulls_columns():
    item = _latent(placeholder_event_id="evt-1")
    conn = MagicMock()
    with patch("dispatcher_svc.main.committer_client") as mock_committer:
        _clear_placeholder(conn, "user-1", item)
    mock_committer.delete_placeholder.assert_called_once_with(UUID(ITEM_UUID), "user-1", "evt-1")
    conn.execute.assert_called_once()


def test_clear_placeholder_noop_calendar_call_when_nothing_to_delete():
    item = _latent(placeholder_event_id=None)
    conn = MagicMock()
    with patch("dispatcher_svc.main.committer_client") as mock_committer:
        _clear_placeholder(conn, "user-1", item)
    mock_committer.delete_placeholder.assert_not_called()
    conn.execute.assert_called_once()  # still nulls the columns, defensively
