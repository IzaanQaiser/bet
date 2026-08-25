"""Unit tests for the Phase 4 registration-completion endpoints — DB,
Twilio Verify, Google's token endpoint, and Secret Manager all mocked
out, same house style as test_waitlist.py. Token minting/verification is
exercised for real (obligation_engine_shared.tokens, not mocked) against
a fixed test signing key, since that's cheap and it's the actual security
boundary between steps.

Covers docs/design plan Phase 4's stated acceptance cases: invalid/
expired/already-used token rejection, wrong OTP rejection, and a full
oauth-callback producing a real users row + real secret creation calls.
"""

import time
from unittest.mock import MagicMock, patch

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
    monkeypatch.setenv("TWILIO_VERIFY_SERVICE_SID", "VAtest0000000000000000000000000")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID_WEB", "test-client-id.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET_WEB", "test-client-secret")
    monkeypatch.setenv("OAUTH_REDIRECT_URI", "https://registration.example.com/register/oauth-callback")
    monkeypatch.setenv("GCP_PROJECT_ID", "test-project")


@pytest.fixture
def client():
    from registration_svc.main import app

    return TestClient(app, base_url="https://registration.example.com")


def _registration_token(phone="+15551234567", ttl=3600):
    return mint_signed_token({"phone_e164": phone}, "registration", SIGNING_KEY, ttl)


def _oauth_session_token(phone="+15551234567", ttl=900):
    return mint_signed_token({"phone_e164": phone}, "oauth-session", SIGNING_KEY, ttl)


def _oauth_callback_state(phone="+15551234567", timezone="America/Toronto", ttl=600):
    claims = {"phone_e164": phone, "timezone": timezone}
    return mint_signed_token(claims, "oauth-callback", SIGNING_KEY, ttl)


# ---- /register/verify-start ----


def test_verify_start_sends_otp_for_approved_number(client):
    mock_conn = _mock_connection()
    mock_conn.execute.return_value.fetchone.return_value = (time.time(),)  # approved_at set
    mock_verify_service = MagicMock()
    with (
        patch("registration_svc.main.get_connection", return_value=mock_conn),
        patch("registration_svc.main._twilio_verify_service", return_value=mock_verify_service),
    ):
        resp = client.post("/register/verify-start", json={"token": _registration_token()})
    assert resp.status_code == 200
    assert resp.json() == {"status": "sent"}
    mock_verify_service.verifications.create.assert_called_once_with(
        to="+15551234567", channel="sms"
    )


def test_verify_start_rejects_unapproved_number(client):
    mock_conn = _mock_connection()
    mock_conn.execute.return_value.fetchone.return_value = (None,)  # approved_at NULL
    with patch("registration_svc.main.get_connection", return_value=mock_conn):
        resp = client.post("/register/verify-start", json={"token": _registration_token()})
    assert resp.status_code == 400


def test_verify_start_rejects_number_not_on_waitlist(client):
    mock_conn = _mock_connection()
    mock_conn.execute.return_value.fetchone.return_value = None
    with patch("registration_svc.main.get_connection", return_value=mock_conn):
        resp = client.post("/register/verify-start", json={"token": _registration_token()})
    assert resp.status_code == 400


def test_verify_start_rejects_expired_token(client):
    with patch("registration_svc.main.get_connection") as mock_get_conn:
        resp = client.post("/register/verify-start", json={"token": _registration_token(ttl=-1)})
    assert resp.status_code == 400
    mock_get_conn.assert_not_called()


def test_verify_start_rejects_wrong_purpose_token(client):
    wrong_purpose_token = mint_signed_token(
        {"phone_e164": "+15551234567"}, "oauth-session", SIGNING_KEY, 3600
    )
    with patch("registration_svc.main.get_connection") as mock_get_conn:
        resp = client.post("/register/verify-start", json={"token": wrong_purpose_token})
    assert resp.status_code == 400
    mock_get_conn.assert_not_called()


def test_verify_start_rejects_garbage_token(client):
    with patch("registration_svc.main.get_connection") as mock_get_conn:
        resp = client.post("/register/verify-start", json={"token": "not-a-real-token"})
    assert resp.status_code == 400
    mock_get_conn.assert_not_called()


# ---- /register/verify-otp ----


def test_verify_otp_success_returns_oauth_session_token(client):
    mock_verify_service = MagicMock()
    mock_verify_service.verification_checks.create.return_value = MagicMock(status="approved")
    with patch("registration_svc.main._twilio_verify_service", return_value=mock_verify_service):
        resp = client.post(
            "/register/verify-otp", json={"token": _registration_token(), "code": "123456"}
        )
    assert resp.status_code == 200
    assert "oauth_session_token" in resp.json()
    mock_verify_service.verification_checks.create.assert_called_once_with(
        to="+15551234567", code="123456"
    )


