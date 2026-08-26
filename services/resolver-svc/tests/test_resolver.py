"""docs/engineering/test-plan.md step 9 (confirmation) + step 10
(clarification) — DB and Twilio mocked, per endpoint. Phase G step D
replaced clarify()/render_confirmation_card/classify_reply with
resolver_svc.conversation.converse() for this flow; converse() itself is
mocked here, same as clarify() was before it."""

import base64
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from obligation_engine_shared.schemas import ExtractedItemMessage
from resolver_svc.conversation import ConversationTurnResult
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
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest0000000000000000000000000")
    monkeypatch.setenv("TWILIO_API_KEY_SID", "SKtest0000000000000000000000000")
    from resolver_svc.main import app

    return TestClient(app)


def _mock_connection(
    *,
    phone="+15551234567",
    tz="America/Los_Angeles",
    item_row=None,
    conversation_row=None,
    other_items_rows=None,
):
    def execute_side_effect(sql, params=None):
        result = MagicMock()
        if "FROM users" in sql:
            result.fetchone.return_value = (phone, tz)
        elif "JOIN obligations" in sql:
            result.fetchall.return_value = other_items_rows or []  # _other_items_context
        elif "FROM items" in sql:
            result.fetchone.return_value = item_row
        elif "FROM conversations" in sql:
            result.fetchone.return_value = conversation_row
        elif "FROM messages" in sql:
            result.fetchall.return_value = []  # _recent_history, empty by default
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
        patch("resolver_svc.main.converse") as mock_converse,
        _no_duplicate(),
    ):
        mock_converse.return_value = ConversationTurnResult(
            still_missing=[], reply_text="bet, pay rent friday 2pm — sound good?"
        )
        resp = client.post("/pubsub/push", json=_push_envelope(extracted))

    assert resp.status_code == 200
    assert resp.json() == {"status": "awaiting_confirmation", "item_id": str(extracted.item_id)}

    # _write_item's own transient CLARIFYING write happens first (same
    # code path missing-fields items use); the final write flips it to
    # AWAITING_CONFIRMATION once converse() resolves everything on the
    # first pass (a literal in the SQL text itself, not parameterized) —
    # check the last state write, not the first.
    update_calls = [c for c in conn.execute.call_args_list if "UPDATE items" in c.args[0]]
    assert "AWAITING_CONFIRMATION" in update_calls[-1].args[0]

    insert_calls = [
        c for c in conn.execute.call_args_list if "INSERT INTO conversations" in c.args[0]
    ]
    assert len(insert_calls) == 1

    mock_sms.assert_called_once()
    assert "pay rent" in mock_sms.call_args.args[2]


def test_complete_email_item_shows_email_confirmation_card(client):
    """Step 15 — a fully-resolved email action (recipient already present
    at extraction) stages action_type/email_recipient/email_draft in
    resolved_fields exactly as before; Phase G step D moved the actual
    outbound text from a fixed template to converse()'s reply_text, which
    is mocked here rather than asserted on for exact wording."""
    extracted = _extracted_message(
        title="Reply to Sarah",
        summary="Confirm the delay.",
        due_at=None,
        missing_fields=[],
        action_type="email",
        email_recipient="sarah@example.com",
        email_draft="Hi Sarah,\n\nConfirming the delay.\n\nThanks",
    )
    conn = _mock_connection()
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main._send_sms") as mock_sms,
        patch("resolver_svc.main.converse") as mock_converse,
        _no_duplicate(),
    ):
        mock_converse.return_value = ConversationTurnResult(
            still_missing=[], reply_text="bet, sending sarah the delay email — good to go?"
        )
        resp = client.post("/pubsub/push", json=_push_envelope(extracted))

    assert resp.status_code == 200
    assert resp.json()["status"] == "awaiting_confirmation"
    mock_sms.assert_called_once()

    insert_calls = [
        c for c in conn.execute.call_args_list if "INSERT INTO conversations" in c.args[0]
    ]
    # (user_id, item_id, exchange_count, pending_fields, resolved_fields) —
    # resolved_fields (the Json wrapper) is the last param.
    resolved_fields = insert_calls[0].args[1][-1].obj
    assert resolved_fields["action_type"] == "email"
    assert resolved_fields["email_recipient"] == "sarah@example.com"


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
        patch("resolver_svc.main.converse") as mock_converse,
        _no_duplicate(),
    ):
        mock_converse.return_value = ConversationTurnResult(
            still_missing=[], reply_text="bet, pay rent friday 2pm — sound good?"
        )
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
        patch("resolver_svc.main.converse") as mock_converse,
        patch("resolver_svc.main._send_sms") as mock_sms,
        _no_duplicate(),
    ):
        mock_converse.return_value = ConversationTurnResult(
            due_at_filled=False, due_at=None, still_missing=["due_at"],
            reply_text="When's it due?",
        )
        resp = client.post("/pubsub/push", json=_push_envelope(extracted))

    assert resp.status_code == 200
    assert resp.json() == {"status": "clarifying", "item_id": str(extracted.item_id)}
    mock_sms.assert_called_once_with(extracted.user_id, "+15551234567", "When's it due?")
    update_calls = [c for c in conn.execute.call_args_list if "UPDATE items" in c.args[0]]
    assert update_calls[0].args[1][-2] == "CLARIFYING"


