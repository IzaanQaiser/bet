"""docs/engineering/test-plan.md step 9 (confirmation) + step 10
(clarification) — DB and Twilio mocked, per endpoint. Templates are
covered in test_confirmation_card.py; the multi-turn clarification
sequence itself (exchange counting, NEEDS_REVIEW) is covered in
test_clarification.py."""

import base64
from datetime import datetime
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from obligation_engine_shared.schemas import ExtractedItemMessage
from resolver_svc.dedupe import DedupeResult


def _push_envelope(message) -> dict:
    return {"message": {"data": base64.b64encode(message.model_dump_json().encode()).decode()}}


def _no_duplicate():
    """Patches resolver_svc.main._check_duplicate for every /pubsub/push
    test below that isn't itself testing the dedupe path (test_dedupe.py
    covers the threshold logic in isolation; the DUPLICATE_SUSPECTED
    integration is covered further down in this file) — real dedupe
    checks hit Vertex AI and Postgres, neither available to a unit test."""
    return patch("resolver_svc.main._check_duplicate", return_value=DedupeResult())


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("TWILIO_API_KEY_SECRET", "test-not-a-real-secret")
    from resolver_svc.main import app

    return TestClient(app)


def _mock_connection(
    *, phone="+15551234567", tz="America/Los_Angeles", item_row=None, conversation_row=None
):
    def execute_side_effect(sql, params=None):
        result = MagicMock()
        if "FROM users" in sql:
            result.fetchone.return_value = (phone, tz)
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


# --- /pubsub/push, complete-extraction path -----------------------------


def test_complete_item_awaits_confirmation(client):
    extracted = _extracted_message()
    conn = _mock_connection()
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main._send_sms") as mock_sms,
        _no_duplicate(),
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


def test_low_confidence_complete_item_still_awaits_confirmation(client):
    """state-machine.md §1.2's "Resolved gap": low confidence alone (no
    missing fields) isn't a field-completeness problem — the confirmation
    card's own "or send a correction" is the safety net for it, not a
    manufactured clarifying question about nothing."""
    extracted = _extracted_message(confidence=0.4)
    conn = _mock_connection()
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main._send_sms") as mock_sms,
        _no_duplicate(),
    ):
        resp = client.post("/pubsub/push", json=_push_envelope(extracted))
    assert resp.json()["status"] == "awaiting_confirmation"
    mock_sms.assert_called_once()


def test_missing_fields_starts_clarification_not_left_stalled(client):
    """Step 10 replaces step 9's "left in EXTRACTED, do nothing" — an
    incomplete item now gets a real clarifying question."""
    extracted = _extracted_message(missing_fields=["due_at"], due_at=None)
    conn = _mock_connection()
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main.clarify") as mock_clarify,
        patch("resolver_svc.main._send_sms") as mock_sms,
        _no_duplicate(),
    ):
        from resolver_svc.clarification import ClarificationResult

        mock_clarify.return_value = ClarificationResult(
            due_at_filled=False, due_at=None, still_missing=["due_at"], question="When's it due?"
        )
        resp = client.post("/pubsub/push", json=_push_envelope(extracted))

    assert resp.status_code == 200
    assert resp.json() == {"status": "clarifying", "item_id": str(extracted.item_id)}
    mock_sms.assert_called_once_with("+15551234567", "When's it due?")
    update_calls = [c for c in conn.execute.call_args_list if "UPDATE items" in c.args[0]]
    assert update_calls[0].args[1][-2] == "CLARIFYING"


def test_malformed_envelope_returns_500_for_retry(client):
    resp = client.post("/pubsub/push", json={"message": {"data": "not-valid-base64json"}})
    assert resp.status_code == 500


def test_sms_send_failure_returns_500(client):
    extracted = _extracted_message()
    conn = _mock_connection()
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main._send_sms", side_effect=RuntimeError("twilio down")),
        _no_duplicate(),
    ):
        resp = client.post("/pubsub/push", json=_push_envelope(extracted))
    assert resp.status_code == 500


