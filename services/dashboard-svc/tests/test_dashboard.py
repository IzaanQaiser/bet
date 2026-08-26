"""Unit tests — DB and Twilio mocked out, same house style as
registration-svc's test suite. Covers docs/design plan Phase 5's stated
acceptance cases: JWT validation (expired/tampered/missing -> 401),
not-a-registered-number 404 path, and per-endpoint scoping — since the
DB is mocked, "scoping" here means proving every query's user_id
parameter always comes from the session token's own claim, never a
client-supplied value, by minting two different sessions and checking
each query used its own token's user_id, not a shared/hardcoded one.
"""

from datetime import UTC, datetime, time
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from obligation_engine_shared.tokens import mint_signed_token

SIGNING_KEY = "test-signing-key-at-least-32-bytes-long"


def _mock_connection():
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("WEB_SESSION_SIGNING_KEY", SIGNING_KEY)
    monkeypatch.setenv("TWILIO_API_KEY_SECRET", "test_api_key_secret")
    monkeypatch.setenv("TWILIO_API_KEY_SID", "SKtest0000000000000000000000000")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest0000000000000000000000000")
    monkeypatch.setenv("TWILIO_VERIFY_SERVICE_SID", "VAtest0000000000000000000000000")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "test-client-secret")


@pytest.fixture
def client():
    from dashboard_svc.main import app

    return TestClient(app, base_url="https://dashboard.example.com")


def _session_token(user_id, ttl=3600):
    return mint_signed_token({"user_id": str(user_id)}, "dashboard-session", SIGNING_KEY, ttl)


def _auth_header(user_id, ttl=3600):
    return {"Authorization": f"Bearer {_session_token(user_id, ttl)}"}


# ---- /auth/start ----


def test_auth_start_sends_otp_for_registered_number(client):
    mock_conn = _mock_connection()
    mock_conn.execute.return_value.fetchone.return_value = (str(uuid4()),)
    mock_verify_service = MagicMock()
    with (
        patch("dashboard_svc.main.get_connection", return_value=mock_conn),
        patch("dashboard_svc.main._twilio_verify_service", return_value=mock_verify_service),
    ):
        resp = client.post("/auth/start", json={"phone_e164": "+15551234567"})
    assert resp.status_code == 200
    mock_verify_service.verifications.create.assert_called_once_with(
        to="+15551234567", channel="sms"
    )


def test_auth_start_rejects_unregistered_number(client):
    mock_conn = _mock_connection()
    mock_conn.execute.return_value.fetchone.return_value = None
    with (
        patch("dashboard_svc.main.get_connection", return_value=mock_conn),
        patch("dashboard_svc.main._twilio_verify_service") as mock_verify,
    ):
        resp = client.post("/auth/start", json={"phone_e164": "+15559999999"})
    assert resp.status_code == 404
    mock_verify.assert_not_called()


# ---- /auth/verify ----


def test_auth_verify_success_returns_session_token(client):
    user_id = uuid4()
    mock_conn = _mock_connection()
    mock_conn.execute.return_value.fetchone.return_value = (user_id,)
    mock_verify_service = MagicMock()
    mock_verify_service.verification_checks.create.return_value = MagicMock(status="approved")
    with (
        patch("dashboard_svc.main.get_connection", return_value=mock_conn),
        patch("dashboard_svc.main._twilio_verify_service", return_value=mock_verify_service),
    ):
        resp = client.post("/auth/verify", json={"phone_e164": "+15551234567", "code": "123456"})
    assert resp.status_code == 200
    assert "session_token" in resp.json()


def test_auth_verify_rejects_wrong_code(client):
    mock_verify_service = MagicMock()
    mock_verify_service.verification_checks.create.return_value = MagicMock(status="pending")
    with (
        patch("dashboard_svc.main.get_connection") as mock_get_conn,
        patch("dashboard_svc.main._twilio_verify_service", return_value=mock_verify_service),
    ):
        resp = client.post("/auth/verify", json={"phone_e164": "+15551234567", "code": "000000"})
    assert resp.status_code == 400
    mock_get_conn.assert_not_called()


# ---- session auth dependency ----


def test_me_items_rejects_missing_session(client):
    resp = client.get("/me/items")
    assert resp.status_code == 401


def test_me_items_rejects_garbage_token(client):
    resp = client.get("/me/items", headers={"Authorization": "Bearer not-a-real-token"})
    assert resp.status_code == 401


