"""Unit tests — DB and Pub/Sub mocked out, per docs/engineering/test-plan.md
step 5. Proves only the stub's actual scope: complete items auto-confirm,
incomplete items are left alone."""

import base64
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from obligation_engine_shared.schemas import ExtractedItemMessage


def _push_envelope(message) -> dict:
    return {"message": {"data": base64.b64encode(message.model_dump_json().encode()).decode()}}


@pytest.fixture
def client():
    from resolver_svc.main import app

    return TestClient(app)


def _mock_connection():
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn


def _extracted_message(**overrides):
    defaults = dict(
        item_id=uuid4(),
        user_id=uuid4(),
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
    defaults.update(overrides)
    return ExtractedItemMessage(**defaults)


def test_complete_item_auto_confirms(client):
    extracted = _extracted_message()
    conn = _mock_connection()
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main.publish") as mock_publish,
    ):
        resp = client.post("/pubsub/push", json=_push_envelope(extracted))

    assert resp.status_code == 200
    assert resp.json() == {"status": "confirmed", "item_id": str(extracted.item_id)}

    update_sql, params = conn.execute.call_args[0]
    assert "state = %s" in update_sql
    assert params[-2] == "CONFIRMED"  # state param, second-to-last (item_id is last)

    mock_publish.assert_called_once()
    topic, confirmed = mock_publish.call_args[0]
    assert topic == "items-confirmed"
    assert confirmed.item_id == extracted.item_id
    assert confirmed.action_type == "calendar"
    assert confirmed.due_at is None


def test_latent_confirms_with_no_action_type(client):
    extracted = _extracted_message(type="latent", due_at=None)
    conn = _mock_connection()
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main.publish") as mock_publish,
    ):
        client.post("/pubsub/push", json=_push_envelope(extracted))

    confirmed = mock_publish.call_args[0][1]
    assert confirmed.action_type is None


def test_incomplete_item_left_in_extracted(client):
    extracted = _extracted_message(missing_fields=["due_at"], due_at=None)
    conn = _mock_connection()
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main.publish") as mock_publish,
    ):
        resp = client.post("/pubsub/push", json=_push_envelope(extracted))

    assert resp.status_code == 200
    assert resp.json() == {"status": "left_in_extracted", "item_id": str(extracted.item_id)}

    update_sql, params = conn.execute.call_args[0]
    assert params[-2] == "EXTRACTED"
    mock_publish.assert_not_called()


def test_malformed_envelope_returns_500_for_retry(client):
    with patch("resolver_svc.main.publish") as mock_publish:
        resp = client.post("/pubsub/push", json={"message": {"data": "not-valid-base64json"}})
    assert resp.status_code == 500
    mock_publish.assert_not_called()


def test_db_write_failure_returns_500_for_retry(client):
    extracted = _extracted_message()
    with (
        patch("resolver_svc.main.get_connection", side_effect=RuntimeError("db down")),
        patch("resolver_svc.main.publish") as mock_publish,
    ):
        resp = client.post("/pubsub/push", json=_push_envelope(extracted))
    assert resp.status_code == 500
    mock_publish.assert_not_called()


def test_publish_failure_returns_500_for_retry(client):
    extracted = _extracted_message()
    conn = _mock_connection()
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main.publish", side_effect=RuntimeError("pubsub down")),
    ):
        resp = client.post("/pubsub/push", json=_push_envelope(extracted))
    assert resp.status_code == 500


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
