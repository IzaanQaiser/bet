"""Integration tests against the Pub/Sub emulator + the real dev Postgres
(via the Cloud SQL Auth Proxy) — docs/engineering/test-plan.md step 3.

Requires PUBSUB_EMULATOR_HOST, GCP_PROJECT_ID, DB_USER, DB_HOST, DB_PORT set.
Skipped automatically otherwise.
"""

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from obligation_engine_shared.db import get_connection
from twilio.request_validator import RequestValidator

pytestmark = pytest.mark.skipif(
    "PUBSUB_EMULATOR_HOST" not in os.environ or "DB_USER" not in os.environ,
    reason="requires a live Pub/Sub emulator and Cloud SQL Auth Proxy connection",
)

WEBHOOK_URL = "https://ingest.example.com/webhook/sms"


@pytest.fixture
def test_user():
    """A throwaway user row, so the webhook's lookup-or-reject succeeds."""
    phone = f"+1555{uuid.uuid4().int % 10**7:07d}"
    with get_connection() as conn:
        row = conn.execute(
            "INSERT INTO users (phone_e164, timezone) VALUES (%s, %s) RETURNING id",
            (phone, "America/Los_Angeles"),
        ).fetchone()
        conn.commit()
        user_id = row[0]
    yield phone, user_id
    with get_connection() as conn:
        conn.execute("DELETE FROM items WHERE user_id = %s", (str(user_id),))
        conn.execute("DELETE FROM users WHERE id = %s", (str(user_id),))
        conn.commit()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "integration_test_token")
    monkeypatch.setenv("DB_USER", os.environ["DB_USER"])
    from ingest_svc.main import app

    return TestClient(app, base_url="https://ingest.example.com")


def _signed_headers(form: dict[str, str]) -> dict[str, str]:
    validator = RequestValidator("integration_test_token")
    return {"X-Twilio-Signature": validator.compute_signature(WEBHOOK_URL, form)}


def test_valid_webhook_creates_item_and_publishes(client, test_user):
    phone, user_id = test_user
    form = {"From": phone, "Body": "pay rent by friday"}
    resp = client.post("/webhook/sms", data=form, headers=_signed_headers(form))
    assert resp.status_code == 200
    item_id = resp.json()["item_id"]

    with get_connection() as conn:
        row = conn.execute("SELECT state, user_id FROM items WHERE id = %s", (item_id,)).fetchone()
    assert row is not None
    assert row[0] == "RECEIVED"
    assert str(row[1]) == str(user_id)


def test_invalid_webhook_creates_nothing(client, test_user):
    phone, user_id = test_user
    form = {"From": phone, "Body": "pay rent by friday"}
    bad_headers = {"X-Twilio-Signature": "not-a-real-signature"}

    with get_connection() as conn:
        before = conn.execute(
            "SELECT count(*) FROM items WHERE user_id = %s", (str(user_id),)
        ).fetchone()[0]

    resp = client.post("/webhook/sms", data=form, headers=bad_headers)
    assert resp.status_code == 403

    with get_connection() as conn:
        after = conn.execute(
            "SELECT count(*) FROM items WHERE user_id = %s", (str(user_id),)
        ).fetchone()[0]
    assert after == before