def test_unrelated_reply_during_clarification_routes_as_new_item(client):
    """Conversation-continuity follow-up (main.py module docstring): a
    reply that arrives while an item is CLARIFYING but doesn't actually
    relate to it (converse() sets relates_to_item=False) must not be
    force-merged into the pending field — the open item is left completely
    untouched (no items/conversations UPDATE at all) and the text gets its
    own brand-new item via create_raw_item + an items-raw publish, exactly
    like a first-contact message."""
    item_id, user_id = str(uuid4()), str(uuid4())
    conn = _mock_connection(
        item_row=("obligation", "Pay rent", "Pay rent, deadline unclear.", 15, False, "CLARIFYING"),
        conversation_row=(["due_at"], {}, 0),
    )
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main.publish") as mock_publish,
        patch("resolver_svc.main._send_sms") as mock_sms,
        patch("resolver_svc.main.converse") as mock_converse,
    ):
        mock_converse.return_value = ConversationTurnResult(relates_to_item=False, reply_text="")
        resp = client.post(
            "/reply",
            json={"user_id": user_id, "item_id": item_id, "text": "remind me to call mom tomorrow"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "routed_as_new_item"
    assert body["item_id"] == item_id
    assert UUID(body["new_item_id"]) != UUID(item_id)

    mock_sms.assert_not_called()  # the new item's own turn replies, not this one
    mock_publish.assert_called_once()
    topic, message = mock_publish.call_args.args
    assert topic == "items-raw"
    assert message.text == "remind me to call mom tomorrow"
    assert str(message.item_id) == body["new_item_id"]

    # the original CLARIFYING item is genuinely untouched
    update_calls = [c for c in conn.execute.call_args_list if "UPDATE items" in c.args[0]]
    convo_updates = [c for c in conn.execute.call_args_list if "UPDATE conversations" in c.args[0]]
    assert update_calls == []
    assert convo_updates == []
    insert_calls = [c for c in conn.execute.call_args_list if "INSERT INTO items" in c.args[0]]
    assert len(insert_calls) == 1  # the new item's own RECEIVED row


def test_unrelated_reply_during_confirmation_routes_as_new_item(client):
    """Same escape hatch, at the AWAITING_CONFIRMATION stage — an unrelated
    reply must not be forced through AFFIRM/DENY/CORRECTION/ATTACH
    classification (and, critically, must never be misread as an AFFIRM)."""
    item_id, user_id = str(uuid4()), str(uuid4())
    conn = _mock_connection(
        item_row=(
            "obligation", "Pay rent", "Pay rent by Friday.", 15, False, "AWAITING_CONFIRMATION"
        ),
        conversation_row=({"due_at": "2026-09-04T14:00:00"},),
    )
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main.publish") as mock_publish,
        patch("resolver_svc.main._send_sms") as mock_sms,
        patch("resolver_svc.main.converse") as mock_converse,
    ):
        mock_converse.return_value = ConversationTurnResult(relates_to_item=False, reply_text="")
        resp = client.post(
            "/reply",
            json={
                "user_id": user_id,
                "item_id": item_id,
                "text": "lol did you see the game last night",
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "routed_as_new_item"
    mock_sms.assert_not_called()

    raw_publishes = [c for c in mock_publish.call_args_list if c.args[0] == "items-raw"]
    assert len(raw_publishes) == 1
    confirmed_publishes = [c for c in mock_publish.call_args_list if c.args[0] == "items-confirmed"]
    assert confirmed_publishes == []  # never misread as AFFIRM

    update_calls = [c for c in conn.execute.call_args_list if "UPDATE items" in c.args[0]]
    assert update_calls == []  # still AWAITING_CONFIRMATION, untouched


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


# --- /pubsub/push, idempotency guard (step 13) ----------------------------


def test_redelivered_already_processed_item_is_a_noop(client):
    """The real bug found in step 11: a concurrent Pub/Sub redelivery of
    the same items.extracted message, arriving after the first delivery
    already finished (a real conversations row exists), must not be
    reprocessed."""
    extracted = _extracted_message()
    conn = _mock_connection(conversation_row=(1,))
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main._check_duplicate") as mock_check_duplicate,
    ):
        resp = client.post("/pubsub/push", json=_push_envelope(extracted))

    assert resp.status_code == 200
    assert resp.json() == {"status": "already_processed", "item_id": str(extracted.item_id)}
    mock_check_duplicate.assert_not_called()


def test_stuck_state_with_no_conversation_is_not_swallowed(client):
    """A second real bug, found verifying this step: items.state can
    legitimately move past RECEIVED (_write_item's own transaction
    commits independently) while a later write in the same request
    still fails — e.g. the conversations INSERT itself. A guard keyed on
    items.state alone would treat that stuck item as "already done"
    forever, so it would never reach 5 delivery attempts and never
    reach dead_letters — silently defeating this whole step. The guard
    must key on the conversations row actually existing, not state."""
    extracted = _extracted_message()
    conn = _mock_connection(conversation_row=None)  # no conversation yet, despite any item state
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        _no_duplicate(),
        patch("resolver_svc.main._send_sms") as mock_sms,
        patch("resolver_svc.main.converse") as mock_converse,
    ):
        mock_converse.return_value = ConversationTurnResult(
            still_missing=[], reply_text="bet, pay rent friday 2pm — sound good?"
        )
        resp = client.post("/pubsub/push", json=_push_envelope(extracted))

    assert resp.status_code == 200
    assert resp.json()["status"] != "already_processed"
    mock_sms.assert_called_once()


# --- /reply, AWAITING_CONFIRMATION path ----------------------------------


def test_y_reply_publishes_confirmed_and_marks_confirmed(client):
    item_id, user_id = str(uuid4()), str(uuid4())
    conn = _mock_connection(
        item_row=(
            "obligation", "Pay rent", "Pay rent by Friday.", 15, False, "AWAITING_CONFIRMATION"
        ),
        conversation_row=({"due_at": "2026-09-04T14:00:00", "action_type": "calendar"},),
    )
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main.publish") as mock_publish,
        patch("resolver_svc.main.converse") as mock_converse,
        patch("resolver_svc.main._send_sms"),
    ):
        mock_converse.return_value = ConversationTurnResult(
            intent="AFFIRM", reply_text="bet, locked it in"
        )
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
        item_row=("latent", "Learn pottery", "Someday.", 120, False, "AWAITING_CONFIRMATION")
    )
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main.publish") as mock_publish,
        patch("resolver_svc.main._send_sms") as mock_sms,
        patch("resolver_svc.main.converse") as mock_converse,
    ):
        mock_converse.return_value = ConversationTurnResult(
            intent="DENY", reply_text="no worries, scrapped it"
        )
        resp = client.post("/reply", json={"user_id": user_id, "item_id": item_id, "text": "no"})

    assert resp.status_code == 200
    assert resp.json() == {"status": "cancelled", "item_id": item_id}
    mock_publish.assert_not_called()
    mock_sms.assert_called_once_with(UUID(user_id), "+15551234567", "no worries, scrapped it")

    update_calls = [c for c in conn.execute.call_args_list if "UPDATE items" in c.args[0]]
    assert "CANCELLED" in update_calls[0].args[0]


