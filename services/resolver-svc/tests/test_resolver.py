"""docs/engineering/test-plan.md step 9 — DB and Twilio mocked, per
endpoint. Templates are covered in test_confirmation_card.py."""

import base64
from datetime import datetime
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from obligation_engine_shared.schemas import ExtractedItemMessage


def _push_envelope(message) -> dict:
    return {"message": {"data": base64.b64encode(message.model_dump_json().encode()).decode()}}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("TWILIO_API_KEY_SECRET", "test-not-a-real-secret")
    from resolver_svc.main import app

    return TestClient(app)


def _mock_connection(*, phone="+15551234567", item_row=None, conversation_row=None):
    def execute_side_effect(sql, params=None):
        result = MagicMock()
        if "FROM users" in sql:
            result.fetchone.return_value = (phone,)
        elif "FROM items" in sql:
            result.fetchone.return_value = item_row
        elif "FROM conversations" in sql:
            result.fetchone.return_value = conversation_row
        else:
            result.fetchone.return_value = None
        return result

    conn = MagicMock()
    conn.execute.side_effect = execute_side_effect
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn


def _extracted_message(**overrides):
    # type="obligation" + missing_fields=[] implies a real due_at by the
    # extractor's own contract (agent-contracts.md §2) — an obligation
    # with no known due_at always has "due_at" in missing_fields, so this
    # combination never occurs for real. Kept realistic here rather than
    # defended against in resolver-svc itself.
    defaults = dict(
        item_id=uuid4(),
        user_id=uuid4(),
        type="obligation",
        title="Pay rent",
        summary="Pay rent by Friday.",
        due_at=datetime(2026, 9, 4, 14, 0),
        effort_minutes=15,
        focus_depth="shallow",
        confidence=0.95,
        missing_fields=[],
        reasoning="Clear obligation.",
    )
    defaults.update(overrides)
    return ExtractedItemMessage(**defaults)


# --- /pubsub/push ------------------------------------------------------


def test_complete_confident_item_awaits_confirmation(client):
    extracted = _extracted_message()
    conn = _mock_connection()
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main._send_sms") as mock_sms,
    ):
        resp = client.post("/pubsub/push", json=_push_envelope(extracted))

    assert resp.status_code == 200
    assert resp.json() == {"status": "awaiting_confirmation", "item_id": str(extracted.item_id)}

    update_calls = [c for c in conn.execute.call_args_list if "UPDATE items" in c.args[0]]
    assert update_calls[0].args[1][-2] == "AWAITING_CONFIRMATION"  # state param

    insert_calls = [
        c for c in conn.execute.call_args_list if "INSERT INTO conversations" in c.args[0]
    ]
    assert len(insert_calls) == 1

    mock_sms.assert_called_once()
    assert "Pay rent" in mock_sms.call_args.args[1]


def test_incomplete_item_left_in_extracted(client):
    extracted = _extracted_message(missing_fields=["due_at"])
    conn = _mock_connection()
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main._send_sms") as mock_sms,
    ):
        resp = client.post("/pubsub/push", json=_push_envelope(extracted))

    assert resp.status_code == 200
    assert resp.json()["status"] == "left_in_extracted"
    update_calls = [c for c in conn.execute.call_args_list if "UPDATE items" in c.args[0]]
    assert update_calls[0].args[1][-2] == "EXTRACTED"
    mock_sms.assert_not_called()


def test_low_confidence_item_left_in_extracted_despite_no_missing_fields(client):
    extracted = _extracted_message(missing_fields=[], confidence=0.5)
    conn = _mock_connection()
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main._send_sms") as mock_sms,
    ):
        resp = client.post("/pubsub/push", json=_push_envelope(extracted))

    assert resp.json()["status"] == "left_in_extracted"
    mock_sms.assert_not_called()


def test_malformed_envelope_returns_500_for_retry(client):
    resp = client.post("/pubsub/push", json={"message": {"data": "not-valid-base64json"}})
    assert resp.status_code == 500


def test_sms_send_failure_returns_500(client):
    extracted = _extracted_message()
    conn = _mock_connection()
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main._send_sms", side_effect=RuntimeError("twilio down")),
    ):
        resp = client.post("/pubsub/push", json=_push_envelope(extracted))
    assert resp.status_code == 500


# --- /reply --------------------------------------------------------------


def test_y_reply_publishes_confirmed_and_marks_confirmed(client):
    item_id, user_id = str(uuid4()), str(uuid4())
    conn = _mock_connection(
        item_row=("obligation", "Pay rent", "Pay rent by Friday.", 15),
        conversation_row=({"due_at": "2026-09-04T14:00:00"},),
    )
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main.publish") as mock_publish,
    ):
        resp = client.post("/reply", json={"user_id": user_id, "item_id": item_id, "text": "y"})

    assert resp.status_code == 200
    assert resp.json() == {"status": "confirmed", "item_id": item_id}

    mock_publish.assert_called_once()
    topic, confirmed = mock_publish.call_args[0]
    assert topic == "items-confirmed"
    assert str(confirmed.item_id) == item_id
    assert confirmed.action_type == "calendar"
    assert confirmed.due_at.isoformat() == "2026-09-04T14:00:00"

    update_calls = [c for c in conn.execute.call_args_list if "UPDATE items" in c.args[0]]
    assert "CONFIRMED" in update_calls[0].args[0]


def test_n_reply_cancels_no_publish(client):
    item_id, user_id = str(uuid4()), str(uuid4())
    conn = _mock_connection(item_row=("latent", "Learn pottery", "Someday.", 120))
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main.publish") as mock_publish,
        patch("resolver_svc.main._send_sms") as mock_sms,
    ):
        resp = client.post("/reply", json={"user_id": user_id, "item_id": item_id, "text": "no"})

    assert resp.status_code == 200
    assert resp.json() == {"status": "cancelled", "item_id": item_id}
    mock_publish.assert_not_called()
    mock_sms.assert_called_once_with("+15551234567", "Cancelled.")

    update_calls = [c for c in conn.execute.call_args_list if "UPDATE items" in c.args[0]]
    assert "CANCELLED" in update_calls[0].args[0]


def test_other_reply_logged_not_acted_on(client):
    item_id, user_id = str(uuid4()), str(uuid4())
    conn = _mock_connection(item_row=("obligation", "Pay rent", "Pay rent by Friday.", 15))
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main.publish") as mock_publish,
        patch("resolver_svc.main._send_sms") as mock_sms,
    ):
        resp = client.post(
            "/reply", json={"user_id": user_id, "item_id": item_id, "text": "move it to Friday"}
        )

    assert resp.status_code == 200
    assert resp.json() == {"status": "unhandled_reply", "item_id": item_id}
    mock_publish.assert_not_called()
    mock_sms.assert_not_called()
    update_calls = [c for c in conn.execute.call_args_list if "UPDATE items" in c.args[0]]
    assert update_calls == []


def test_reply_unknown_item_returns_404(client):
    item_id, user_id = str(uuid4()), str(uuid4())
    conn = _mock_connection(item_row=None)
    with patch("resolver_svc.main.get_connection", return_value=conn):
        resp = client.post("/reply", json={"user_id": user_id, "item_id": item_id, "text": "y"})
    assert resp.status_code == 404


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
