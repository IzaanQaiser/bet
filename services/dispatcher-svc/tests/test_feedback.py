"""docs/engineering/test-plan.md step 14 — the feedback loop: Y/N/Later
replies to a fired suggestion, the 24h no-response timeout, and (ADR
0009) the auto-scheduled-placeholder endpoints (/latents/{id}/next-fit,
/users/{id}/next-fit, /latents/{id}/fire). DB, Twilio, Calendar, and the
committer-svc/Cloud-Tasks client modules are all mocked here."""

from datetime import date, datetime, time
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from dispatcher_svc.capacity_engine import Event
from dispatcher_svc.main import _capped_effort_minutes, _resolve_stale_suggestions
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("TWILIO_API_KEY_SECRET", "test-not-a-real-secret")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest0000000000000000000000000")
    monkeypatch.setenv("TWILIO_API_KEY_SID", "SKtest0000000000000000000000000")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "test-client-secret")
    from dispatcher_svc.main import app

    return TestClient(app)


def _mock_connection(fetchone_result=None):
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = fetchone_result
    conn.execute.return_value.fetchall.return_value = []
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn


def _suggestion_context(
    *,
    suggestion_id="sugg-1",
    title="Learn pottery",
    summary="Take a class.",
    effort_minutes=120,
    dismissal_count=0,
    placeholder_event_id="evt-placeholder",
    tz_name="America/Toronto",
    wh_start=time(9, 0),
    wh_end=time(18, 0),
    refresh_ref="projects/p/secrets/user-refresh-token-x/versions/latest",
    phone="+15551234567",
    scheduled_for=datetime(2026, 8, 27, 9, 0),
):
    return (
        suggestion_id,
        title,
        summary,
        effort_minutes,
        dismissal_count,
        placeholder_event_id,
        tz_name,
        wh_start,
        wh_end,
        refresh_ref,
        phone,
        scheduled_for,
    )


def _reply_payload(text, item_id=None, user_id=None):
    return {
        "user_id": str(user_id or uuid4()),
        "item_id": str(item_id or uuid4()),
        "text": text,
    }


# --- pure functions ---------------------------------------------------


def test_capped_effort_minutes_uses_original_when_it_fits():
    assert _capped_effort_minutes(60, block_minutes=180) == 60


def test_capped_effort_minutes_caps_to_largest_fitting_bucket():
    assert _capped_effort_minutes(120, block_minutes=50) == 30


def test_capped_effort_minutes_falls_back_to_smallest_bucket_if_none_fit():
    assert _capped_effort_minutes(120, block_minutes=5) == 15


def test_resolve_stale_suggestions_returns_count_and_commits():
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = [("s1",), ("s2",)]
    resolved = _resolve_stale_suggestions(conn, "user-1")
    assert resolved == 2
    conn.commit.assert_called_once()
    sql = conn.execute.call_args.args[0]
    assert "no_response" in sql
    assert "24 hours" in sql


def test_resolve_stale_suggestions_no_commit_when_none_stale():
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = []
    resolved = _resolve_stale_suggestions(conn, "user-1")
    assert resolved == 0
    conn.commit.assert_not_called()


# --- /reply outcome table (state-machine.md §2, ADR 0009) --------------