def test_other_reply_during_confirmation_gets_a_natural_clarifying_reply(client):
    """Phase G step D: an unclear reply during AWAITING_CONFIRMATION no
    longer goes silent (the old classify_reply-based system had no OTHER
    handling at all) — converse() classifies it OTHER and writes a natural
    "what do you mean" reply; still no write, no state change."""
    item_id, user_id = str(uuid4()), str(uuid4())
    conn = _mock_connection(
        item_row=(
            "obligation", "Pay rent", "Pay rent by Friday.", 15, False, "AWAITING_CONFIRMATION"
        )
    )
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main.publish") as mock_publish,
        patch("resolver_svc.main._send_sms") as mock_sms,
        patch("resolver_svc.main.converse") as mock_converse,
    ):
        mock_converse.return_value = ConversationTurnResult(
            intent="OTHER", reply_text="not sure what you mean — yes, no, or what should change?"
        )
        resp = client.post(
            "/reply", json={"user_id": user_id, "item_id": item_id, "text": "move it to Friday"}
        )

    assert resp.status_code == 200
    assert resp.json() == {"status": "unhandled_reply", "item_id": item_id}
    mock_publish.assert_not_called()
    mock_sms.assert_called_once_with(
        UUID(user_id), "+15551234567", "not sure what you mean — yes, no, or what should change?"
    )
    update_calls = [c for c in conn.execute.call_args_list if "UPDATE items" in c.args[0]]
    assert update_calls == []


