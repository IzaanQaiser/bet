"""Unit tests — DB and Pub/Sub mocked out, per docs/engineering/test-plan.md
step 3 (signature validation, payload parsing) and step 9 (inbound-reply
routing, state-machine.md §4)."""

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


def _mock_connection(user_id, open_item_id=None):
    """Differentiates by query text — the webhook now runs two SELECTs
    against the same connection (user lookup, then open-conversation
    check), not one."""

    def execute_side_effect(sql, params=None):
        result = MagicMock()
        if "FROM users" in sql:
            result.fetchone.return_value = (user_id,)
        elif "FROM conversations" in sql:
            result.fetchone.return_value = (open_item_id,) if open_item_id else None
        else:
            result.fetchone.return_value = None
        return result

    conn = MagicMock()
    conn.execute.side_effect = execute_side_effect
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


def test_no_open_conversation_creates_new_item(client):
    user_id = uuid4()
    form = {"From": "+15551234567", "Body": "pay rent by friday"}
    with (
        patch("ingest_svc.main.get_connection", return_value=_mock_connection(user_id)),
        patch("ingest_svc.main.publish") as mock_publish,
        patch("ingest_svc.main._forward_to_resolver") as mock_forward,
    ):
        resp = client.post("/webhook/sms", data=form, headers=_signed_headers(form))
    assert resp.status_code == 200
    assert resp.json()["status"] == "received"
    mock_publish.assert_called_once()
    mock_forward.assert_not_called()


def test_open_conversation_routes_to_resolver_not_new_item(client):
    user_id = uuid4()
    open_item_id = uuid4()
    form = {"From": "+15551234567", "Body": "yes"}
    with (
        patch(
            "ingest_svc.main.get_connection",
            return_value=_mock_connection(user_id, open_item_id=open_item_id),
        ),
        patch("ingest_svc.main.publish") as mock_publish,
        patch("ingest_svc.main._forward_to_resolver") as mock_forward,
    ):
        resp = client.post("/webhook/sms", data=form, headers=_signed_headers(form))
    assert resp.status_code == 200
    assert resp.json() == {"status": "routed_to_resolver", "item_id": str(open_item_id)}
    mock_publish.assert_not_called()
    mock_forward.assert_called_once_with(user_id, open_item_id, "yes")