def test_n_reply_below_threshold_reschedules_immediately(client):
    conn = _mock_connection(fetchone_result=_suggestion_context(dismissal_count=0))
    events_by_day = {date(2026, 8, 28): []}
    with (
        patch("dispatcher_svc.main.get_connection", return_value=conn),
        patch("dispatcher_svc.main._send_sms") as mock_sms,
        patch("dispatcher_svc.main.user_credentials"),
        patch("dispatcher_svc.main.AuthorizedSession"),
        patch("dispatcher_svc.main.fetch_events_for_range", return_value=events_by_day),
        patch("dispatcher_svc.main.committer_client") as mock_committer,
        patch("dispatcher_svc.main.tasks_client") as mock_tasks,
    ):
        mock_committer.upsert_placeholder.return_value = "new-evt-id"
        resp = client.post("/reply", json=_reply_payload("n"))

    assert resp.status_code == 200
    assert resp.json()["status"] == "dismissed"
    mock_sms.assert_called_once()
    # A first dismissal reschedules — not a cooldown message.
    assert "np" in mock_sms.call_args.args[2]
    mock_committer.upsert_placeholder.assert_called_once()
    mock_tasks.enqueue_fire_task.assert_called_once()

    dismissal_update = [
        c for c in conn.execute.call_args_list
        if "UPDATE latents" in c.args[0] and "dismissal_count" in c.args[0]
        and "dormant_until" not in c.args[0]
    ]
    assert len(dismissal_update) == 1
    assert dismissal_update[0].args[1][0] == 1  # dismissal_count incremented to 1


def test_n_reply_reaching_threshold_goes_dormant_and_clears_placeholder(client):
    conn = _mock_connection(fetchone_result=_suggestion_context(dismissal_count=1))
    with (
        patch("dispatcher_svc.main.get_connection", return_value=conn),
        patch("dispatcher_svc.main._send_sms"),
        patch("dispatcher_svc.main.committer_client") as mock_committer,
    ):
        resp = client.post("/reply", json=_reply_payload("no"))

    assert resp.json()["status"] == "dismissed"
    mock_committer.delete_placeholder.assert_called_once()

    dormancy_update = [
        c for c in conn.execute.call_args_list
        if "UPDATE latents" in c.args[0] and "dormant_until" in c.args[0]
        and "dismissal_count" in c.args[0]
    ]
    assert len(dormancy_update) == 1
    assert dormancy_update[0].args[1][0] == 2  # dismissal_count incremented to 2


def test_later_reply_snoozes_clears_placeholder_no_dismissal_change(client):
    conn = _mock_connection(fetchone_result=_suggestion_context(dismissal_count=1))
    with (
        patch("dispatcher_svc.main.get_connection", return_value=conn),
        patch("dispatcher_svc.main._send_sms") as mock_sms,
        patch("dispatcher_svc.main.committer_client") as mock_committer,
    ):
        resp = client.post("/reply", json=_reply_payload("later"))

    assert resp.json()["status"] == "snoozed"
    mock_sms.assert_called_once()
    mock_committer.delete_placeholder.assert_called_once()

    suggestion_update = [
        c for c in conn.execute.call_args_list if "UPDATE suggestions" in c.args[0]
    ][0]
    assert "snoozed" in suggestion_update.args[0]

    dormancy_update = [
        c for c in conn.execute.call_args_list
        if "UPDATE latents" in c.args[0] and "dormant_until" in c.args[0]
    ][0]
    assert "dismissal_count" not in dormancy_update.args[0]


def test_y_reply_publishes_confirmed_and_sends_ack(client):
    conn = _mock_connection(
        fetchone_result=_suggestion_context(
            effort_minutes=120, scheduled_for=datetime(2026, 8, 27, 9, 0)
        )
    )
    events_by_day = {date(2026, 8, 27): []}
    with (
        patch("dispatcher_svc.main.get_connection", return_value=conn),
        patch("dispatcher_svc.main._send_sms") as mock_sms,
        patch("dispatcher_svc.main.user_credentials"),
        patch("dispatcher_svc.main.AuthorizedSession"),
        patch("dispatcher_svc.main.fetch_events_for_range", return_value=events_by_day),
        patch("dispatcher_svc.main.publish") as mock_publish,
    ):
        resp = client.post("/reply", json=_reply_payload("y"))

    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"
    mock_sms.assert_called_once()

    mock_publish.assert_called_once()
    topic, confirmed = mock_publish.call_args[0]
    assert topic == "items-confirmed"
    assert confirmed.type == "obligation"
    assert confirmed.action_type == "calendar"
    # full working day free (9-18), no events -> largest block is the
    # whole day; effort_minutes (120) fits under it untouched.
    assert confirmed.effort_minutes == 120
    assert confirmed.due_at == datetime(2026, 8, 27, 9, 0, tzinfo=confirmed.due_at.tzinfo)

    suggestion_update = [
        c for c in conn.execute.call_args_list if "UPDATE suggestions" in c.args[0]
    ][0]
    assert "accepted" in suggestion_update.args[0]