def test_correction_reply_never_auto_publishes(client):
    """The sharpest new risk Phase G step D introduces (module docstring):
    a CORRECTION must merge the updated field and stay AWAITING_CONFIRMATION
    no matter how complete the result looks — only a later, separate AFFIRM
    turn is ever allowed to publish to items.confirmed (ADR 0003)."""
    item_id, user_id = str(uuid4()), str(uuid4())
    conn = _mock_connection(
        item_row=(
            "obligation", "Pay rent", "Pay rent by Friday.", 15, False, "AWAITING_CONFIRMATION"
        ),
        conversation_row=({"due_at": "2026-09-04T14:00:00", "action_type": "calendar"},),
    )
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main.publish") as mock_publish,
        patch("resolver_svc.main._send_sms") as mock_sms,
        patch("resolver_svc.main.converse") as mock_converse,
    ):
        mock_converse.return_value = ConversationTurnResult(
            intent="CORRECTION",
            due_at_filled=True,
            due_at="2026-09-04T15:00:00",
            reply_text="gotcha, switched it to 3pm — still good?",
        )
        resp = client.post(
            "/reply", json={"user_id": user_id, "item_id": item_id, "text": "make it 3pm instead"}
        )

    assert resp.status_code == 200
    assert resp.json() == {"status": "awaiting_confirmation", "item_id": item_id}
    mock_publish.assert_not_called()
    mock_sms.assert_called_once_with(
        UUID(user_id), "+15551234567", "gotcha, switched it to 3pm — still good?"
    )

    update_calls = [c for c in conn.execute.call_args_list if "UPDATE items" in c.args[0]]
    assert update_calls == []  # items.state untouched — still AWAITING_CONFIRMATION
    convo_updates = [
        c for c in conn.execute.call_args_list if "UPDATE conversations" in c.args[0]
    ]
    assert len(convo_updates) == 1
    resolved_fields = convo_updates[0].args[1][0].obj  # Json wrapper
    assert resolved_fields["due_at"] == "2026-09-04T15:00:00"


def test_reply_unknown_item_returns_404(client):
    item_id, user_id = str(uuid4()), str(uuid4())
    conn = _mock_connection(item_row=None)
    with patch("resolver_svc.main.get_connection", return_value=conn):
        resp = client.post("/reply", json={"user_id": user_id, "item_id": item_id, "text": "y"})
    assert resp.status_code == 404


def test_reply_unexpected_state_does_not_crash(client):
    item_id, user_id = str(uuid4()), str(uuid4())
    conn = _mock_connection(
        item_row=("obligation", "Pay rent", "Pay rent by Friday.", 15, False, "COMMITTED")
    )
    with patch("resolver_svc.main.get_connection", return_value=conn):
        resp = client.post("/reply", json={"user_id": user_id, "item_id": item_id, "text": "y"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "unexpected_state"


# --- /pubsub/push, dedupe path (step 12) ---------------------------------


def test_check_duplicate_excludes_dead_states_from_both_queries():
    """Regression guard for a real bug, found live: a user-deleted
    (CANCELLED) item stayed a permanently eligible dedupe match forever,
    since neither the exact dedupe_hash query nor the vector-similarity
    query filtered on state at all — confirming a "duplicate" against a
    dead item resurrected it via the merge path instead of leaving it
    deleted."""
    from resolver_svc.main import _check_duplicate

    extracted = _extracted_message()
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    conn.execute.return_value.fetchone.return_value = None

    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main.embed", return_value=[0.1, 0.2, 0.3]),
    ):
        _check_duplicate(extracted)

    hash_call, vector_call = conn.execute.call_args_list[0], conn.execute.call_args_list[1]
    hash_sql, hash_params = hash_call.args
    vector_sql, vector_params = vector_call.args
    assert "state != ALL" in hash_sql
    assert "CANCELLED" in hash_params[-1] and "MERGED" in hash_params[-1]
    assert "state != ALL" in vector_sql
    assert "CANCELLED" in vector_params[-2] and "MERGED" in vector_params[-2]


def test_other_items_context_formats_and_excludes_current_item():
    """Cross-item situational awareness (user-directed): converse() should
    see the user's other real committed obligations, formatted as plain
    strings, excluding whichever item is currently being discussed."""
    from resolver_svc.main import _other_items_context

    user_id, item_id = uuid4(), uuid4()
    due = datetime(2026, 8, 26, 23, 0, tzinfo=UTC)  # naive-local due_at stored as UTC-aware here
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = [("Assignment due", due)]

    result = _other_items_context(conn, user_id, item_id, "America/Toronto")

    sql, params = conn.execute.call_args.args
    assert "state = 'COMMITTED'" in sql
    assert "i.id != %s" in sql
    assert params == (str(user_id), str(item_id), 20)
    assert result == ["Assignment due — due Wed 26 Aug, 7:00 PM"]


def test_other_items_context_empty_when_nothing_committed():
    from resolver_svc.main import _other_items_context

    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = []

    result = _other_items_context(conn, uuid4(), uuid4(), "America/Toronto")

    assert result == []


def test_duplicate_found_routes_to_duplicate_suspected_not_clarification(client):
    """A duplicate match short-circuits before the completeness check ever
    runs (state-machine.md §1.1) — even an item with missing_fields set
    goes to DUPLICATE_SUSPECTED, not CLARIFYING. The dedupe question itself
    is now converse()-driven, in voice — no fixed "Reply Y to merge" script
    (agent-contracts.md §3.5's dedupe-question follow-up)."""
    extracted = _extracted_message(missing_fields=["due_at"], due_at=None)
    existing_id = uuid4()
    conn = _mock_connection()
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main._send_sms") as mock_sms,
        patch("resolver_svc.main.converse") as mock_converse,
        patch(
            "resolver_svc.main._check_duplicate",
            return_value=DedupeResult(duplicate_item_id=existing_id, duplicate_title="Pay rent"),
        ),
    ):
        mock_converse.return_value = ConversationTurnResult(
            reply_text="isn't this the same as pay rent you already had on there?"
        )
        resp = client.post("/pubsub/push", json=_push_envelope(extracted))

    assert resp.status_code == 200
    assert resp.json() == {"status": "duplicate_suspected", "item_id": str(extracted.item_id)}
    mock_converse.assert_called_once()
    assert mock_converse.call_args.kwargs["dedupe_candidate_title"] == "Pay rent"
    assert mock_converse.call_args.kwargs["awaiting_dedupe_reply"] is False
    mock_sms.assert_called_once_with(
        extracted.user_id,
        "+15551234567",
        "isn't this the same as pay rent you already had on there?",
    )

    update_calls = [c for c in conn.execute.call_args_list if "UPDATE items" in c.args[0]]
    assert update_calls[0].args[1][-2] == "DUPLICATE_SUSPECTED"


