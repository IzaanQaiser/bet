"""Unit tests — DB, Calendar, Cloud Tasks all mocked. Covers the
reconciliation decision logic (cancelled -> cancel item; time-changed ->
update + reschedule; no match -> skip untouched) and the two endpoints'
independent verification (channel token for /webhook, Google-signed OIDC
identity for /sync/run — this service can't be Cloud-Run-IAM-gated since
/webhook must be public)."""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("GCP_PROJECT_ID", "obligation-engine-hack")
    monkeypatch.setenv("DISPATCHER_SVC_URL", "https://dispatcher-svc.example.run.app")
    monkeypatch.setenv("CALENDAR_SYNC_SVC_URL", "https://calendar-sync-svc.example.run.app")


@pytest.fixture
def client():
    from calendar_sync_svc.main import app

    return TestClient(app)


def _obligation_event(event_id="evt-1", start="2026-08-27T17:00:00-07:00", cancelled=False):
    event = {"id": event_id}
    if cancelled:
        event["status"] = "cancelled"
    else:
        event["start"] = {"dateTime": start}
    return event


# --- _parse_event_start -------------------------------------------------


def test_parse_event_start_returns_datetime():
    from calendar_sync_svc.main import _parse_event_start

    result = _parse_event_start({"start": {"dateTime": "2026-08-27T17:00:00-07:00"}})
    assert result == datetime.fromisoformat("2026-08-27T17:00:00-07:00")


def test_parse_event_start_none_for_all_day_event():
    from calendar_sync_svc.main import _parse_event_start

    assert _parse_event_start({"start": {"date": "2026-08-27"}}) is None


def test_parse_event_start_none_for_cancelled_event_with_no_start():
    from calendar_sync_svc.main import _parse_event_start

    assert _parse_event_start({"id": "x", "status": "cancelled"}) is None


# --- _reconcile_obligation -----------------------------------------------


def test_reconcile_obligation_cancelled_cancels_item():
    from calendar_sync_svc.main import _reconcile_obligation

    conn = MagicMock()
    item_id = uuid4()
    _reconcile_obligation(conn, item_id, datetime.now(UTC), _obligation_event(cancelled=True), True)

    update_calls = [c for c in conn.execute.call_args_list if "UPDATE items" in c.args[0]]
    assert len(update_calls) == 1
    assert "CANCELLED" in update_calls[0].args[0]
    conn.commit.assert_called()


def test_reconcile_obligation_unchanged_time_is_a_noop():
    from calendar_sync_svc.main import _reconcile_obligation

    conn = MagicMock()
    stored = datetime.fromisoformat("2026-08-27T17:00:00-07:00")
    event = _obligation_event(start="2026-08-27T17:00:00-07:00")
    with patch("calendar_sync_svc.main._enqueue_reminder_task") as mock_enqueue:
        _reconcile_obligation(conn, uuid4(), stored, event, False)
    conn.execute.assert_not_called()
    mock_enqueue.assert_not_called()


def test_reconcile_obligation_time_changed_updates_and_reschedules():
    from calendar_sync_svc.main import _reconcile_obligation

    conn = MagicMock()
    item_id = uuid4()
    stored = datetime.fromisoformat("2026-08-27T17:00:00-07:00")
    new_time = "2026-08-28T09:00:00-07:00"  # well in the future
    with patch("calendar_sync_svc.main._enqueue_reminder_task") as mock_enqueue:
        _reconcile_obligation(conn, item_id, stored, _obligation_event(start=new_time), False)

    update_calls = [c for c in conn.execute.call_args_list if "UPDATE obligations" in c.args[0]]
    assert len(update_calls) == 1
    params = update_calls[0].args[1]
    assert params[0] == datetime.fromisoformat(new_time)  # due_at
    assert params[1] == datetime.fromisoformat(new_time)  # reminder_at == due_at (v1)
    assert params[2] is None  # reminder not already past
    mock_enqueue.assert_called_once_with(item_id, datetime.fromisoformat(new_time))