def test_y_reply_picks_earliest_fitting_gap_not_largest(client):
    """Real bug, found live: accept picked the day's *largest* free
    interval, which could commit the obligation to a completely
    different time than what was actually texted (scheduled_for) — here,
    a meeting splits the day into an earlier ~5h45m gap and a later ~7h
    gap; the 120min item must land in the earlier one, not the bigger
    later one."""
    conn = _mock_connection(
        fetchone_result=_suggestion_context(
            effort_minutes=120, scheduled_for=datetime(2026, 8, 27, 9, 0)
        )
    )
    events_by_day = {date(2026, 8, 27): [Event(start=time(14, 45), end=time(15, 0))]}
    with (
        patch("dispatcher_svc.main.get_connection", return_value=conn),
        patch("dispatcher_svc.main._send_sms"),
        patch("dispatcher_svc.main.user_credentials"),
        patch("dispatcher_svc.main.AuthorizedSession"),
        patch("dispatcher_svc.main.fetch_events_for_range", return_value=events_by_day),
        patch("dispatcher_svc.main.publish") as mock_publish,
    ):
        resp = client.post("/reply", json=_reply_payload("y"))

    assert resp.status_code == 200
    _topic, confirmed = mock_publish.call_args[0]
    assert confirmed.due_at == datetime(2026, 8, 27, 9, 0, tzinfo=confirmed.due_at.tzinfo)


def test_y_reply_falls_back_to_largest_capped_when_nothing_fully_fits(client):
    """Pre-existing behavior, preserved: an explicit Y is never refused
    over a small overrun — if nothing fits the full request, fall back
    to whatever's biggest and cap the effort down to it."""
    conn = _mock_connection(
        fetchone_result=_suggestion_context(
            effort_minutes=120, scheduled_for=datetime(2026, 8, 27, 9, 0)
        )
    )
    # Only a 60min gap exists anywhere in the day — smaller than the 120min ask.
    events_by_day = {
        date(2026, 8, 27): [
            Event(start=time(10, 0), end=time(18, 0)),
        ]
    }
    with (
        patch("dispatcher_svc.main.get_connection", return_value=conn),
        patch("dispatcher_svc.main._send_sms"),
        patch("dispatcher_svc.main.user_credentials"),
        patch("dispatcher_svc.main.AuthorizedSession"),
        patch("dispatcher_svc.main.fetch_events_for_range", return_value=events_by_day),
        patch("dispatcher_svc.main.publish") as mock_publish,
    ):
        resp = client.post("/reply", json=_reply_payload("y"))

    assert resp.status_code == 200
    _topic, confirmed = mock_publish.call_args[0]
    assert confirmed.effort_minutes == 60  # capped down to the only available gap
    assert confirmed.due_at == datetime(2026, 8, 27, 9, 0, tzinfo=confirmed.due_at.tzinfo)


def test_y_reply_no_capacity_left_dismisses_instead(client):
    conn = _mock_connection(fetchone_result=_suggestion_context())
    # A full day of events covering the entire working window -> no free interval.
    events_by_day = {date(2026, 8, 27): [Event(start=time(9, 0), end=time(18, 0))]}
    with (
        patch("dispatcher_svc.main.get_connection", return_value=conn),
        patch("dispatcher_svc.main._send_sms") as mock_sms,
        patch("dispatcher_svc.main.user_credentials"),
        patch("dispatcher_svc.main.AuthorizedSession"),
        patch("dispatcher_svc.main.fetch_events_for_range", return_value=events_by_day),
        patch("dispatcher_svc.main.publish") as mock_publish,
    ):
        resp = client.post("/reply", json=_reply_payload("y"))

    assert resp.json()["status"] == "no_capacity"
    mock_publish.assert_not_called()
    mock_sms.assert_called_once()
    assert "filled up" in mock_sms.call_args.args[2]