# --- /reply, DUPLICATE_SUSPECTED path (step 12) ---------------------------


def test_duplicate_y_reply_merges_no_publish(client):
    # Regression guard for a real bug, found live: this update used to
    # write state='MERGED' without ever setting parent_item_id, silently
    # dropping the link its own log line already claimed to record.
    item_id, user_id = str(uuid4()), str(uuid4())
    match_item_id = str(uuid4())
    conn = _mock_connection(
        item_row=(
            "obligation", "Pay rent", "Pay rent by Friday.", 15, False, "DUPLICATE_SUSPECTED"
        ),
        conversation_row=(
            None,
            {"_dedupe_match_item_id": match_item_id, "_dedupe_match_title": "Pay rent"},
        ),
    )
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main.publish") as mock_publish,
        patch("resolver_svc.main._send_sms") as mock_sms,
        patch("resolver_svc.main.converse") as mock_converse,
    ):
        mock_converse.return_value = ConversationTurnResult(
            intent="AFFIRM", reply_text="sounds good, merging it with pay rent."
        )
        resp = client.post(
            "/reply", json={"user_id": user_id, "item_id": item_id, "text": "yeah same one"}
        )

    assert resp.status_code == 200
    assert resp.json() == {"status": "merged", "item_id": item_id}
    assert mock_converse.call_args.kwargs["awaiting_dedupe_reply"] is True
    assert mock_converse.call_args.kwargs["dedupe_candidate_title"] == "Pay rent"
    mock_publish.assert_not_called()
    mock_sms.assert_called_once_with(
        UUID(user_id), "+15551234567", "sounds good, merging it with pay rent."
    )
    update_calls = [c for c in conn.execute.call_args_list if "UPDATE items" in c.args[0]]
    sql, params = update_calls[0].args
    assert "MERGED" in sql
    assert "parent_item_id" in sql
    assert params == (match_item_id, item_id)


def test_duplicate_n_reply_no_missing_fields_awaits_confirmation(client):
    item_id, user_id = str(uuid4()), str(uuid4())
    conn = _mock_connection(
        item_row=(
            "obligation", "Pay rent", "Pay rent by Friday.", 15, False, "DUPLICATE_SUSPECTED"
        ),
        conversation_row=(
            [],
            {"due_at": "2026-09-04T14:00:00", "_dedupe_match_item_id": str(uuid4())},
        ),
    )
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main._send_sms") as mock_sms,
        patch("resolver_svc.main.converse") as mock_converse,
    ):
        mock_converse.side_effect = [
            ConversationTurnResult(intent="DENY", reply_text="got it, keeping separate."),
            ConversationTurnResult(
                still_missing=[], reply_text="bet, pay rent friday 2pm — sound good?"
            ),
        ]
        resp = client.post(
            "/reply", json={"user_id": user_id, "item_id": item_id, "text": "nah different"}
        )

    assert resp.status_code == 200
    assert resp.json() == {"status": "awaiting_confirmation", "item_id": item_id}
    mock_sms.assert_called_once()
    assert "pay rent" in mock_sms.call_args.args[2]
    update_calls = [c for c in conn.execute.call_args_list if "UPDATE items" in c.args[0]]
    assert "AWAITING_CONFIRMATION" in update_calls[0].args[0]