def test_me_items_rejects_expired_session(client):
    resp = client.get("/me/items", headers=_auth_header(uuid4(), ttl=-1))
    assert resp.status_code == 401


def test_me_items_rejects_wrong_purpose_token(client):
    wrong_purpose = mint_signed_token({"user_id": str(uuid4())}, "registration", SIGNING_KEY, 3600)
    resp = client.get("/me/items", headers={"Authorization": f"Bearer {wrong_purpose}"})
    assert resp.status_code == 401


# ---- /me/items scoping ----


def test_me_items_scopes_query_to_session_user(client):
    user_a, user_b = uuid4(), uuid4()
    mock_conn = _mock_connection()
    mock_conn.execute.return_value.fetchall.return_value = []

    with patch("dashboard_svc.main.get_connection", return_value=mock_conn):
        client.get("/me/items", headers=_auth_header(user_a))
        first_call_user = mock_conn.execute.call_args_list[0].args[1][0]

        client.get("/me/items", headers=_auth_header(user_b))
        second_call_user = mock_conn.execute.call_args_list[-1].args[1][0]

    assert first_call_user == str(user_a)
    assert second_call_user == str(user_b)
    assert first_call_user != second_call_user


def test_me_items_groups_by_state(client):
    user_id = uuid4()
    item_a, item_b, item_c = uuid4(), uuid4(), uuid4()
    now = datetime.now(UTC)
    mock_conn = _mock_connection()

    committed_row = (
        item_b, "Committed thing", "summary", now, "cal-evt-1", 60, "obligation", None, None
    )
    cancelled_row = (item_c, "Cancelled thing", "CANCELLED", now)
    in_progress_row = (item_a, "In progress thing", "summary", "CLARIFYING", now)

    def side_effect(sql, params=None):
        result = MagicMock()
        states = params[1] if params and len(params) > 1 else []
        if "obligations" in sql:
            result.fetchall.return_value = [committed_row]
        elif "conversations" in sql:
            result.fetchall.return_value = []
        elif "CANCELLED" in states:
            result.fetchall.return_value = [cancelled_row]
        elif "RECEIVED" in states:
            result.fetchall.return_value = [in_progress_row]
        else:
            result.fetchall.return_value = []
        return result

    mock_conn.execute.side_effect = side_effect
    with patch("dashboard_svc.main.get_connection", return_value=mock_conn):
        resp = client.get("/me/items", headers=_auth_header(user_id))

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["in_progress"]) == 1
    assert body["in_progress"][0]["title"] == "In progress thing"
    assert len(body["committed"]) == 1
    assert body["committed"][0]["calendar_event_id"] == "cal-evt-1"
    assert body["committed"][0]["effort_minutes"] == 60
    assert len(body["other"]) == 1
    assert body["other"][0]["state"] == "CANCELLED"


def test_me_items_committed_includes_ideas_alongside_obligations(client):
    """Real bug, found live: a committed idea (type='latent') never gets
    an obligations row — it lives in latents instead — so the old inner
    JOIN obligations silently excluded every "someday" idea from this
    list even after the user was told "i've got that down for you". The
    committed query now UNIONs both sources into one result set."""
    user_id = uuid4()
    item_obligation, item_idea = uuid4(), uuid4()
    now = datetime.now(UTC)
    next_fit = datetime(2026, 8, 28, 13, 0, tzinfo=UTC)
    mock_conn = _mock_connection()

    # Simulates the real UNION ALL: obligations contribute a real due_at/
    # calendar_event_id and NULL focus_depth/next_fit_start, latents
    # contribute NULL due_at/calendar_event_id and real focus_depth/
    # next_fit_start.
    committed_rows = [
        (item_obligation, "Pay rent", "summary", now, "cal-evt-1", 15, "obligation", None, None),
        (
            item_idea, "Make an AI nerf gun turret", "someday idea", None, None, 240, "latent",
            "deep", next_fit,
        ),
    ]

    def side_effect(sql, params=None):
        result = MagicMock()
        if "obligations" in sql:
            result.fetchall.return_value = committed_rows
        else:
            result.fetchall.return_value = []
        return result

    mock_conn.execute.side_effect = side_effect
    with patch("dashboard_svc.main.get_connection", return_value=mock_conn):
        resp = client.get("/me/items", headers=_auth_header(user_id))

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["committed"]) == 2
    idea_row = next(r for r in body["committed"] if r["title"] == "Make an AI nerf gun turret")
    assert idea_row["due_at"] is None
    assert idea_row["calendar_event_id"] is None
    assert idea_row["effort_minutes"] == 240
    assert idea_row["type"] == "latent"
    assert idea_row["focus_depth"] == "deep"
    assert idea_row["next_fit_start"] == next_fit.isoformat()
    obligation_row = next(r for r in body["committed"] if r["title"] == "Pay rent")
    assert obligation_row["type"] == "obligation"
    assert obligation_row["focus_depth"] is None
    assert obligation_row["next_fit_start"] is None


