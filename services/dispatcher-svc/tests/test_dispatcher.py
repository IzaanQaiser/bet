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
    _compute_day,
    _eligible_latents,
    _send_reminders,
    _send_suggestion,
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


def test_reminder_not_resent_if_already_sent():
    """The SQL's own `reminder_sent_at IS NULL` clause is what enforces
    this for real (verified against live Postgres in the integration
    test) — here, an empty result set (what that clause produces once a
    reminder has been sent) must send nothing."""
    conn = _mock_connection(fetchall_result=[])
    with patch("dispatcher_svc.main._send_sms") as mock_sms:
        sent = _send_reminders(conn, "user-1", "+15551234567", datetime.now(UTC), TZ)
    assert sent == 0
    mock_sms.assert_not_called()


def test_reminder_sent_when_due_marks_reminder_sent_at():
    due_at = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    conn = _mock_connection(fetchall_result=[("item-1", "Pay rent", due_at)])
    with patch("dispatcher_svc.main._send_sms") as mock_sms:
        sent = _send_reminders(conn, "user-1", "+15551234567", datetime.now(UTC), TZ)
    assert sent == 1
    mock_sms.assert_called_once()
    call_kwargs = mock_sms.call_args.kwargs
    assert call_kwargs["to"] == "+15551234567"
    assert "Pay rent" in call_kwargs["body"]

    update_calls = [c for c in conn.execute.call_args_list if "UPDATE obligations" in c.args[0]]
    assert len(update_calls) == 1
    assert update_calls[0].args[1] == ("item-1",)


def test_compute_day_reproduces_worked_example():
    events = [
        Event(start=time(9, 0), end=time(12, 0)),
        Event(start=time(15, 0), end=time(15, 30)),
    ]
    computation = _compute_day(events, A_DAY, WH_START, WH_END, [300] * 14)
    assert computation.booked == 210
    assert round(computation.snapshot.load_delta, 2) == -0.30
    assert computation.largest_interval == Interval(start=time(12, 0), end=time(15, 0))


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
        focus_depth="deep",
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
        focus_depth="deep",
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