def test_duplicate_reply_unrelated_routes_as_new_item(client):
    """Before this fix, any dedupe reply that classify_reply() couldn't
    parse as Y or N was silently dropped — no SMS, no new item, nothing.
    Now converse()'s relates_to_item still applies here too (same escape
    hatch as CLARIFYING/AWAITING_CONFIRMATION): genuinely unrelated text
    leaves the pending dedupe question alone and becomes its own new item."""
    item_id, user_id = str(uuid4()), str(uuid4())
    conn = _mock_connection(
        item_row=(
            "obligation", "Pay rent", "Pay rent by Friday.", 15, False, "DUPLICATE_SUSPECTED"
        ),
        conversation_row=(
            [],
            {"_dedupe_match_item_id": str(uuid4()), "_dedupe_match_title": "Pay rent"},
        ),
    )
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main.publish") as mock_publish,
        patch("resolver_svc.main._send_sms") as mock_sms,
        patch("resolver_svc.main.converse") as mock_converse,
    ):
        mock_converse.return_value = ConversationTurnResult(relates_to_item=False, reply_text="")
        resp = client.post(
            "/reply",
            json={"user_id": user_id, "item_id": item_id, "text": "actually book a haircut friday"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "routed_as_new_item"
    mock_sms.assert_not_called()
    mock_publish.assert_called_once()
    topic, message = mock_publish.call_args.args
    assert topic == "items-raw"
    assert message.text == "actually book a haircut friday"

    update_calls = [c for c in conn.execute.call_args_list if "UPDATE items" in c.args[0]]
    assert update_calls == []  # still DUPLICATE_SUSPECTED, untouched


def test_duplicate_reply_ambiguous_gets_natural_clarifying_reply(client):
    """A dedupe reply that's ambiguous about whether it's the same item —
    not unrelated, just unclear — gets a real natural clarifying reply
    (converse()'s OTHER intent) instead of the old silent drop or a wrong
    guess at Y/N."""
    item_id, user_id = str(uuid4()), str(uuid4())
    conn = _mock_connection(
        item_row=(
            "obligation", "Pay rent", "Pay rent by Friday.", 15, False, "DUPLICATE_SUSPECTED"
        ),
        conversation_row=(
            [],
            {"_dedupe_match_item_id": str(uuid4()), "_dedupe_match_title": "Pay rent"},
        ),
    )
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main.publish") as mock_publish,
        patch("resolver_svc.main._send_sms") as mock_sms,
        patch("resolver_svc.main.converse") as mock_converse,
    ):
        mock_converse.return_value = ConversationTurnResult(
            intent="OTHER", reply_text="wait, is this the same pay rent thing or a different one?"
        )
        resp = client.post(
            "/reply", json={"user_id": user_id, "item_id": item_id, "text": "wait what do you mean"}
        )

    assert resp.status_code == 200
    assert resp.json() == {"status": "unhandled_reply", "item_id": item_id}
    mock_publish.assert_not_called()
    mock_sms.assert_called_once_with(
        UUID(user_id), "+15551234567", "wait, is this the same pay rent thing or a different one?"
    )
    update_calls = [c for c in conn.execute.call_args_list if "UPDATE items" in c.args[0]]
    assert update_calls == []  # still DUPLICATE_SUSPECTED, untouched


def test_duplicate_n_reply_with_missing_fields_resumes_clarification(client):
    item_id, user_id = str(uuid4()), str(uuid4())
    conn = _mock_connection(
        item_row=(
            "obligation", "Pay rent", "Pay rent by Friday.", 15, False, "DUPLICATE_SUSPECTED"
        ),
        conversation_row=(["due_at"], {"_dedupe_match_item_id": str(uuid4())}),
    )
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main.converse") as mock_converse,
        patch("resolver_svc.main._send_sms") as mock_sms,
    ):
        mock_converse.side_effect = [
            ConversationTurnResult(intent="DENY", reply_text="got it, keeping separate."),
            ConversationTurnResult(
                due_at_filled=False, due_at=None, still_missing=["due_at"],
                reply_text="When's it due?",
            ),
        ]
        resp = client.post(
            "/reply", json={"user_id": user_id, "item_id": item_id, "text": "nah different"}
        )

    assert resp.status_code == 200
    assert resp.json() == {"status": "clarifying", "item_id": item_id}
    mock_sms.assert_called_once_with(UUID(user_id), "+15551234567", "When's it due?")
    update_calls = [c for c in conn.execute.call_args_list if "UPDATE items" in c.args[0]]
    assert "CLARIFYING" in update_calls[0].args[0]


# --- /reply, thread-attach (step 12) --------------------------------------