# ---- /me/messages ----


def test_me_messages_scopes_to_session_user_and_orders_oldest_first(client):
    user_id = uuid4()
    mock_conn = _mock_connection()
    t1, t2 = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)
    # DB returns DESC (newest first); handler must reverse to oldest-first
    db_rows = [("out", "second", t2), ("in", "first", t1)]
    mock_conn.execute.return_value.fetchall.return_value = db_rows

    with patch("dashboard_svc.main.get_connection", return_value=mock_conn):
        resp = client.get("/me/messages", headers=_auth_header(user_id))

    assert resp.status_code == 200
    messages = resp.json()["messages"]
    assert [m["body"] for m in messages] == ["first", "second"]
    called_user_id = mock_conn.execute.call_args.args[1][0]
    assert called_user_id == str(user_id)


# ---- /me/suggestions ----


def test_me_suggestions_scopes_to_session_user(client):
    user_id = uuid4()
    mock_conn = _mock_connection()
    mock_conn.execute.return_value.fetchall.return_value = []
    with patch("dashboard_svc.main.get_connection", return_value=mock_conn):
        resp = client.get("/me/suggestions", headers=_auth_header(user_id))
    assert resp.status_code == 200
    called_user_id = mock_conn.execute.call_args.args[1][0]
    assert called_user_id == str(user_id)


# ---- /me/profile ----


def test_me_profile_get_returns_own_row(client):
    user_id = uuid4()
    mock_conn = _mock_connection()
    mock_conn.execute.return_value.fetchone.return_value = (
        "+15551234567",
        "America/Toronto",
        time(9, 0),
        time(18, 0),
    )
    with patch("dashboard_svc.main.get_connection", return_value=mock_conn):
        resp = client.get("/me/profile", headers=_auth_header(user_id))
    assert resp.status_code == 200
    body = resp.json()
    assert body["timezone"] == "America/Toronto"
    called_user_id = mock_conn.execute.call_args.args[1][0]
    assert called_user_id == str(user_id)


def test_me_profile_patch_updates_only_provided_fields(client):
    user_id = uuid4()
    mock_conn = _mock_connection()
    with patch("dashboard_svc.main.get_connection", return_value=mock_conn):
        resp = client.patch(
            "/me/profile", json={"timezone": "America/Los_Angeles"}, headers=_auth_header(user_id)
        )
    assert resp.status_code == 200
    sql, params = mock_conn.execute.call_args.args
    assert "timezone = %s" in sql
    assert "working_hours_start" not in sql
    assert params == ("America/Los_Angeles", str(user_id))
    mock_conn.commit.assert_called_once()


def test_me_profile_patch_rejects_unknown_timezone(client):
    with patch("dashboard_svc.main.get_connection") as mock_get_conn:
        resp = client.patch(
            "/me/profile", json={"timezone": "Not/A_Real_Zone"}, headers=_auth_header(uuid4())
        )
    assert resp.status_code == 400
    mock_get_conn.assert_not_called()


def test_me_profile_patch_rejects_empty_body(client):
    with patch("dashboard_svc.main.get_connection") as mock_get_conn:
        resp = client.patch("/me/profile", json={}, headers=_auth_header(uuid4()))
    assert resp.status_code == 400
    mock_get_conn.assert_not_called()


# ---- DELETE /me/items/{id} ----


def _mock_secret_client(refresh_token="real-refresh-token"):
    client = MagicMock()
    client.access_secret_version.return_value.payload.data = refresh_token.encode()
    return client