# --- /reply, AWAITING_CONFIRMATION path ----------------------------------


def test_y_reply_publishes_confirmed_and_marks_confirmed(client):
    item_id, user_id = str(uuid4()), str(uuid4())
    conn = _mock_connection(
        item_row=("obligation", "Pay rent", "Pay rent by Friday.", 15, "AWAITING_CONFIRMATION"),
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
    conn = _mock_connection(
        item_row=("latent", "Learn pottery", "Someday.", 120, "AWAITING_CONFIRMATION")
    )
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


def test_other_reply_during_confirmation_logged_not_acted_on(client):
    item_id, user_id = str(uuid4()), str(uuid4())
    conn = _mock_connection(
        item_row=("obligation", "Pay rent", "Pay rent by Friday.", 15, "AWAITING_CONFIRMATION")
    )
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


def test_reply_unexpected_state_does_not_crash(client):
    item_id, user_id = str(uuid4()), str(uuid4())
    conn = _mock_connection(
        item_row=("obligation", "Pay rent", "Pay rent by Friday.", 15, "COMMITTED")
    )
    with patch("resolver_svc.main.get_connection", return_value=conn):
        resp = client.post("/reply", json={"user_id": user_id, "item_id": item_id, "text": "y"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "unexpected_state"


# --- /pubsub/push, dedupe path (step 12) ---------------------------------


def test_duplicate_found_routes_to_duplicate_suspected_not_clarification(client):
    """A duplicate match short-circuits before the completeness check ever
    runs (state-machine.md §1.1) — even an item with missing_fields set
    goes to DUPLICATE_SUSPECTED, not CLARIFYING."""
    extracted = _extracted_message(missing_fields=["due_at"], due_at=None)
    existing_id = uuid4()
    conn = _mock_connection()
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main._send_sms") as mock_sms,
        patch(
            "resolver_svc.main._check_duplicate",
            return_value=DedupeResult(duplicate_item_id=existing_id, duplicate_title="Pay rent"),
        ),
    ):
        resp = client.post("/pubsub/push", json=_push_envelope(extracted))

    assert resp.status_code == 200
    assert resp.json() == {"status": "duplicate_suspected", "item_id": str(extracted.item_id)}
    mock_sms.assert_called_once()
    assert '"Pay rent"' in mock_sms.call_args.args[1]
    assert "Reply Y to merge" in mock_sms.call_args.args[1]

    update_calls = [c for c in conn.execute.call_args_list if "UPDATE items" in c.args[0]]
    assert update_calls[0].args[1][-2] == "DUPLICATE_SUSPECTED"


# --- /reply, DUPLICATE_SUSPECTED path (step 12) ---------------------------


def test_duplicate_y_reply_merges_no_publish(client):
    item_id, user_id = str(uuid4()), str(uuid4())
    conn = _mock_connection(
        item_row=("obligation", "Pay rent", "Pay rent by Friday.", 15, "DUPLICATE_SUSPECTED"),
        conversation_row=(
            None,
            {"_dedupe_match_item_id": str(uuid4()), "_dedupe_match_title": "Pay rent"},
        ),
    )
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main.publish") as mock_publish,
        patch("resolver_svc.main._send_sms") as mock_sms,
    ):
        resp = client.post("/reply", json={"user_id": user_id, "item_id": item_id, "text": "y"})

    assert resp.status_code == 200
    assert resp.json() == {"status": "merged", "item_id": item_id}
    mock_publish.assert_not_called()
    mock_sms.assert_called_once_with(
        "+15551234567", 'Got it — that\'s the same as "Pay rent". Nothing new added.'
    )
    update_calls = [c for c in conn.execute.call_args_list if "UPDATE items" in c.args[0]]
    assert "MERGED" in update_calls[0].args[0]


def test_duplicate_n_reply_no_missing_fields_awaits_confirmation(client):
    item_id, user_id = str(uuid4()), str(uuid4())
    conn = _mock_connection(
        item_row=("obligation", "Pay rent", "Pay rent by Friday.", 15, "DUPLICATE_SUSPECTED"),
        conversation_row=(
            [],
            {"due_at": "2026-09-04T14:00:00", "_dedupe_match_item_id": str(uuid4())},
        ),
    )
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main._send_sms") as mock_sms,
    ):
        resp = client.post("/reply", json={"user_id": user_id, "item_id": item_id, "text": "n"})

    assert resp.status_code == 200
    assert resp.json() == {"status": "awaiting_confirmation", "item_id": item_id}
    mock_sms.assert_called_once()
    assert "Pay rent" in mock_sms.call_args.args[1]
    update_calls = [c for c in conn.execute.call_args_list if "UPDATE items" in c.args[0]]
    assert "AWAITING_CONFIRMATION" in update_calls[0].args[0]