def test_reconcile_obligation_past_new_time_marks_reminder_sent_no_enqueue():
    from calendar_sync_svc.main import _reconcile_obligation

    conn = MagicMock()
    stored = datetime.fromisoformat("2026-08-27T17:00:00-07:00")
    past_time = "2020-01-01T09:00:00-07:00"
    with patch("calendar_sync_svc.main._enqueue_reminder_task") as mock_enqueue:
        _reconcile_obligation(conn, uuid4(), stored, _obligation_event(start=past_time), False)
    mock_enqueue.assert_not_called()


# --- _reconcile_latent ----------------------------------------------------


def test_reconcile_latent_cancelled_cancels_item_and_clears_placeholder():
    from calendar_sync_svc.main import _reconcile_latent

    conn = MagicMock()
    item_id = uuid4()
    _reconcile_latent(conn, item_id, datetime.now(UTC), _obligation_event(cancelled=True), True)

    item_update = [c for c in conn.execute.call_args_list if "UPDATE items" in c.args[0]]
    latent_update = [c for c in conn.execute.call_args_list if "UPDATE latents" in c.args[0]]
    assert len(item_update) == 1
    assert "CANCELLED" in item_update[0].args[0]
    assert len(latent_update) == 1
    assert "next_fit_start = NULL" in latent_update[0].args[0]
    assert "placeholder_event_id = NULL" in latent_update[0].args[0]


def test_reconcile_latent_moved_updates_next_fit_start_and_reschedules():
    from calendar_sync_svc.main import _reconcile_latent

    conn = MagicMock()
    item_id = uuid4()
    stored = datetime.fromisoformat("2026-08-27T17:00:00-07:00")
    new_time = "2026-08-27T20:00:00-07:00"
    with patch("calendar_sync_svc.main._enqueue_fire_task") as mock_enqueue:
        _reconcile_latent(conn, item_id, stored, _obligation_event(start=new_time), False)

    update_calls = [c for c in conn.execute.call_args_list if "UPDATE latents" in c.args[0]]
    assert len(update_calls) == 1
    assert update_calls[0].args[1] == (datetime.fromisoformat(new_time), str(item_id))
    mock_enqueue.assert_called_once_with(item_id, datetime.fromisoformat(new_time))


def test_reconcile_latent_unchanged_time_is_a_noop():
    from calendar_sync_svc.main import _reconcile_latent

    conn = MagicMock()
    stored = datetime.fromisoformat("2026-08-27T17:00:00-07:00")
    with patch("calendar_sync_svc.main._enqueue_fire_task") as mock_enqueue:
        _reconcile_latent(
            conn, uuid4(), stored, _obligation_event(start="2026-08-27T17:00:00-07:00"), False
        )
    conn.execute.assert_not_called()
    mock_enqueue.assert_not_called()


# --- _reconcile_event: routing + the "never touch untracked events" rule -


def _mock_lookup_connection(*, ob_row=None, lat_row=None):
    def execute_side_effect(sql, params=None):
        result = MagicMock()
        if "FROM obligations o" in sql:
            result.fetchone.return_value = ob_row
        elif "FROM latents l" in sql:
            result.fetchone.return_value = lat_row
        else:
            result.fetchone.return_value = None
        return result

    conn = MagicMock()
    conn.execute.side_effect = execute_side_effect
    return conn


def test_reconcile_event_untracked_event_is_never_touched():
    from calendar_sync_svc.main import _reconcile_event

    conn = _mock_lookup_connection(ob_row=None, lat_row=None)
    _reconcile_event(conn, _obligation_event())
    # Only the two SELECT lookups happened — no UPDATE of any kind.
    update_calls = [
        c for c in conn.execute.call_args_list if c.args[0].strip().startswith("UPDATE")
    ]
    assert update_calls == []


def test_reconcile_event_routes_to_obligation_when_matched():
    from calendar_sync_svc.main import _reconcile_event

    item_id = uuid4()
    conn = _mock_lookup_connection(ob_row=(item_id, datetime.now(UTC)))
    with patch("calendar_sync_svc.main._reconcile_obligation") as mock_reconcile:
        _reconcile_event(conn, _obligation_event(cancelled=True))
    mock_reconcile.assert_called_once()
    assert mock_reconcile.call_args.args[1] == item_id


