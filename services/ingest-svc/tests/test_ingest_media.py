"""docs/engineering/test-plan.md step 11 — DB, GCS, and Pub/Sub mocked.
Twilio signature validation itself is covered in test_webhook.py; these
tests focus on MMS-specific routing (MIME type acceptance/rejection,
media persisted before the items row is written)."""

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
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest0000000000000000000000000000")


@pytest.fixture
def client():
    from ingest_svc.main import app

    return TestClient(app, base_url="https://ingest.example.com")


def _signed_headers(form: dict[str, str]) -> dict[str, str]:
    validator = RequestValidator(TEST_AUTH_TOKEN)
    return {"X-Twilio-Signature": validator.compute_signature(WEBHOOK_URL, form)}


def _mock_connection(user_id, open_item_id=None):
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


@pytest.mark.parametrize(
    "content_type",
    ["image/jpeg", "image/png", "image/gif", "image/webp", "application/pdf"],
)
def test_supported_media_types_processed_and_stored(client, content_type):
    user_id = uuid4()
    form = {
        "From": "+15551234567",
        "Body": "",
        "NumMedia": "1",
        "MediaUrl0": "https://api.twilio.com/media/fake",
        "MediaContentType0": content_type,
    }
    mock_download = MagicMock(content=b"fake-bytes", status_code=200)
    mock_download.raise_for_status = MagicMock()
    with (
        patch("ingest_svc.main.get_connection", return_value=_mock_connection(user_id)),
        patch("ingest_svc.main.publish") as mock_publish,
        patch("ingest_svc.main.requests.get", return_value=mock_download) as mock_get,
        patch("ingest_svc.main.storage.Client") as mock_storage_client,
    ):
        resp = client.post("/webhook/sms", data=form, headers=_signed_headers(form))

    assert resp.status_code == 200
    mock_get.assert_called_once()
    mock_storage_client.return_value.bucket.return_value.blob.return_value.upload_from_string.assert_called_once()

    mock_publish.assert_called_once()
    published_message = mock_publish.call_args[0][1]
    assert published_message.mime_type == content_type
    assert published_message.media_uri is not None
    assert published_message.media_uri.startswith("gs://")


def test_unsupported_media_type_rejected_with_400(client):
    form = {
        "From": "+15551234567",
        "Body": "",
        "NumMedia": "1",
        "MediaUrl0": "https://api.twilio.com/media/fake",
        "MediaContentType0": "video/mp4",
    }
    with (
        patch("ingest_svc.main.get_connection") as mock_conn,
        patch("ingest_svc.main.publish") as mock_publish,
        patch("ingest_svc.main.requests.get") as mock_get,
    ):
        resp = client.post("/webhook/sms", data=form, headers=_signed_headers(form))

    assert resp.status_code == 400
    assert "unsupported attachment type" in resp.json()["detail"]
    mock_conn.assert_not_called()
    mock_publish.assert_not_called()
    mock_get.assert_not_called()  # never even attempts to download an unsupported type


def test_text_only_message_has_no_media_regression(client):
    user_id = uuid4()
    form = {"From": "+15551234567", "Body": "pay rent by friday"}
    with (
        patch("ingest_svc.main.get_connection", return_value=_mock_connection(user_id)),
        patch("ingest_svc.main.publish") as mock_publish,
        patch("ingest_svc.main.requests.get") as mock_get,
    ):
        resp = client.post("/webhook/sms", data=form, headers=_signed_headers(form))

    assert resp.status_code == 200
    mock_get.assert_not_called()
    published_message = mock_publish.call_args[0][1]
    assert published_message.media_uri is None
    assert published_message.mime_type is None


def test_media_download_failure_returns_500_before_any_db_write():
    # A dedicated client here, not the shared fixture: needs
    # raise_server_exceptions=False so this unhandled exception surfaces
    # as the 500 FastAPI's own default handler would actually send in
    # production (uvicorn), rather than TestClient's debug-mode re-raise.
    from ingest_svc.main import app

    client = TestClient(app, base_url="https://ingest.example.com", raise_server_exceptions=False)
    form = {
        "From": "+15551234567",
        "Body": "",
        "NumMedia": "1",
        "MediaUrl0": "https://api.twilio.com/media/fake",
        "MediaContentType0": "image/jpeg",
    }
    with (
        patch(
            "ingest_svc.main.get_connection", return_value=_mock_connection(uuid4())
        ) as mock_conn_factory,
        patch("ingest_svc.main.publish") as mock_publish,
        patch("ingest_svc.main.requests.get", side_effect=RuntimeError("network error")),
    ):
        resp = client.post("/webhook/sms", data=form, headers=_signed_headers(form))

    assert resp.status_code == 500
    mock_publish.assert_not_called()
    # get_connection was used once for the routing check, but never again
    # to INSERT an items row — the download failure happens before that.
    assert mock_conn_factory.call_count == 1
