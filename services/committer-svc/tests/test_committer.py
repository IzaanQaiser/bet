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


def _mock_connection(*, already_committed=False, user_row=None):
    """already_committed feeds the idempotency guard's _already_committed()
    check (SELECT 1 FROM obligations/latents WHERE item_id=...) at the top
    of /pubsub/push — False (the default) lets every pre-existing test
    reach the real commit logic unchanged; tests exercising the guard
    itself override it. Keyed on the target table, not items.state — see
    _already_committed()'s own docstring for why (a real bug found
    verifying step 14's accept path)."""

    def execute_side_effect(sql, params=None):
        result = MagicMock()
        if "FROM obligations" in sql or "FROM latents" in sql:
            result.fetchone.return_value = (1,) if already_committed else None
        elif "FROM users" in sql:
            result.fetchone.return_value = user_row
        else:
            result.fetchone.return_value = None
        return result

    conn = MagicMock()
    conn.execute.side_effect = execute_side_effect
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

    # call 0 is the idempotency guard's items.state check; 1 is the SELECT
    # in _user_credentials; 2 is the obligations INSERT; 3 is the items UPDATE.
    insert_sql, insert_params = conn.execute.call_args_list[2][0]
    assert "INSERT INTO obligations" in insert_sql
    assert insert_params[2] == "gcal-event-123"  # calendar_event_id

    update_sql, update_params = conn.execute.call_args_list[3][0]
    assert "state = 'COMMITTED'" in update_sql
    assert update_params == (confirmed.type, str(confirmed.item_id))


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

    # call 0 is the idempotency guard's items.state check.
    insert_sql = conn.execute.call_args_list[1][0][0]
    assert "INSERT INTO latents" in insert_sql
    update_sql = conn.execute.call_args_list[2][0][0]
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
    # The idempotency guard's state check, then the credentials SELECT,
    # happened — the calendar call raised before the obligations INSERT /
    # items UPDATE were ever reached.
    assert conn.execute.call_count == 2
    assert "SELECT" in conn.execute.call_args_list[1][0][0]


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
    # Only the idempotency guard's already-committed check happened —
    # the NotImplementedError is raised before any real DB write.
    assert conn.execute.call_count == 1
    assert "FROM obligations" in conn.execute.call_args_list[0][0][0]


def test_malformed_envelope_returns_500_for_retry(client):
    resp = client.post("/pubsub/push", json={"message": {"data": "not-valid-base64json"}})
    assert resp.status_code == 500


# --- idempotency guard (step 13, refined by a real bug found in step 14) --


def test_redelivered_already_committed_item_is_a_noop(client):
    """The real bug class found in step 11: a concurrent Pub/Sub
    redelivery of the same items.confirmed message must not create a
    second real Calendar event."""
    confirmed = _confirmed_message()
    conn = _mock_connection(already_committed=True)

    with (
        patch("committer_svc.main.get_connection", return_value=conn),
        patch("committer_svc.main.AuthorizedSession") as mock_session_cls,
    ):
        resp = client.post("/pubsub/push", json=_push_envelope(confirmed))

    assert resp.status_code == 200
    assert resp.json() == {"status": "already_processed", "item_id": str(confirmed.item_id)}
    mock_session_cls.assert_not_called()
    assert conn.execute.call_count == 1  # only the already-committed check


def test_accepted_latent_is_not_blocked_by_its_own_prior_commit(client):
    """The real bug found verifying step 14's accept path: an item that
    was already COMMITTED once as a latent must not be treated as
    already-processed when it legitimately comes through a second time
    as an accepted obligation — _already_committed() checks the
    obligations table specifically (empty here), not items.state or the
    latents table (which does have a row, from the original commit)."""
    confirmed = _confirmed_message(type="obligation")  # dispatcher-svc's accept publish
    conn = _mock_connection(
        already_committed=False,  # no obligations row yet, even though latents has one
        user_row=("projects/p/secrets/user-refresh-token-x/versions/latest", "America/Los_Angeles"),
    )
    calendar_response = MagicMock()
    calendar_response.json.return_value = {"id": "gcal-event-456"}

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


# --- /pubsub/dlq (step 13) -------------------------------------------------


def _dlq_envelope(payload: dict, retry_count: int = 5) -> dict:
    import base64
    import json

    data = base64.b64encode(json.dumps(payload).encode()).decode()
    return {
        "message": {
            "data": data,
            "attributes": {"CloudPubSubDeadLetterSourceDeliveryCount": str(retry_count)},
            "messageId": "123",
        },
        "subscription": "projects/p/subscriptions/items-raw-dlq-committer-push",
    }


def test_dlq_writes_dead_letter_row_and_marks_failed(client):
    item_id = str(uuid4())
    payload = {"item_id": item_id, "user_id": str(uuid4()), "text": "hi"}
    conn = _mock_connection()

    with patch("committer_svc.main.get_connection", return_value=conn):
        resp = client.post(
            "/pubsub/dlq?stage=items-raw", json=_dlq_envelope(payload, retry_count=5)
        )

    assert resp.status_code == 200
    assert resp.json() == {"status": "dead_lettered", "item_id": item_id}

    insert_sql, insert_params = conn.execute.call_args_list[0][0]
    assert "INSERT INTO dead_letters" in insert_sql
    assert insert_params[0] == item_id
    assert insert_params[1] == "items-raw"
    assert insert_params[4] == 5  # retry_count

    update_sql, update_params = conn.execute.call_args_list[1][0]
    assert "state = 'FAILED'" in update_sql
    assert update_params[0] == item_id


def test_dlq_malformed_envelope_acked_not_retried(client):
    """A dead-lettered message that can't even be parsed has no item_id
    to record against — logged and acked (200), not retried forever."""
    conn = _mock_connection()
    with patch("committer_svc.main.get_connection", return_value=conn):
        resp = client.post(
            "/pubsub/dlq?stage=items-raw",
            json={"message": {"data": "not-valid-base64json", "attributes": {}}},
        )
    assert resp.status_code == 200
    conn.execute.assert_not_called()


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