def test_duplicate_n_reply_with_missing_fields_resumes_clarification(client):
    item_id, user_id = str(uuid4()), str(uuid4())
    conn = _mock_connection(
        item_row=("obligation", "Pay rent", "Pay rent by Friday.", 15, "DUPLICATE_SUSPECTED"),
        conversation_row=(["due_at"], {"_dedupe_match_item_id": str(uuid4())}),
    )
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main.clarify") as mock_clarify,
        patch("resolver_svc.main._send_sms") as mock_sms,
    ):
        from resolver_svc.clarification import ClarificationResult

        mock_clarify.return_value = ClarificationResult(
            due_at_filled=False, due_at=None, still_missing=["due_at"], question="When's it due?"
        )
        resp = client.post("/reply", json={"user_id": user_id, "item_id": item_id, "text": "n"})

    assert resp.status_code == 200
    assert resp.json() == {"status": "clarifying", "item_id": item_id}
    mock_sms.assert_called_once_with("+15551234567", "When's it due?")
    update_calls = [c for c in conn.execute.call_args_list if "UPDATE items" in c.args[0]]
    assert "CLARIFYING" in update_calls[0].args[0]


# --- /reply, thread-attach (step 12) --------------------------------------


def test_attach_reply_sets_parent_item_id(client):
    item_id, user_id = str(uuid4()), str(uuid4())
    target_id = str(uuid4())
    conn = _mock_connection(
        item_row=("latent", "Learn pottery", "Someday.", 120, "AWAITING_CONFIRMATION"),
        conversation_row=(
            {"_thread_attach_item_id": target_id, "_thread_attach_title": "Take a ceramics class"},
        ),
    )
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main._send_sms") as mock_sms,
    ):
        resp = client.post("/reply", json={"user_id": user_id, "item_id": item_id, "text": "a"})

    assert resp.status_code == 200
    assert resp.json() == {"status": "attached", "item_id": item_id}
    mock_sms.assert_called_once_with("+15551234567", 'Attached to "Take a ceramics class".')
    update_calls = [c for c in conn.execute.call_args_list if "UPDATE items" in c.args[0]]
    assert update_calls[0].args[0].strip().startswith("UPDATE items SET parent_item_id")
    assert update_calls[0].args[1][0] == target_id


def test_attach_reply_with_no_candidate_is_unhandled(client):
    item_id, user_id = str(uuid4()), str(uuid4())
    conn = _mock_connection(
        item_row=("latent", "Learn pottery", "Someday.", 120, "AWAITING_CONFIRMATION"),
        conversation_row=({},),
    )
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main._send_sms") as mock_sms,
    ):
        resp = client.post("/reply", json={"user_id": user_id, "item_id": item_id, "text": "a"})

    assert resp.status_code == 200
    assert resp.json() == {"status": "unhandled_reply", "item_id": item_id}
    mock_sms.assert_not_called()


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