def test_other_reply_logged_not_acted_on(client):
    conn = _mock_connection(fetchone_result=_suggestion_context())
    with (
        patch("dispatcher_svc.main.get_connection", return_value=conn),
        patch("dispatcher_svc.main._send_sms") as mock_sms,
        patch("dispatcher_svc.main.publish") as mock_publish,
    ):
        resp = client.post("/reply", json=_reply_payload("maybe tomorrow?"))

    assert resp.json()["status"] == "unhandled_reply"
    mock_sms.assert_not_called()
    mock_publish.assert_not_called()


def test_reply_with_no_open_suggestion_returns_unexpected_state(client):
    conn = _mock_connection(fetchone_result=None)
    with patch("dispatcher_svc.main.get_connection", return_value=conn):
        resp = client.post("/reply", json=_reply_payload("y"))
    assert resp.json()["status"] == "unexpected_state"


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# --- POST /latents/{item_id}/next-fit (ADR 0009) ------------------------
# User-directed real bug fix: a freshly-committed idea previously showed
# "someday" until the next twice-daily /dispatch sweep. committer-svc now
# fires this endpoint immediately on commit via an unscheduled Cloud Task.


def _mock_next_fit_connection(*, item_row, next_fit_updates):
    def execute_side_effect(sql, params=None):
        result = MagicMock()
        if "FROM items i" in sql and "JOIN latents l" in sql:
            result.fetchone.return_value = item_row
        elif "UPDATE latents SET next_fit_start" in sql:
            next_fit_updates.append(params)
        return result

    conn = MagicMock()
    conn.execute.side_effect = execute_side_effect
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn


def _item_row(
    title="Nerf gun turret", effort_minutes=120, user_id=None, tz_name="America/Toronto",
    wh_start=time(9, 0), wh_end=time(18, 0),
    refresh_ref="projects/p/secrets/user-refresh-token-x/versions/latest",
    dismissal_count=0, dormant_until=None, last_surfaced_at=None,
    next_fit_start=None, placeholder_event_id=None,
):
    return (
        title, effort_minutes, user_id or uuid4(), tz_name, wh_start, wh_end, refresh_ref,
        dismissal_count, dormant_until, last_surfaced_at, next_fit_start, placeholder_event_id,
    )


def test_next_fit_computes_and_persists_earliest_fitting_day(client):
    item_id = uuid4()
    next_fit_updates = []
    conn = _mock_next_fit_connection(item_row=_item_row(), next_fit_updates=next_fit_updates)
    events_by_day = {date(2026, 8, 27): [], date(2026, 8, 28): []}

    with (
        patch("dispatcher_svc.main.get_connection", return_value=conn),
        patch("dispatcher_svc.main.user_credentials"),
        patch("dispatcher_svc.main.AuthorizedSession"),
        patch("dispatcher_svc.main.fetch_events_for_range", return_value=events_by_day),
        patch("dispatcher_svc.main.committer_client") as mock_committer,
        patch("dispatcher_svc.main.tasks_client") as mock_tasks,
    ):
        mock_committer.upsert_placeholder.return_value = "evt-1"
        resp = client.post(f"/latents/{item_id}/next-fit")

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    mock_committer.upsert_placeholder.assert_called_once()
    mock_tasks.enqueue_fire_task.assert_called_once()
    assert len(next_fit_updates) == 1


def test_next_fit_skips_users_with_no_linked_google_account(client):
    item_id = uuid4()
    conn = _mock_next_fit_connection(item_row=_item_row(refresh_ref=None), next_fit_updates=[])

    with (
        patch("dispatcher_svc.main.get_connection", return_value=conn),
        patch("dispatcher_svc.main.fetch_events_for_range") as mock_fetch,
    ):
        resp = client.post(f"/latents/{item_id}/next-fit")

    assert resp.status_code == 200
    assert resp.json()["status"] == "skipped_no_google_account"
    mock_fetch.assert_not_called()


