"""Integration test against the real Pub/Sub emulator + the real dev
Postgres (via the Cloud SQL Auth Proxy) + the **real** GCS media bucket —
docs/engineering/test-plan.md step 11's `test_mms_end_to_end`, the
ingest-svc half. Only the Twilio media download itself is mocked (no
real Twilio-hosted media URL to fetch in a test); the GCS upload is
real, read back afterward to confirm the object genuinely exists.

Requires PUBSUB_EMULATOR_HOST, GCP_PROJECT_ID, DB_USER, DB_HOST, DB_PORT.
Skipped automatically otherwise.
"""

import os
import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from obligation_engine_shared.db import get_connection
from twilio.request_validator import RequestValidator

pytestmark = pytest.mark.skipif(
    "PUBSUB_EMULATOR_HOST" not in os.environ or "DB_USER" not in os.environ,
    reason="requires a live Pub/Sub emulator and Cloud SQL Auth Proxy connection",
)

WEBHOOK_URL = "https://ingest.example.com/webhook/sms"
TEST_AUTH_TOKEN = "integration_test_token"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", TEST_AUTH_TOKEN)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest0000000000000000000000000000")
    monkeypatch.setenv("DB_USER", os.environ["DB_USER"])
    from ingest_svc.main import app

    return TestClient(app, base_url="https://ingest.example.com")


@pytest.fixture
def test_user():
    phone = f"+1555{uuid.uuid4().int % 10**7:07d}"
    with get_connection() as conn:
        row = conn.execute(
            "INSERT INTO users (phone_e164, timezone) VALUES (%s, %s) RETURNING id",
            (phone, "America/Los_Angeles"),
        ).fetchone()
        user_id = row[0]
        conn.commit()
    yield phone, user_id
    with get_connection() as conn:
        conn.execute("DELETE FROM items WHERE user_id = %s", (str(user_id),))
        conn.execute("DELETE FROM users WHERE id = %s", (str(user_id),))
        conn.commit()


def _signed_headers(form: dict[str, str]) -> dict[str, str]:
    validator = RequestValidator(TEST_AUTH_TOKEN)
    return {"X-Twilio-Signature": validator.compute_signature(WEBHOOK_URL, form)}


def test_mms_stores_media_in_real_gcs_and_publishes(client, test_user):
    from google.cloud import storage

    phone, user_id = test_user
    form = {
        "From": phone,
        "Body": "",
        "NumMedia": "1",
        "MediaUrl0": "https://api.twilio.com/media/fake",
        "MediaContentType0": "image/jpeg",
    }
    fixture_bytes = b"\xff\xd8\xff\xe0fake-jpeg-bytes-for-integration-test"
    mock_download = MagicMock(content=fixture_bytes, status_code=200)
    mock_download.raise_for_status = MagicMock()

    with patch("ingest_svc.main.requests.get", return_value=mock_download):
        resp = client.post("/webhook/sms", data=form, headers=_signed_headers(form))
    assert resp.status_code == 200
    item_id = resp.json()["item_id"]

    with get_connection() as conn:
        raw_media_uri = conn.execute(
            "SELECT raw_media_uri FROM items WHERE id = %s", (item_id,)
        ).fetchone()[0]
    assert raw_media_uri == f"gs://obligation-engine-hack-media/{item_id}.jpg"

    # Read the object back for real — proves the upload genuinely happened,
    # not just that upload_from_string was called on a mock.
    bucket_name, _, blob_name = raw_media_uri.removeprefix("gs://").partition("/")
    blob = storage.Client().bucket(bucket_name).blob(blob_name)
    assert blob.exists()
    assert blob.download_as_bytes() == fixture_bytes
    blob.delete()
