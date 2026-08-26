"""Integration tests against the real dev Postgres (via the Cloud SQL Auth
Proxy) + the real Pub/Sub emulator (for the auto-confirm publish) —
docs/engineering/test-plan.md steps 9/10, now driven by Phase G step D's
unified converse() call and the v1 "no confirmation step" change (main.py's
own module docstring has the full note): the moment still_missing empties
out, the item auto-commits straight to CONFIRMED and publishes to
items.confirmed — no separate AWAITING_CONFIRMATION state, no affirmative
reply required, so there's no Y/N round trip left to test here. Twilio and
the conversation Gemini call are both mocked (a real SMS exchange and a
real multi-turn conversation are the required manual verifications, per
the test plan).

Requires PUBSUB_EMULATOR_HOST, GCP_PROJECT_ID, DB_USER, DB_HOST, DB_PORT.
Skipped automatically otherwise.
"""

import base64
import os
import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from obligation_engine_shared.db import get_connection
from obligation_engine_shared.schemas import ExtractedItemMessage
from resolver_svc.conversation import ConversationTurnResult
from resolver_svc.dedupe import DedupeResult

pytestmark = pytest.mark.skipif(
    "PUBSUB_EMULATOR_HOST" not in os.environ or "DB_USER" not in os.environ,
    reason="requires a live Pub/Sub emulator and Cloud SQL Auth Proxy connection",
)

# The real dedupe check (step 12) hits Vertex AI's embedding API — mocked
# here to a guaranteed no-match, same as converse() is mocked below, so
# these steps-9/10-scoped tests stay about what they're actually testing.
# test_dedupe_integration.py covers the real dedupe path against real
# Postgres/pgvector.
_no_duplicate = patch("resolver_svc.main._check_duplicate", return_value=DedupeResult())


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DB_USER", os.environ["DB_USER"])
    monkeypatch.setenv("TWILIO_API_KEY_SECRET", "test-not-a-real-secret")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest0000000000000000000000000")
    monkeypatch.setenv("TWILIO_API_KEY_SID", "SKtest0000000000000000000000000")
    from resolver_svc.main import app

    return TestClient(app)


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
    yield user_id, phone
    with get_connection() as conn:
        conn.execute(
            "DELETE FROM conversations WHERE item_id IN (SELECT id FROM items WHERE user_id = %s)",
            (str(user_id),),
        )
        conn.execute("DELETE FROM items WHERE user_id = %s", (str(user_id),))
        conn.execute("DELETE FROM users WHERE id = %s", (str(user_id),))
        conn.commit()


def _push_envelope(message) -> dict:
    return {"message": {"data": base64.b64encode(message.model_dump_json().encode()).decode()}}


def test_zero_clarification_path_auto_confirms_and_publishes(
    client, test_user, confirmed_subscription
):
    user_id, _phone = test_user
    subscriber, sub_path = confirmed_subscription
    with get_connection() as conn:
        row = conn.execute(
            "INSERT INTO items (user_id, raw_channel, ingested_at, state) "
            "VALUES (%s, 'sms', now(), 'RECEIVED') RETURNING id",
            (str(user_id),),
        ).fetchone()
        item_id = row[0]
        conn.commit()

    extracted = ExtractedItemMessage(
        item_id=item_id,
        user_id=user_id,
        type="latent",
        title="Learn pottery",
        summary="Someday, no rush.",
        due_at=None,
        effort_minutes=120,
        confidence=0.95,
        missing_fields=[],
        reasoning="Clear latent, no deadline.",
    )
    with (
        patch("resolver_svc.main._send_sms") as mock_sms,
        patch("resolver_svc.main.converse") as mock_converse,
        _no_duplicate,
    ):
        mock_converse.return_value = ConversationTurnResult(
            still_missing=[], reply_text="learn pottery, locked in for someday."
        )
        resp = client.post("/pubsub/push", json=_push_envelope(extracted))
    assert resp.status_code == 200
    mock_sms.assert_called_once()

    with get_connection() as conn:
        state = conn.execute("SELECT state FROM items WHERE id = %s", (str(item_id),)).fetchone()[0]
        convo = conn.execute(
            "SELECT resolved_fields FROM conversations WHERE item_id = %s", (str(item_id),)
        ).fetchone()
    assert state == "CONFIRMED"
    assert convo is not None

    pulled = subscriber.pull(subscription=sub_path, max_messages=1, timeout=10)
    assert len(pulled.received_messages) == 1
    subscriber.acknowledge(
        subscription=sub_path, ack_ids=[m.ack_id for m in pulled.received_messages]
    )