def test_delete_committed_item_deletes_calendar_event_and_cancels(client):
    user_id, item_id = uuid4(), uuid4()
    mock_conn = _mock_connection()
    mock_conn.execute.return_value.fetchone.return_value = (
        user_id,
        "cal-evt-1",
        "projects/test/secrets/user-refresh-token-x/versions/latest",
    )
    mock_session = MagicMock()
    mock_session.delete.return_value = MagicMock(status_code=204)

    with (
        patch("dashboard_svc.main.get_connection", return_value=mock_conn),
        patch(
            "dashboard_svc.main.secretmanager.SecretManagerServiceClient",
            return_value=_mock_secret_client(),
        ),
        patch("dashboard_svc.main.AuthorizedSession", return_value=mock_session),
    ):
        resp = client.delete(f"/me/items/{item_id}", headers=_auth_header(user_id))

    assert resp.status_code == 200
    mock_session.delete.assert_called_once_with(
        "https://www.googleapis.com/calendar/v3/calendars/primary/events/cal-evt-1"
    )
    update_calls = [c for c in mock_conn.execute.call_args_list if "UPDATE items" in c.args[0]]
    assert len(update_calls) == 1
    assert update_calls[0].args[1] == (str(item_id),)
    mock_conn.commit.assert_called_once()


def test_delete_in_progress_item_skips_calendar_no_event_id(client):
    user_id, item_id = uuid4(), uuid4()
    mock_conn = _mock_connection()
    mock_conn.execute.return_value.fetchone.return_value = (user_id, None, None)

    with (
        patch("dashboard_svc.main.get_connection", return_value=mock_conn),
        patch("dashboard_svc.main.AuthorizedSession") as mock_authed_session,
    ):
        resp = client.delete(f"/me/items/{item_id}", headers=_auth_header(user_id))

    assert resp.status_code == 200
    mock_authed_session.assert_not_called()


def test_delete_already_gone_calendar_event_still_succeeds(client):
    """The exact case this endpoint exists for: the user already deleted
    the event directly in Google Calendar. A 404 from Google here is the
    goal state, not an error."""
    user_id, item_id = uuid4(), uuid4()
    mock_conn = _mock_connection()
    mock_conn.execute.return_value.fetchone.return_value = (
        user_id,
        "cal-evt-1",
        "projects/test/secrets/user-refresh-token-x/versions/latest",
    )
    mock_session = MagicMock()
    mock_session.delete.return_value = MagicMock(status_code=404)

    with (
        patch("dashboard_svc.main.get_connection", return_value=mock_conn),
        patch(
            "dashboard_svc.main.secretmanager.SecretManagerServiceClient",
            return_value=_mock_secret_client(),
        ),
        patch("dashboard_svc.main.AuthorizedSession", return_value=mock_session),
    ):
        resp = client.delete(f"/me/items/{item_id}", headers=_auth_header(user_id))

    assert resp.status_code == 200


def test_delete_calendar_api_error_is_best_effort_not_fatal(client):
    user_id, item_id = uuid4(), uuid4()
    mock_conn = _mock_connection()
    mock_conn.execute.return_value.fetchone.return_value = (
        user_id,
        "cal-evt-1",
        "projects/test/secrets/user-refresh-token-x/versions/latest",
    )

    with (
        patch("dashboard_svc.main.get_connection", return_value=mock_conn),
        patch(
            "dashboard_svc.main.secretmanager.SecretManagerServiceClient",
            return_value=_mock_secret_client(),
        ),
        patch("dashboard_svc.main.AuthorizedSession", side_effect=RuntimeError("network blip")),
    ):
        resp = client.delete(f"/me/items/{item_id}", headers=_auth_header(user_id))

    assert resp.status_code == 200
    update_calls = [c for c in mock_conn.execute.call_args_list if "UPDATE items" in c.args[0]]
    assert len(update_calls) == 1


def test_delete_item_rejects_other_users_item(client):
    owner_id, requester_id, item_id = uuid4(), uuid4(), uuid4()
    mock_conn = _mock_connection()
    mock_conn.execute.return_value.fetchone.return_value = (owner_id, None, None)

    with patch("dashboard_svc.main.get_connection", return_value=mock_conn):
        resp = client.delete(f"/me/items/{item_id}", headers=_auth_header(requester_id))

    assert resp.status_code == 404
    update_calls = [c for c in mock_conn.execute.call_args_list if "UPDATE items" in c.args[0]]
    assert len(update_calls) == 0


def test_delete_nonexistent_item_returns_404(client):
    mock_conn = _mock_connection()
    mock_conn.execute.return_value.fetchone.return_value = None
    with patch("dashboard_svc.main.get_connection", return_value=mock_conn):
        resp = client.delete(f"/me/items/{uuid4()}", headers=_auth_header(uuid4()))
    assert resp.status_code == 404


def test_delete_item_rejects_missing_session(client):
    resp = client.delete(f"/me/items/{uuid4()}")
    assert resp.status_code == 401


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
