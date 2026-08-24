"""Integration tests against the Pub/Sub emulator + the real dev Postgres
(via the Cloud SQL Auth Proxy) — docs/engineering/test-plan.md step 5.

Requires PUBSUB_EMULATOR_HOST, GCP_PROJECT_ID, DB_USER, DB_HOST, DB_PORT set.
Skipped automatically otherwise.
"""

import base64
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from obligation_engine_shared.db import get_connection
from obligation_engine_shared.schemas import ExtractedItemMessage

pytestmark = pytest.mark.skipif(
    "PUBSUB_EMULATOR_HOST" not in os.environ or "DB_USER" not in os.environ,
    reason="requires a live Pub/Sub emulator and Cloud SQL Auth Proxy connection",
)


@pytest.fixture
def test_item():
    """A throwaway user + RECEIVED item row, so the UPDATE has something to hit."""
    phone = f"+1555{uuid.uuid4().int % 10**7:07d}"
    with get_connection() as conn:
        user_row = conn.execute(
            "INSERT INTO users (phone_e164, timezone) VALUES (%s, %s) RETURNING id",
            (phone, "America/Los_Angeles"),
        ).fetchone()
        user_id = user_row[0]
        item_row = conn.execute(
            """
            INSERT INTO items (user_id, raw_channel, ingested_at, state)
            VALUES (%s, 'sms', now(), 'RECEIVED') RETURNING id
            """,
            (str(user_id),),
        ).fetchone()
        item_id = item_row[0]
        conn.commit()
    yield item_id, user_id
    with get_connection() as conn:
        conn.execute("DELETE FROM items WHERE id = %s", (str(item_id),))
        conn.execute("DELETE FROM users WHERE id = %s", (str(user_id),))
        conn.commit()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DB_USER", os.environ["DB_USER"])
    from resolver_svc.main import app

    return TestClient(app)


@pytest.fixture
def extracted_subscription():
    from google.cloud import pubsub_v1

    subscriber = pubsub_v1.SubscriberClient()
    project_id = os.environ["GCP_PROJECT_ID"]
    topic_path = subscriber.topic_path(project_id, "items-confirmed")
    sub_name = f"test-resolver-integration-{uuid.uuid4().hex[:8]}"
    sub_path = subscriber.subscription_path(project_id, sub_name)
    subscriber.create_subscription(name=sub_path, topic=topic_path)
    yield subscriber, sub_path
    subscriber.delete_subscription(subscription=sub_path)


def test_extracted_to_confirmed_stub(client, test_item, extracted_subscription):
    item_id, user_id = test_item
    subscriber, sub_path = extracted_subscription

    extracted = ExtractedItemMessage(
        item_id=item_id,
        user_id=user_id,
        type="obligation",
        title="Pay rent",
        summary="Pay rent by Friday.",
        due_at=None,
        effort_minutes=15,
        focus_depth="shallow",
        confidence=0.95,
        missing_fields=[],
        reasoning="Clear obligation.",
    )
    envelope = {
        "message": {"data": base64.b64encode(extracted.model_dump_json().encode()).decode()}
    }
    resp = client.post("/pubsub/push", json=envelope)
    assert resp.status_code == 200

    with get_connection() as conn:
        row = conn.execute(
            "SELECT state, title FROM items WHERE id = %s", (str(item_id),)
        ).fetchone()
    assert row[0] == "CONFIRMED"
    assert row[1] == "Pay rent"

    pulled = subscriber.pull(subscription=sub_path, max_messages=1, timeout=10)
    assert len(pulled.received_messages) == 1
    subscriber.acknowledge(
        subscription=sub_path, ack_ids=[m.ack_id for m in pulled.received_messages]
    )