def test_reconcile_event_routes_to_latent_when_matched():
    from calendar_sync_svc.main import _reconcile_event

    item_id = uuid4()
    conn = _mock_lookup_connection(ob_row=None, lat_row=(item_id, datetime.now(UTC)))
    with patch("calendar_sync_svc.main._reconcile_latent") as mock_reconcile:
        _reconcile_event(conn, _obligation_event(cancelled=True))
    mock_reconcile.assert_called_once()
    assert mock_reconcile.call_args.args[1] == item_id


# --- /webhook -------------------------------------------------------------


def test_webhook_rejects_missing_headers(client):
    resp = client.post("/webhook")
    assert resp.status_code == 400


def test_webhook_rejects_unknown_channel(client):
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = None
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    with patch("calendar_sync_svc.main.get_connection", return_value=conn):
        resp = client.post(
            "/webhook",
            headers={"X-Goog-Channel-ID": "chan-1", "X-Goog-Channel-Token": "wrong-token"},
        )
    assert resp.status_code == 403


def test_webhook_rejects_mismatched_token(client):
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = (uuid4(), "real-token")
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    with patch("calendar_sync_svc.main.get_connection", return_value=conn):
        resp = client.post(
            "/webhook",
            headers={"X-Goog-Channel-ID": "chan-1", "X-Goog-Channel-Token": "wrong-token"},
        )
    assert resp.status_code == 403


def test_webhook_sync_state_acks_without_syncing(client):
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = (uuid4(), "real-token")
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    with (
        patch("calendar_sync_svc.main.get_connection", return_value=conn),
        patch("calendar_sync_svc.main._sync_user") as mock_sync,
    ):
        resp = client.post(
            "/webhook",
            headers={
                "X-Goog-Channel-ID": "chan-1",
                "X-Goog-Channel-Token": "real-token",
                "X-Goog-Resource-State": "sync",
            },
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ack"
    mock_sync.assert_not_called()


def test_webhook_valid_call_triggers_sync(client):
    user_id = uuid4()

    def execute_side_effect(sql, params=None):
        result = MagicMock()
        if "FROM calendar_sync_channels" in sql:
            result.fetchone.return_value = (user_id, "real-token")
        elif "FROM users" in sql:
            result.fetchone.return_value = (
                "projects/p/secrets/user-refresh-token-x/versions/latest",
            )
        return result

    conn = MagicMock()
    conn.execute.side_effect = execute_side_effect
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    with (
        patch("calendar_sync_svc.main.get_connection", return_value=conn),
        patch("calendar_sync_svc.main._sync_user") as mock_sync,
    ):
        resp = client.post(
            "/webhook",
            headers={
                "X-Goog-Channel-ID": "chan-1",
                "X-Goog-Channel-Token": "real-token",
                "X-Goog-Resource-State": "exists",
            },
        )
    assert resp.status_code == 200
    mock_sync.assert_called_once()
    assert mock_sync.call_args.args[1] == user_id


# --- /sync/run: OIDC verification -----------------------------------------


def test_sync_run_rejects_missing_bearer_token(client):
    resp = client.post("/sync/run")
    assert resp.status_code == 401


def test_sync_run_rejects_invalid_token(client):
    with patch(
        "calendar_sync_svc.main.google_id_token.verify_oauth2_token",
        side_effect=ValueError("bad token"),
    ):
        resp = client.post("/sync/run", headers={"Authorization": "Bearer garbage"})
    assert resp.status_code == 401


def test_sync_run_rejects_wrong_identity(client):
    with patch(
        "calendar_sync_svc.main.google_id_token.verify_oauth2_token",
        return_value={"email": "someone-else@example.com", "email_verified": True},
    ):
        resp = client.post("/sync/run", headers={"Authorization": "Bearer token"})
    assert resp.status_code == 401


def test_sync_run_accepts_correct_identity_and_runs(client):
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = []
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    with (
        patch(
            "calendar_sync_svc.main.google_id_token.verify_oauth2_token",
            return_value={
                "email": "sa-calendar-sync@obligation-engine-hack.iam.gserviceaccount.com",
                "email_verified": True,
            },
        ),
        patch("calendar_sync_svc.main.get_connection", return_value=conn),
    ):
        resp = client.post("/sync/run", headers={"Authorization": "Bearer token"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
