"""Unit tests — DB, Secret Manager, and the Calendar API call all mocked,
per docs/engineering/test-plan.md step 6."""

import base64
from datetime import datetime
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from obligation_engine_shared.schemas import ConfirmedItemMessage


def _push_envelope(message) -> dict:
    return {"message": {"data": base64.b64encode(message.model_dump_json().encode()).decode()}}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "test-client-secret")
    from committer_svc.main import app

    return TestClient(app)


def _mock_connection(user_row=None):
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = user_row
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn


def _confirmed_message(**overrides):
    defaults = dict(
        item_id=uuid4(),
        user_id=uuid4(),
        type="obligation",
        title="Pay rent",
        summary="Pay rent by Friday.",
        due_at=datetime(2026, 8, 28, 14, 0),
        effort_minutes=15,
        action_type="calendar",
        email_draft=None,
    )
    defaults.update(overrides)
    return ConfirmedItemMessage(**defaults)


def _mock_secret_client(refresh_token="refresh-token-value"):
    secret_client = MagicMock()
    secret_client.access_secret_version.return_value.payload.data = refresh_token.encode()
    return secret_client


def test_obligation_branch_calls_calendar_write(client):
    confirmed = _confirmed_message()
    conn = _mock_connection(
        user_row=("projects/p/secrets/user-refresh-token-x/versions/latest", "America/Los_Angeles")
    )
    calendar_response = MagicMock()
    calendar_response.json.return_value = {"id": "gcal-event-123"}

    with (
        patch("committer_svc.main.get_connection", return_value=conn),
        patch("committer_svc.main._secret_client", return_value=_mock_secret_client()),
        patch("committer_svc.main.AuthorizedSession") as mock_session_cls,
    ):
        mock_session_cls.return_value.post.return_value = calendar_response
        resp = client.post("/pubsub/push", json=_push_envelope(confirmed))

    assert resp.status_code == 200
    assert resp.json() == {"status": "committed", "item_id": str(confirmed.item_id)}

    mock_session_cls.return_value.post.assert_called_once()
    _, kwargs = mock_session_cls.return_value.post.call_args
    assert kwargs["json"]["summary"] == "Pay rent"

    # call 0 is the SELECT in _user_credentials; 1 is the obligations INSERT; 2 is the items UPDATE.
    insert_sql, insert_params = conn.execute.call_args_list[1][0]
    assert "INSERT INTO obligations" in insert_sql
    assert insert_params[2] == "gcal-event-123"  # calendar_event_id

    update_sql, update_params = conn.execute.call_args_list[2][0]
    assert "state = 'COMMITTED'" in update_sql
    assert update_params[0] == str(confirmed.item_id)


def test_latent_branch_does_not_call_calendar(client):
    confirmed = _confirmed_message(
        type="latent", due_at=None, action_type=None, title="Learn pottery", summary="Someday."
    )
    conn = _mock_connection()

    with (
        patch("committer_svc.main.get_connection", return_value=conn),
        patch("committer_svc.main.AuthorizedSession") as mock_session_cls,
    ):
        resp = client.post("/pubsub/push", json=_push_envelope(confirmed))

    assert resp.status_code == 200
    mock_session_cls.assert_not_called()

    insert_sql = conn.execute.call_args_list[0][0][0]
    assert "INSERT INTO latents" in insert_sql
    update_sql = conn.execute.call_args_list[1][0][0]
    assert "state = 'COMMITTED'" in update_sql


def test_calendar_failure_does_not_mark_committed(client):
    confirmed = _confirmed_message()
    conn = _mock_connection(
        user_row=("projects/p/secrets/user-refresh-token-x/versions/latest", "America/Los_Angeles")
    )

    with (
        patch("committer_svc.main.get_connection", return_value=conn),
        patch("committer_svc.main._secret_client", return_value=_mock_secret_client()),
        patch("committer_svc.main.AuthorizedSession") as mock_session_cls,
    ):
        mock_session_cls.return_value.post.side_effect = RuntimeError("Calendar API 500")
        resp = client.post("/pubsub/push", json=_push_envelope(confirmed))

    assert resp.status_code == 500
    # Only the credentials SELECT happened — the calendar call raised
    # before the obligations INSERT / items UPDATE were ever reached.
    assert conn.execute.call_count == 1
    assert "SELECT" in conn.execute.call_args_list[0][0][0]


def test_no_linked_google_account_fails_without_writing(client):
    confirmed = _confirmed_message()
    conn = _mock_connection(user_row=None)

    with patch("committer_svc.main.get_connection", return_value=conn):
        resp = client.post("/pubsub/push", json=_push_envelope(confirmed))

    assert resp.status_code == 500


def test_email_action_type_not_implemented(client):
    confirmed = _confirmed_message(action_type="email", email_draft="Draft body.")
    conn = _mock_connection(
        user_row=("projects/p/secrets/user-refresh-token-x/versions/latest", "America/Los_Angeles")
    )

    with patch("committer_svc.main.get_connection", return_value=conn):
        resp = client.post("/pubsub/push", json=_push_envelope(confirmed))

    assert resp.status_code == 500
    conn.execute.assert_not_called()


def test_malformed_envelope_returns_500_for_retry(client):
    resp = client.post("/pubsub/push", json={"message": {"data": "not-valid-base64json"}})
    assert resp.status_code == 500


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