@pytest.fixture
def confirmed_subscription():
    from google.cloud import pubsub_v1

    subscriber = pubsub_v1.SubscriberClient()
    project_id = os.environ["GCP_PROJECT_ID"]
    topic_path = subscriber.topic_path(project_id, "items-confirmed")
    sub_name = f"test-resolver-reply-{uuid.uuid4().hex[:8]}"
    sub_path = subscriber.subscription_path(project_id, sub_name)
    subscriber.create_subscription(name=sub_path, topic=topic_path)
    yield subscriber, sub_path
    subscriber.delete_subscription(subscription=sub_path)


@pytest.fixture
def received_item(test_user):
    user_id, phone = test_user
    with get_connection() as conn:
        row = conn.execute(
            "INSERT INTO items (user_id, raw_channel, ingested_at, state) "
            "VALUES (%s, 'sms', now(), 'RECEIVED') RETURNING id",
            (str(user_id),),
        ).fetchone()
        item_id = row[0]
        conn.commit()
    return item_id, user_id, phone


def _extracted_missing_due_at(item_id, user_id):
    return ExtractedItemMessage(
        item_id=item_id,
        user_id=user_id,
        type="obligation",
        title="Pay rent",
        summary="Pay rent, deadline unclear.",
        due_at=None,
        effort_minutes=15,
        confidence=0.95,
        missing_fields=["due_at"],
        reasoning="Deadline missing.",
    )


def test_single_exchange_resolves_and_auto_confirms(
    client, received_item, confirmed_subscription
):
    item_id, user_id, phone = received_item
    subscriber, sub_path = confirmed_subscription
    extracted = _extracted_missing_due_at(item_id, user_id)

    with (
        patch("resolver_svc.main._send_sms") as mock_sms,
        patch("resolver_svc.main.converse") as mock_converse,
        _no_duplicate,
    ):
        mock_converse.return_value = ConversationTurnResult(
            due_at_filled=False, due_at=None, still_missing=["due_at"],
            reply_text="when's it due?",
        )
        resp = client.post("/pubsub/push", json=_push_envelope(extracted))
        assert resp.json()["status"] == "clarifying"

        mock_converse.return_value = ConversationTurnResult(
            due_at_filled=True, due_at="2026-08-28T14:00:00", still_missing=[],
            reply_text="bet, pay rent locked in for friday 2pm.",
        )
        resp = client.post(
            "/reply", json={"user_id": str(user_id), "item_id": str(item_id), "text": "friday"}
        )

    assert resp.json()["status"] == "confirmed"
    assert mock_sms.call_count == 2  # one question, one confirmation message

    with get_connection() as conn:
        state = conn.execute("SELECT state FROM items WHERE id = %s", (str(item_id),)).fetchone()[0]
        resolved_fields, exchange_count = conn.execute(
            "SELECT resolved_fields, exchange_count FROM conversations WHERE item_id = %s",
            (str(item_id),),
        ).fetchone()
    assert state == "CONFIRMED"
    assert resolved_fields["due_at"] == "2026-08-28T14:00:00"
    assert exchange_count == 1  # the resolving reply itself never increments it

    pulled = subscriber.pull(subscription=sub_path, max_messages=1, timeout=10)
    assert len(pulled.received_messages) == 1
    subscriber.acknowledge(
        subscription=sub_path, ack_ids=[m.ack_id for m in pulled.received_messages]
    )


def test_three_exchange_exhaustion_reaches_needs_review(client, received_item):
    item_id, user_id, phone = received_item
    extracted = _extracted_missing_due_at(item_id, user_id)
    still_ambiguous = ConversationTurnResult(
        due_at_filled=False,
        due_at=None,
        still_missing=["due_at"],
        reply_text="still not sure — when?",
    )

    with (
        patch("resolver_svc.main._send_sms") as mock_sms,
        patch("resolver_svc.main.converse", return_value=still_ambiguous),
        _no_duplicate,
    ):
        client.post("/pubsub/push", json=_push_envelope(extracted))  # exchange 1
        for reply_text in ["soon", "idk", "no idea"]:  # exchanges 2, 3, then exhaustion
            resp = client.post(
                "/reply",
                json={"user_id": str(user_id), "item_id": str(item_id), "text": reply_text},
            )

    assert resp.json()["status"] == "needs_review"
    assert mock_sms.call_count == 4  # 3 questions + 1 terminal message, not a 4th question
    assert "couldn't get all the details" in mock_sms.call_args.args[2]

    with get_connection() as conn:
        state = conn.execute("SELECT state FROM items WHERE id = %s", (str(item_id),)).fetchone()[0]
        exchange_count = conn.execute(
            "SELECT exchange_count FROM conversations WHERE item_id = %s", (str(item_id),)
        ).fetchone()[0]
    assert state == "NEEDS_REVIEW"
    assert exchange_count == 3  # capped, never incremented past the 3rd sent question