def test_attach_reply_sets_parent_item_id(client):
    item_id, user_id = str(uuid4()), str(uuid4())
    target_id = str(uuid4())
    conn = _mock_connection(
        item_row=("latent", "Learn pottery", "Someday.", 120, False, "AWAITING_CONFIRMATION"),
        conversation_row=(
            {"_thread_attach_item_id": target_id, "_thread_attach_title": "Take a ceramics class"},
        ),
    )
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main._send_sms") as mock_sms,
        patch("resolver_svc.main.converse") as mock_converse,
    ):
        mock_converse.return_value = ConversationTurnResult(
            intent="ATTACH", reply_text='cool, linked it to "Take a ceramics class"'
        )
        resp = client.post("/reply", json={"user_id": user_id, "item_id": item_id, "text": "a"})

    assert resp.status_code == 200
    assert resp.json() == {"status": "attached", "item_id": item_id}
    mock_sms.assert_called_once_with(
        UUID(user_id), "+15551234567", 'cool, linked it to "Take a ceramics class"'
    )
    update_calls = [c for c in conn.execute.call_args_list if "UPDATE items" in c.args[0]]
    assert update_calls[0].args[0].strip().startswith("UPDATE items SET parent_item_id")
    assert update_calls[0].args[1][0] == target_id


def test_attach_reply_with_no_candidate_is_unhandled(client):
    """No _thread_attach_item_id on record — falls through to the generic
    reply path (same as OTHER), which now sends a natural reply rather
    than staying silent."""
    item_id, user_id = str(uuid4()), str(uuid4())
    conn = _mock_connection(
        item_row=("latent", "Learn pottery", "Someday.", 120, False, "AWAITING_CONFIRMATION"),
        conversation_row=({},),
    )
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main._send_sms") as mock_sms,
        patch("resolver_svc.main.converse") as mock_converse,
    ):
        mock_converse.return_value = ConversationTurnResult(
            intent="ATTACH", reply_text="hmm, nothing to attach that to — yes or no on the task?"
        )
        resp = client.post("/reply", json={"user_id": user_id, "item_id": item_id, "text": "a"})

    assert resp.status_code == 200
    assert resp.json() == {"status": "unhandled_reply", "item_id": item_id}
    mock_sms.assert_called_once_with(
        UUID(user_id), "+15551234567", "hmm, nothing to attach that to — yes or no on the task?"
    )


# --- effort-aware clarification + two-stage reminders --------------------


def test_compute_reminder_times_derives_early_and_final_from_effort():
    from resolver_svc.main import _compute_reminder_times

    times = _compute_reminder_times("2026-09-04T18:00:00", 120)
    assert times == (datetime(2026, 9, 4, 14, 0), datetime(2026, 9, 4, 16, 0))


def test_compute_reminder_times_none_when_either_input_missing():
    from resolver_svc.main import _compute_reminder_times

    assert _compute_reminder_times(None, 60) is None
    assert _compute_reminder_times("2026-09-04T18:00:00", None) is None


def test_compute_reminder_times_event_reminds_at_start_not_before():
    """Real bug, found live: a meeting used the task-shaped formula and
    never got reminded at its own start time. An event gets a fixed
    30-minute heads-up before start AND a reminder AT the actual start —
    the second value equals due_at itself, on purpose."""
    from resolver_svc.main import _compute_reminder_times

    times = _compute_reminder_times("2026-08-25T20:39:00", 30, is_scheduled_event=True)
    assert times == (datetime(2026, 8, 25, 20, 9), datetime(2026, 8, 25, 20, 39))


def test_compute_reminder_times_event_lead_is_flat_30min_not_effort_scaled():
    """User-directed correction: the event heads-up used to scale with
    effort_minutes (the event's own duration) — wrong, since a longer
    meeting doesn't deserve proportionally more notice, and it made the
    lead time unpredictable. Always exactly 30 minutes before start,
    regardless of effort."""
    from resolver_svc.main import _compute_reminder_times

    times_60 = _compute_reminder_times("2026-08-25T20:39:00", 60, is_scheduled_event=True)
    times_240 = _compute_reminder_times("2026-08-25T20:39:00", 240, is_scheduled_event=True)
    assert times_60[0] == datetime(2026, 8, 25, 20, 9)
    assert times_240[0] == datetime(2026, 8, 25, 20, 9)
    assert times_60[1] == times_240[1] == datetime(2026, 8, 25, 20, 39)


def test_compute_reminder_times_task_never_reminds_at_due_at():
    """Regression guard for the earlier, opposite complaint: a task must
    never get reminded at its own deadline — both reminders strictly
    precede due_at."""
    from resolver_svc.main import _compute_reminder_times

    times = _compute_reminder_times("2026-08-25T20:39:00", 30, is_scheduled_event=False)
    assert times[0] < datetime(2026, 8, 25, 20, 39)
    assert times[1] < datetime(2026, 8, 25, 20, 39)