def test_verify_otp_rejects_wrong_code(client):
    mock_verify_service = MagicMock()
    mock_verify_service.verification_checks.create.return_value = MagicMock(status="pending")
    with patch("registration_svc.main._twilio_verify_service", return_value=mock_verify_service):
        resp = client.post(
            "/register/verify-otp", json={"token": _registration_token(), "code": "000000"}
        )
    assert resp.status_code == 400


def test_verify_otp_rejects_expired_registration_token(client):
    resp = client.post(
        "/register/verify-otp", json={"token": _registration_token(ttl=-1), "code": "123456"}
    )
    assert resp.status_code == 400


# ---- /register/oauth-start ----


def test_oauth_start_redirects_to_google_with_state(client):
    resp = client.get(
        "/register/oauth-start",
        params={"token": _oauth_session_token(), "timezone": "America/Toronto"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "prompt=consent" in location
    assert "access_type=offline" in location


def test_oauth_start_rejects_expired_session(client):
    resp = client.get(
        "/register/oauth-start",
        params={"token": _oauth_session_token(ttl=-1), "timezone": "America/Toronto"},
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_oauth_start_rejects_registration_token_reused_as_session(client):
    """A Phase 3 registration token should never work here directly — it
    has to go through verify-otp first to become an oauth-session token."""
    resp = client.get(
        "/register/oauth-start",
        params={"token": _registration_token(), "timezone": "America/Toronto"},
        follow_redirects=False,
    )
    assert resp.status_code == 400


# ---- /register/oauth-callback ----


def test_oauth_callback_creates_user_and_secret(client):
    mock_conn = _mock_connection()
    mock_conn.execute.return_value.fetchone.return_value = None  # no existing user

    mock_token_response = MagicMock()
    mock_token_response.json.return_value = {"refresh_token": "real-refresh-token"}
    mock_token_response.raise_for_status = MagicMock()

    mock_secret_client = MagicMock()
    mock_secret = MagicMock()
    mock_secret.name = "projects/test-project/secrets/user-refresh-token-abc"
    mock_secret_client.create_secret.return_value = mock_secret

    with (
        patch("registration_svc.main.get_connection", return_value=mock_conn),
        patch(
            "registration_svc.main.requests.post", return_value=mock_token_response
        ) as mock_post,
        patch(
            "registration_svc.main.secretmanager.SecretManagerServiceClient",
            return_value=mock_secret_client,
        ),
    ):
        resp = client.get(
            "/register/oauth-callback",
            params={"code": "auth-code-123", "state": _oauth_callback_state()},
            follow_redirects=False,
        )

    assert resp.status_code == 302
    assert resp.headers["location"].endswith("/dashboard")
    mock_post.assert_called_once()
    mock_secret_client.create_secret.assert_called_once()
    mock_secret_client.add_secret_version.assert_called_once()

    insert_calls = [c for c in mock_conn.execute.call_args_list if "INSERT INTO users" in c.args[0]]
    assert len(insert_calls) == 1
    _, params = insert_calls[0].args
    assert params[1] == "+15551234567"
    assert params[2] == "America/Toronto"
    mock_conn.commit.assert_called_once()


def test_oauth_callback_rejects_already_registered_number(client):
    mock_conn = _mock_connection()
    mock_conn.execute.return_value.fetchone.return_value = ("existing-user-id",)
    with (
        patch("registration_svc.main.get_connection", return_value=mock_conn),
        patch("registration_svc.main.requests.post") as mock_post,
    ):
        resp = client.get(
            "/register/oauth-callback",
            params={"code": "auth-code-123", "state": _oauth_callback_state()},
            follow_redirects=False,
        )
    assert resp.status_code == 409
    mock_post.assert_not_called()


def test_oauth_callback_rejects_expired_state(client):
    with patch("registration_svc.main.get_connection") as mock_get_conn:
        resp = client.get(
            "/register/oauth-callback",
            params={"code": "auth-code-123", "state": _oauth_callback_state(ttl=-1)},
            follow_redirects=False,
        )
    assert resp.status_code == 400
    mock_get_conn.assert_not_called()


def test_oauth_callback_errors_when_google_omits_refresh_token(client):
    mock_conn = _mock_connection()
    mock_conn.execute.return_value.fetchone.return_value = None

    mock_token_response = MagicMock()
    mock_token_response.json.return_value = {"access_token": "no-refresh-token-here"}
    mock_token_response.raise_for_status = MagicMock()

    with (
        patch("registration_svc.main.get_connection", return_value=mock_conn),
        patch("registration_svc.main.requests.post", return_value=mock_token_response),
    ):
        resp = client.get(
            "/register/oauth-callback",
            params={"code": "auth-code-123", "state": _oauth_callback_state()},
            follow_redirects=False,
        )
    assert resp.status_code == 400