def test_next_fit_unknown_item_returns_404(client):
    conn = _mock_next_fit_connection(item_row=None, next_fit_updates=[])
    with patch("dispatcher_svc.main.get_connection", return_value=conn):
        resp = client.post(f"/latents/{uuid4()}/next-fit")
    assert resp.status_code == 404


# --- POST /users/{user_id}/next-fit (ADR 0009) --------------------------
# User-directed real bug fix: changing working hours on the dashboard
# didn't recompute any already-committed idea's next_fit_start until the
# next twice-daily sweep.


def _mock_user_next_fit_connection(*, user_row, latent_rows, next_fit_updates):
    def execute_side_effect(sql, params=None):
        result = MagicMock()
        if "FROM users WHERE id" in sql:
            result.fetchone.return_value = user_row
        elif "FROM latents l" in sql:
            result.fetchall.return_value = latent_rows
        elif "UPDATE latents SET next_fit_start" in sql:
            next_fit_updates.append(params)
        return result

    conn = MagicMock()
    conn.execute.side_effect = execute_side_effect
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn


def test_next_fit_for_user_recomputes_every_committed_latent(client):
    user_id = uuid4()
    user_row = (
        "America/Toronto",
        time(9, 0),
        time(23, 0),
        "projects/p/secrets/user-refresh-token-x/versions/latest",
    )
    latent_rows = [
        (uuid4(), "Idea one", 120, 0, None, None, False, None, None),
        (uuid4(), "Idea two", 240, 0, None, None, False, None, None),
    ]
    next_fit_updates = []
    conn = _mock_user_next_fit_connection(
        user_row=user_row, latent_rows=latent_rows, next_fit_updates=next_fit_updates
    )
    events_by_day = {date(2026, 8, 27): [], date(2026, 8, 28): []}

    with (
        patch("dispatcher_svc.main.get_connection", return_value=conn),
        patch("dispatcher_svc.main.user_credentials"),
        patch("dispatcher_svc.main.AuthorizedSession"),
        patch("dispatcher_svc.main.fetch_events_for_range", return_value=events_by_day),
        patch("dispatcher_svc.main.committer_client") as mock_committer,
        patch("dispatcher_svc.main.tasks_client"),
    ):
        mock_committer.upsert_placeholder.return_value = "evt-1"
        resp = client.post(f"/users/{user_id}/next-fit")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["latents_updated"] == 2
    assert len(next_fit_updates) == 2


def test_next_fit_for_user_skips_with_no_linked_google_account(client):
    user_id = uuid4()
    user_row = ("America/Toronto", time(9, 0), time(18, 0), None)
    conn = _mock_user_next_fit_connection(user_row=user_row, latent_rows=[], next_fit_updates=[])

    with (
        patch("dispatcher_svc.main.get_connection", return_value=conn),
        patch("dispatcher_svc.main.fetch_events_for_range") as mock_fetch,
    ):
        resp = client.post(f"/users/{user_id}/next-fit")

    assert resp.status_code == 200
    assert resp.json()["status"] == "skipped_no_google_account"
    mock_fetch.assert_not_called()


def test_next_fit_for_user_unknown_user_returns_404(client):
    conn = _mock_user_next_fit_connection(user_row=None, latent_rows=[], next_fit_updates=[])
    with patch("dispatcher_svc.main.get_connection", return_value=conn):
        resp = client.post(f"/users/{uuid4()}/next-fit")
    assert resp.status_code == 404


# --- POST /latents/{item_id}/fire (ADR 0009) -----------------------------
# The Cloud Task tasks_client.enqueue_fire_task schedules for exactly a
# latent's next_fit_start.


def _mock_fire_connection(*, row):
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = row
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn


