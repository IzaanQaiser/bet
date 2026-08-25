"""docs/engineering/test-plan.md step 14 — the feedback loop: Y/N/Later
replies to a sent suggestion, and the 24h no-response timeout. DB,
Twilio, and Calendar all mocked; state-machine.md §2's outcome table is
tested exhaustively."""

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
    snapshot_id="snap-1",
    title="Learn pottery",
    summary="Take a class.",
    effort_minutes=120,
    dismissal_count=0,
    tz_name="America/Toronto",
    wh_start=time(9, 0),
    wh_end=time(18, 0),
    refresh_ref="projects/p/secrets/user-refresh-token-x/versions/latest",
    phone="+15551234567",
    snapshot_date=date(2026, 8, 27),
):
    return (
        suggestion_id,
        snapshot_id,
        title,
        summary,
        effort_minutes,
        dismissal_count,
        tz_name,
        wh_start,
        wh_end,
        refresh_ref,
        phone,
        snapshot_date,
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


# --- /reply outcome table (state-machine.md §2) ------------------------


def test_n_reply_below_threshold_returns_to_eligible(client):
    conn = _mock_connection(fetchone_result=_suggestion_context(dismissal_count=0))
    with (
        patch("dispatcher_svc.main.get_connection", return_value=conn),
        patch("dispatcher_svc.main._send_sms") as mock_sms,
    ):
        resp = client.post("/reply", json=_reply_payload("n"))

    assert resp.status_code == 200
    assert resp.json()["status"] == "dismissed"
    mock_sms.assert_called_once()

    update_calls = [c for c in conn.execute.call_args_list if "UPDATE latents" in c.args[0]]
    assert len(update_calls) == 1
    assert "dormant_until" not in update_calls[0].args[0]
    assert update_calls[0].args[1][0] == 1  # dismissal_count incremented to 1


def test_n_reply_reaching_threshold_goes_dormant(client):
    conn = _mock_connection(fetchone_result=_suggestion_context(dismissal_count=1))
    with (
        patch("dispatcher_svc.main.get_connection", return_value=conn),
        patch("dispatcher_svc.main._send_sms"),
    ):
        resp = client.post("/reply", json=_reply_payload("no"))

    assert resp.json()["status"] == "dismissed"
    update_calls = [c for c in conn.execute.call_args_list if "UPDATE latents" in c.args[0]]
    assert len(update_calls) == 1
    assert "dormant_until" in update_calls[0].args[0]
    assert update_calls[0].args[1][0] == 2  # dismissal_count incremented to 2


def test_later_reply_snoozes_no_dismissal_change(client):
    conn = _mock_connection(fetchone_result=_suggestion_context(dismissal_count=1))
    with (
        patch("dispatcher_svc.main.get_connection", return_value=conn),
        patch("dispatcher_svc.main._send_sms") as mock_sms,
    ):
        resp = client.post("/reply", json=_reply_payload("later"))

    assert resp.json()["status"] == "snoozed"
    mock_sms.assert_called_once()

    suggestion_update = [
        c for c in conn.execute.call_args_list if "UPDATE suggestions" in c.args[0]
    ][0]
    assert "snoozed" in suggestion_update.args[0]

    latent_update = [c for c in conn.execute.call_args_list if "UPDATE latents" in c.args[0]][0]
    assert "dismissal_count" not in latent_update.args[0]
    assert "dormant_until" in latent_update.args[0]


def test_y_reply_publishes_confirmed_and_sends_ack(client):
    conn = _mock_connection(
        fetchone_result=_suggestion_context(effort_minutes=120, snapshot_date=date(2026, 8, 27))
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
