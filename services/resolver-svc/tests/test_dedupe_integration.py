"""Integration tests for the dedupe check (step 12) against real
Postgres + pgvector — docs/engineering/test-plan.md step 12's
`test_near_duplicate_caught` / `test_dissimilar_item_not_caught`, plus
a real exact-hash-match case. The embedding call itself is mocked to a
controlled fixture vector (matching how clarify() is mocked in
test_resolver_integration.py) — what's actually exercised here for real
is the `dedupe_hash` lookup and the pgvector `<=>` cosine search, which
is what's genuinely new in this step.

Requires DB_USER, DB_HOST, DB_PORT (Cloud SQL Auth Proxy). Skipped
automatically otherwise.
"""

import base64
import os
import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from obligation_engine_shared.db import get_connection
from obligation_engine_shared.schemas import ExtractedItemMessage
from resolver_svc.dedupe import compute_dedupe_hash, vector_literal

pytestmark = pytest.mark.skipif(
    "DB_USER" not in os.environ, reason="requires a live Cloud SQL Auth Proxy connection"
)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DB_USER", os.environ["DB_USER"])
    monkeypatch.setenv("TWILIO_API_KEY_SECRET", "test-not-a-real-secret")
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
        conn.execute(
            "DELETE FROM item_embeddings "
            "WHERE item_id IN (SELECT id FROM items WHERE user_id = %s)",
            (str(user_id),),
        )
        conn.execute("DELETE FROM items WHERE user_id = %s", (str(user_id),))
        conn.execute("DELETE FROM users WHERE id = %s", (str(user_id),))
        conn.commit()


def _push_envelope(message) -> dict:
    return {"message": {"data": base64.b64encode(message.model_dump_json().encode()).decode()}}


def _seed_existing_item(user_id, title, summary, item_type, embedding):
    # dedupe_hash included, matching what _write_item would have set for a
    # real item that went through resolver-svc's own processing — this
    # fixture seeds state directly, bypassing that, so it has to set it
    # itself or the exact-hash-match test below has nothing to match.
    with get_connection() as conn:
        row = conn.execute(
            """
            INSERT INTO items (user_id, raw_channel, ingested_at, state, type, title, summary,
                                effort_minutes, focus_depth, confidence, dedupe_hash)
            VALUES (%s, 'sms', now(), 'AWAITING_CONFIRMATION', %s, %s, %s, 15, 'shallow', 0.9, %s)
            RETURNING id
            """,
            (str(user_id), item_type, title, summary, compute_dedupe_hash(title, summary)),
        ).fetchone()
        item_id = row[0]
        conn.execute(
            "INSERT INTO item_embeddings (item_id, embedding) VALUES (%s, %s::vector)",
            (str(item_id), vector_literal(embedding)),
        )
        conn.commit()
    return item_id


def _new_received_item(user_id):
    with get_connection() as conn:
        row = conn.execute(
            "INSERT INTO items (user_id, raw_channel, ingested_at, state) "
            "VALUES (%s, 'sms', now(), 'RECEIVED') RETURNING id",
            (str(user_id),),
        ).fetchone()
        conn.commit()
    return row[0]


def _vector(seed=1.0, second=0.1, dims=768):
    v = [0.0] * dims
    v[0] = seed
    v[1] = second
    return v


def test_exact_hash_match_skips_embedding_call(client, test_user):
    user_id, _phone = test_user
    _seed_existing_item(user_id, "Pay rent", "Pay rent by Friday.", "obligation", _vector())
    new_item_id = _new_received_item(user_id)

    extracted = ExtractedItemMessage(
        item_id=new_item_id,
        user_id=user_id,
        type="obligation",
        title="pay rent",
        summary="  PAY RENT BY FRIDAY.  ",
        due_at=None,
        effort_minutes=15,
        focus_depth="shallow",
        confidence=0.95,
        missing_fields=[],
        reasoning="Duplicate resend, exact hash match after normalization.",
    )

    with (
        patch("resolver_svc.main._send_sms") as mock_sms,
        patch("resolver_svc.main.embed") as mock_embed,
    ):
        resp = client.post("/pubsub/push", json=_push_envelope(extracted))

    assert resp.status_code == 200
    assert resp.json() == {"status": "duplicate_suspected", "item_id": str(new_item_id)}
    mock_embed.assert_not_called()
    mock_sms.assert_called_once()
    assert '"Pay rent"' in mock_sms.call_args.args[2]

    with get_connection() as conn:
        state = conn.execute(
            "SELECT state FROM items WHERE id = %s", (str(new_item_id),)
        ).fetchone()[0]
    assert state == "DUPLICATE_SUSPECTED"


def test_near_duplicate_caught(client, test_user):
    user_id, _phone = test_user
    _seed_existing_item(user_id, "Pay rent", "Pay rent by Friday.", "obligation", _vector(seed=1.0))
    new_item_id = _new_received_item(user_id)

    extracted = ExtractedItemMessage(
        item_id=new_item_id,
        user_id=user_id,
        type="obligation",
        title="rent due",
        summary="rent is due this friday, $1450",
        due_at=None,
        effort_minutes=15,
        focus_depth="shallow",
        confidence=0.9,
        missing_fields=[],
        reasoning="Different wording, same underlying fact.",
    )

    with (
        patch("resolver_svc.main._send_sms") as mock_sms,
        patch("resolver_svc.main.embed", return_value=_vector(seed=0.995)),  # cosine ~0.9999
    ):
        resp = client.post("/pubsub/push", json=_push_envelope(extracted))

    assert resp.json() == {"status": "duplicate_suspected", "item_id": str(new_item_id)}
    mock_sms.assert_called_once()
    assert '"Pay rent"' in mock_sms.call_args.args[2]
    assert "Reply Y to merge" in mock_sms.call_args.args[2]


def test_dissimilar_item_not_caught(client, test_user):
    user_id, _phone = test_user
    _seed_existing_item(user_id, "Pay rent", "Pay rent by Friday.", "obligation", _vector(seed=1.0))
    new_item_id = _new_received_item(user_id)

    extracted = ExtractedItemMessage(
        item_id=new_item_id,
        user_id=user_id,
        type="latent",
        title="Learn pottery",
        summary="Someday, no rush.",
        due_at=None,
        effort_minutes=120,
        focus_depth="deep",
        confidence=0.9,
        missing_fields=[],
        reasoning="Unrelated idea.",
    )

    unrelated = [0.0] * 768
    unrelated[2] = 1.0  # orthogonal to the seeded [1, 0.1, 0, ...] vector

    with (
        patch("resolver_svc.main._send_sms") as mock_sms,
        patch("resolver_svc.main.embed", return_value=unrelated),
    ):
        resp = client.post("/pubsub/push", json=_push_envelope(extracted))

    assert resp.json()["status"] == "awaiting_confirmation"
    mock_sms.assert_called_once()

    with get_connection() as conn:
        state = conn.execute(
            "SELECT state FROM items WHERE id = %s", (str(new_item_id),)
        ).fetchone()[0]
    assert state == "AWAITING_CONFIRMATION"