def _fire_row(
    title="Nerf gun turret", effort_minutes=240, user_id=None, tz_name="America/Vancouver",
    wh_start=time(9, 0), wh_end=time(23, 0),
    refresh_ref="projects/p/secrets/user-refresh-token-x/versions/latest",
    dismissal_count=0, dormant_until=None, last_surfaced_at=None,
    next_fit_start=None, placeholder_event_id="evt-1", has_open_suggestion=False,
):
    return (
        title, effort_minutes, user_id or uuid4(), tz_name, wh_start, wh_end, refresh_ref, phone,
        dismissal_count, dormant_until, last_surfaced_at, next_fit_start, placeholder_event_id,
        has_open_suggestion,
    )


phone = "+15551234567"


def test_fire_sends_sms_and_creates_suggestion_when_slot_still_fits(client):
    scheduled_for = datetime(2026, 8, 27, 9, 0, tzinfo=None)
    row = _fire_row(next_fit_start=datetime(2026, 8, 27, 9, 0))
    conn = _mock_fire_connection(row=row)
    events_by_day = {date(2026, 8, 27): []}  # fully free

    with (
        patch("dispatcher_svc.main.get_connection", return_value=conn),
        patch("dispatcher_svc.main._send_sms") as mock_sms,
        patch("dispatcher_svc.main.user_credentials"),
        patch("dispatcher_svc.main.AuthorizedSession"),
        patch("dispatcher_svc.main.fetch_events_for_range", return_value=events_by_day),
    ):
        resp = client.post(
            f"/latents/{uuid4()}/fire", json={"scheduled_for": scheduled_for.isoformat()}
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "fired"
    mock_sms.assert_called_once()
    insert_calls = [
        c for c in conn.execute.call_args_list if "INSERT INTO suggestions" in c.args[0]
    ]
    assert len(insert_calls) == 1


def test_fire_skips_stale_task(client):
    """A later write already superseded this task's scheduled_for by the
    time it actually fires — must no-op, not double-text."""
    row = _fire_row(next_fit_start=datetime(2026, 8, 28, 9, 0))  # different from payload below
    conn = _mock_fire_connection(row=row)

    with (
        patch("dispatcher_svc.main.get_connection", return_value=conn),
        patch("dispatcher_svc.main._send_sms") as mock_sms,
    ):
        resp = client.post(
            f"/latents/{uuid4()}/fire",
            json={"scheduled_for": datetime(2026, 8, 27, 9, 0).isoformat()},
        )

    assert resp.json()["status"] == "stale_task_skipped"
    mock_sms.assert_not_called()


def test_fire_no_longer_committed_latent_is_a_noop(client):
    conn = _mock_fire_connection(row=None)
    with patch("dispatcher_svc.main.get_connection", return_value=conn):
        resp = client.post(
            f"/latents/{uuid4()}/fire",
            json={"scheduled_for": datetime(2026, 8, 27, 9, 0).isoformat()},
        )
    assert resp.json()["status"] == "skipped"


def test_fire_reschedules_silently_when_slot_no_longer_fits(client):
    next_fit = datetime(2026, 8, 27, 9, 0)
    row = _fire_row(next_fit_start=next_fit)
    conn = _mock_fire_connection(row=row)
    # The block is now fully booked — the stored next_fit_start is stale.
    events_by_day = {date(2026, 8, 27): [Event(start=time(9, 0), end=time(23, 0))]}

    with (
        patch("dispatcher_svc.main.get_connection", return_value=conn),
        patch("dispatcher_svc.main._send_sms") as mock_sms,
        patch("dispatcher_svc.main.user_credentials"),
        patch("dispatcher_svc.main.AuthorizedSession"),
        patch("dispatcher_svc.main.fetch_events_for_range", return_value=events_by_day),
        patch("dispatcher_svc.main.committer_client") as mock_committer,
    ):
        resp = client.post(
            f"/latents/{uuid4()}/fire", json={"scheduled_for": next_fit.isoformat()}
        )

    assert resp.json()["status"] == "rescheduled_silently"
    mock_sms.assert_not_called()
    # No fitting slot anywhere in this mocked (all-booked) window -> cleared.
    mock_committer.delete_placeholder.assert_called_once()