def test_ensure_reminder_mention_noop_when_already_passed():
    from resolver_svc.main import _ensure_reminder_mention

    # reminder_1_at_passed non-None means converse() already had the
    # chance to mention it naturally — no deterministic append here.
    text = _ensure_reminder_mention("bet, locked in", "2:00 PM", "2026-09-04T18:00:00", 120)
    assert text == "bet, locked in"


def test_ensure_reminder_mention_appends_when_newly_computable():
    from resolver_svc.main import _ensure_reminder_mention

    text = _ensure_reminder_mention(
        "alright, sounds good — confirm?", None, "2026-09-04T18:00:00", 120
    )
    assert text == (
        "alright, sounds good — confirm? I'll remind you at 2:00 PM and 4:00 PM."
    )


def test_ensure_reminder_mention_noop_when_still_not_computable():
    from resolver_svc.main import _ensure_reminder_mention

    text = _ensure_reminder_mention("what's the deadline?", None, None, None)
    assert text == "what's the deadline?"


def test_effort_minutes_missing_starts_clarification(client):
    """effort_minutes now joins due_at/email_recipient in the "ask, don't
    guess" family — an extraction that flagged it missing gets a real
    clarifying question, same as any other missing field."""
    extracted = _extracted_message(missing_fields=["effort_minutes"], effort_minutes=None)
    conn = _mock_connection()
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main.converse") as mock_converse,
        patch("resolver_svc.main._send_sms") as mock_sms,
        _no_duplicate(),
    ):
        mock_converse.return_value = ConversationTurnResult(
            effort_minutes_filled=False,
            still_missing=["effort_minutes"],
            reply_text="how long do you think it'll take?",
        )
        resp = client.post("/pubsub/push", json=_push_envelope(extracted))

    assert resp.status_code == 200
    assert resp.json() == {"status": "clarifying", "item_id": str(extracted.item_id)}
    mock_sms.assert_called_once_with(
        extracted.user_id, "+15551234567", "how long do you think it'll take?"
    )


def test_effort_minutes_reply_persists_and_states_reminder_times(client):
    """The user's own example, end to end at the mechanism level: due_at
    was already known, effort_minutes was the only thing missing. The
    reply that fills it ("2 hours") also completes the item on the same
    turn — reminder_1_at/reminder_2_at can't have been passed into that
    specific converse() call (nothing could compute them before it), so
    the deterministic append in _ensure_reminder_mention is what states
    them, not the LLM."""
    item_id, user_id = str(uuid4()), str(uuid4())
    conn = _mock_connection(
        item_row=("obligation", "Pay rent", "Pay rent by Friday.", None, False, "CLARIFYING"),
        conversation_row=(
            ["effort_minutes"],
            {"due_at": "2026-09-04T18:00:00", "action_type": "calendar"},
            0,
        ),
    )
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main._send_sms") as mock_sms,
        patch("resolver_svc.main.converse") as mock_converse,
    ):
        mock_converse.return_value = ConversationTurnResult(
            effort_minutes_filled=True,
            effort_minutes=120,
            still_missing=[],
            reply_text="alright, sounds good — confirm?",
        )
        resp = client.post(
            "/reply", json={"user_id": user_id, "item_id": item_id, "text": "2 hours"}
        )

    assert resp.status_code == 200
    assert resp.json() == {"status": "awaiting_confirmation", "item_id": item_id}

    effort_updates = [
        c
        for c in conn.execute.call_args_list
        if "UPDATE items" in c.args[0] and "effort_minutes" in c.args[0]
    ]
    assert len(effort_updates) == 1
    assert effort_updates[0].args[1] == (120, item_id)

    mock_sms.assert_called_once_with(
        UUID(user_id),
        "+15551234567",
        "alright, sounds good — confirm? I'll remind you at 2:00 PM and 4:00 PM.",
    )


def test_affirm_publishes_reminder_times_on_confirmed_item(client):
    item_id, user_id = str(uuid4()), str(uuid4())
    conn = _mock_connection(
        item_row=(
            "obligation", "Pay rent", "Pay rent by Friday.", 120, False, "AWAITING_CONFIRMATION"
        ),
        conversation_row=(
            {"due_at": "2026-09-04T18:00:00", "action_type": "calendar"},
        ),
    )
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main.publish") as mock_publish,
        patch("resolver_svc.main.converse") as mock_converse,
        patch("resolver_svc.main._send_sms"),
    ):
        mock_converse.return_value = ConversationTurnResult(
            intent="AFFIRM", reply_text="bet, locked it in — I'll remind you at 2 and 4pm"
        )
        resp = client.post("/reply", json={"user_id": user_id, "item_id": item_id, "text": "y"})

    assert resp.status_code == 200
    mock_publish.assert_called_once()
    _topic, confirmed = mock_publish.call_args[0]
    assert confirmed.reminder_1_at == datetime(2026, 9, 4, 14, 0)
    assert confirmed.reminder_2_at == datetime(2026, 9, 4, 16, 0)


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
