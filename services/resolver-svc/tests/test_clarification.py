"""docs/engineering/test-plan.md step 10 — the real clarification loop.
Gemini itself is mocked (clarify()); a small stateful fake connection
drives the exchange-counting table test across a real multi-call
sequence, since MagicMock alone can't track state between calls."""

import base64
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from obligation_engine_shared.schemas import ExtractedItemMessage
from psycopg.types.json import Json


def _push_envelope(message) -> dict:
    return {"message": {"data": base64.b64encode(message.model_dump_json().encode()).decode()}}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("TWILIO_API_KEY_SECRET", "test-not-a-real-secret")
    from resolver_svc.main import app

    return TestClient(app)


def _extracted_message(**overrides):
    defaults = dict(
        item_id=uuid4(),
        user_id=uuid4(),
        type="obligation",
        title="Pay rent",
        summary="Pay rent, deadline unclear.",
        due_at=None,
        effort_minutes=15,
        focus_depth="shallow",
        confidence=0.95,
        missing_fields=["due_at"],
        reasoning="Deadline missing.",
    )
    defaults.update(overrides)
    return ExtractedItemMessage(**defaults)


class _FakeConn:
    """Tracks one items row + one conversations row across sequential
    execute() calls, enough to drive a real multi-turn exchange."""

    def __init__(self, item_id, user_id, phone="+15551234567", tz="America/Los_Angeles"):
        self.phone = phone
        self.tz = tz
        self.item = {
            "id": str(item_id),
            "type": "obligation",
            "title": "Pay rent",
            "summary": "Pay rent, deadline unclear.",
            "effort_minutes": 15,
            "state": "EXTRACTED",
        }
        self.convo = None
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def commit(self):
        pass

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        result = _Result()
        if "FROM users" in sql:
            result.row = (self.phone, self.tz)
        elif sql.strip().startswith("UPDATE items") and "state = %s" in sql:
            *_, state, _item_id = params
            self.item["state"] = state
        elif "state = 'NEEDS_REVIEW'" in sql:
            self.item["state"] = "NEEDS_REVIEW"
        elif "state = 'AWAITING_CONFIRMATION'" in sql:
            self.item["state"] = "AWAITING_CONFIRMATION"
        elif sql.strip().startswith("INSERT INTO conversations") and "exchange_count" in sql:
            _user_id, _item_id, exchange_count, pending_fields, resolved_fields = params
            self.convo = {
                "pending_fields": pending_fields,
                "resolved_fields": _unwrap(resolved_fields),
                "exchange_count": exchange_count,
            }
        elif sql.strip().startswith("INSERT INTO conversations"):
            _user_id, _item_id, resolved_fields = params
            self.convo = {
                "pending_fields": [],
                "resolved_fields": _unwrap(resolved_fields),
                "exchange_count": 0,
            }
        elif "FROM conversations" in sql:
            result.row = (
                self.convo["pending_fields"],
                self.convo["resolved_fields"],
                self.convo["exchange_count"],
            )
        elif sql.strip().startswith("UPDATE conversations") and "exchange_count" in sql:
            exchange_count, pending_fields, resolved_fields, _item_id = params
            self.convo["exchange_count"] = exchange_count
            self.convo["pending_fields"] = pending_fields
            self.convo["resolved_fields"] = _unwrap(resolved_fields)
        elif sql.strip().startswith("UPDATE conversations"):
            if len(params) == 2:
                resolved_fields, _item_id = params
                self.convo["resolved_fields"] = _unwrap(resolved_fields)
            else:
                resolved_fields, pending_fields, _item_id = params
                self.convo["resolved_fields"] = _unwrap(resolved_fields)
                self.convo["pending_fields"] = pending_fields
        elif sql.strip().startswith("SELECT type, title, summary, effort_minutes, state"):
            result.row = (
                self.item["type"],
                self.item["title"],
                self.item["summary"],
                self.item["effort_minutes"],
                self.item["state"],
            )
        return result


class _Result:
    row = None

    def fetchone(self):
        return self.row


def _unwrap(value):
    return value.obj if isinstance(value, Json) else value


def _clarify_result(due_at_filled=False, due_at=None, still_missing=None, question=None):
    from resolver_svc.clarification import ClarificationResult

    return ClarificationResult(
        due_at_filled=due_at_filled,
        due_at=due_at,
        still_missing=still_missing if still_missing is not None else ["due_at"],
        question=question,
    )


