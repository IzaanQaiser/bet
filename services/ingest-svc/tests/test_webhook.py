"""Unit tests — DB and Pub/Sub mocked out, per docs/engineering/test-plan.md
step 3. Signature validation and payload parsing only."""

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

TEST_AUTH_TOKEN = "test_auth_token_1234567890"
WEBHOOK_URL = "https://ingest.example.com/webhook/sms"


@pytest.fixture(autouse=True)
def _auth_token(monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", TEST_AUTH_TOKEN)


@pytest.fixture
def client():
    from ingest_svc.main import app

    return TestClient(app, base_url="https://ingest.example.com")


def _signed_headers(form: dict[str, str]) -> dict[str, str]:
    validator = RequestValidator(TEST_AUTH_TOKEN)
    return {"X-Twilio-Signature": validator.compute_signature(WEBHOOK_URL, form)}


def _mock_connection(user_id):
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = (user_id,)
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn


def test_valid_signature_accepted(client):
    user_id = uuid4()
    form = {"From": "+15551234567", "Body": "pay rent by friday"}
    with (
        patch("ingest_svc.main.get_connection", return_value=_mock_connection(user_id)),
        patch("ingest_svc.main.publish") as mock_publish,
    ):
        resp = client.post("/webhook/sms", data=form, headers=_signed_headers(form))
    assert resp.status_code == 200
    mock_publish.assert_called_once()


def test_tampered_signature_rejected(client):
    form = {"From": "+15551234567", "Body": "pay rent by friday"}
    headers = _signed_headers(form)
    headers["X-Twilio-Signature"] = headers["X-Twilio-Signature"][:-1] + (
        "A" if headers["X-Twilio-Signature"][-1] != "A" else "B"
    )
    with (
        patch("ingest_svc.main.get_connection") as mock_conn,
        patch("ingest_svc.main.publish") as mock_publish,
    ):
        resp = client.post("/webhook/sms", data=form, headers=headers)
    assert resp.status_code == 403
    mock_conn.assert_not_called()
    mock_publish.assert_not_called()


def test_missing_signature_rejected(client):
    form = {"From": "+15551234567", "Body": "pay rent by friday"}
    with (
        patch("ingest_svc.main.get_connection") as mock_conn,
        patch("ingest_svc.main.publish") as mock_publish,
    ):
        resp = client.post("/webhook/sms", data=form)
    assert resp.status_code == 403
    mock_conn.assert_not_called()
    mock_publish.assert_not_called()


def test_parses_text_only_payload(client):
    user_id = uuid4()
    form = {"From": "+15551234567", "Body": "pay rent by friday"}
    with (
        patch("ingest_svc.main.get_connection", return_value=_mock_connection(user_id)),
        patch("ingest_svc.main.publish") as mock_publish,
    ):
        client.post("/webhook/sms", data=form, headers=_signed_headers(form))
    published_message = mock_publish.call_args[0][1]
    assert published_message.text == "pay rent by friday"
    assert published_message.media_uri is None
    assert published_message.mime_type is None