def test_exchange_counting_table(client):
    """A full 3-exchange exhaustion sequence, checked at every step:
    exchange_count only increments on outbound questions, never inbound
    replies, and the 3rd unresolved reply reaches NEEDS_REVIEW with no
    4th question sent (state-machine.md §1.2)."""
    item_id, user_id = uuid4(), uuid4()
    conn = _FakeConn(item_id, user_id)
    extracted = _extracted_message(item_id=item_id, user_id=user_id)

    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main.clarify") as mock_clarify,
        patch("resolver_svc.main._send_sms") as mock_sms,
    ):
        # Turn 1 (via /pubsub/push): first question sent, exchange_count -> 1.
        mock_clarify.return_value = _clarify_result(question="When's rent due?")
        resp = client.post("/pubsub/push", json=_push_envelope(extracted))
        assert resp.status_code == 200
        assert conn.convo["exchange_count"] == 1
        assert conn.item["state"] == "CLARIFYING"
        assert mock_sms.call_count == 1

        # Reply 1, still ambiguous: 2nd question sent, exchange_count -> 2.
        mock_clarify.return_value = _clarify_result(question="Roughly which week?")
        resp = client.post(
            "/reply", json={"user_id": str(user_id), "item_id": str(item_id), "text": "soon"}
        )
        assert resp.json()["status"] == "clarifying"
        assert conn.convo["exchange_count"] == 2
        assert conn.item["state"] == "CLARIFYING"
        assert mock_sms.call_count == 2

        # Reply 2, still ambiguous: 3rd question sent, exchange_count -> 3.
        mock_clarify.return_value = _clarify_result(question="Any rough date at all?")
        resp = client.post(
            "/reply", json={"user_id": str(user_id), "item_id": str(item_id), "text": "idk"}
        )
        assert resp.json()["status"] == "clarifying"
        assert conn.convo["exchange_count"] == 3
        assert mock_sms.call_count == 3

        # Reply 3, still ambiguous: exchange_count already at 3 -> NEEDS_REVIEW,
        # no 4th question. exchange_count itself is untouched by this reply.
        mock_clarify.return_value = _clarify_result(question="One more try?")
        resp = client.post(
            "/reply", json={"user_id": str(user_id), "item_id": str(item_id), "text": "no idea"}
        )
        assert resp.json()["status"] == "needs_review"
        assert conn.item["state"] == "NEEDS_REVIEW"
        assert conn.convo["exchange_count"] == 3  # unchanged — inbound replies never increment it
        assert mock_sms.call_count == 4  # 3 questions + 1 terminal message, not a 4th question
        assert "couldn't get all the details" in mock_sms.call_args.args[1]


def test_single_exchange_resolves_to_awaiting_confirmation(client):
    item_id, user_id = uuid4(), uuid4()
    conn = _FakeConn(item_id, user_id)
    extracted = _extracted_message(item_id=item_id, user_id=user_id)

    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main.clarify") as mock_clarify,
        patch("resolver_svc.main._send_sms") as mock_sms,
    ):
        mock_clarify.return_value = _clarify_result(question="When's rent due?")
        client.post("/pubsub/push", json=_push_envelope(extracted))

        mock_clarify.return_value = _clarify_result(
            due_at_filled=True, due_at="2026-08-28T14:00:00", still_missing=[]
        )
        resp = client.post(
            "/reply",
            json={"user_id": str(user_id), "item_id": str(item_id), "text": "this friday"},
        )

    assert resp.json()["status"] == "awaiting_confirmation"
    assert conn.item["state"] == "AWAITING_CONFIRMATION"
    assert conn.convo["resolved_fields"]["due_at"] == "2026-08-28T14:00:00"
    assert conn.convo["exchange_count"] == 1  # the reply that resolved it never increments
    # 1 clarifying question + 1 confirmation card, not a 3rd clarifying question.
    assert mock_sms.call_count == 2
    assert "Pay rent" in mock_sms.call_args.args[1]


def test_due_at_lands_only_in_conversations_never_an_items_column(client):
    """agent-contracts.md §1: due_at has no items column at all — the
    UPDATE items statement must never reference it."""
    item_id, user_id = uuid4(), uuid4()
    conn = _FakeConn(item_id, user_id)
    extracted = _extracted_message(item_id=item_id, user_id=user_id)

    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main.clarify") as mock_clarify,
        patch("resolver_svc.main._send_sms"),
    ):
        mock_clarify.return_value = _clarify_result(question="When's rent due?")
        client.post("/pubsub/push", json=_push_envelope(extracted))

        mock_clarify.return_value = _clarify_result(
            due_at_filled=True, due_at="2026-08-28T14:00:00", still_missing=[]
        )
        client.post(
            "/reply",
            json={"user_id": str(user_id), "item_id": str(item_id), "text": "this friday"},
        )

    # Every UPDATE items call across the whole exchange — never once
    # references due_at as a column; it only ever lands in
    # conversations.resolved_fields (agent-contracts.md §1).
    items_updates = [sql for sql, _params in conn.calls if sql.strip().startswith("UPDATE items")]
    assert items_updates, "expected at least one UPDATE items call"
    assert all("due_at" not in sql for sql in items_updates)
    assert conn.convo["resolved_fields"] == {"due_at": "2026-08-28T14:00:00"}
