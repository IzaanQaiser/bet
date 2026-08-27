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
        confidence=0.95,
        missing_fields=[],
        reasoning="Clear obligation.",
    )
    defaults.update(overrides)
    return ExtractedItemMessage(**defaults)


# --- /pubsub/push, complete-extraction path -----------------------------


def test_complete_item_auto_confirms_and_publishes(client):
    """V1 polish, user-directed (main.py module docstring): the explicit
    confirmation step is gone — the moment converse() resolves everything
    on the first pass (still_missing empty), the item publishes straight
    to items.confirmed and moves to CONFIRMED, no affirmative required."""
    extracted = _extracted_message()
    conn = _mock_connection()
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main.publish") as mock_publish,
        patch("resolver_svc.main._send_sms") as mock_sms,
        patch("resolver_svc.main.converse") as mock_converse,
        _no_duplicate(),
    ):
        mock_converse.return_value = ConversationTurnResult(
            still_missing=[], reply_text="bet, pay rent locked in for friday 2pm."
        )
        resp = client.post("/pubsub/push", json=_push_envelope(extracted))

    assert resp.status_code == 200
    assert resp.json() == {"status": "confirmed", "item_id": str(extracted.item_id)}

    mock_publish.assert_called_once()
    topic, confirmed = mock_publish.call_args.args
    assert topic == "items-confirmed"
    assert str(confirmed.item_id) == str(extracted.item_id)

    # _write_item's own transient CLARIFYING write happens first (same
    # code path missing-fields items use); the final write flips it
    # straight to CONFIRMED once converse() resolves everything on the
    # first pass (a literal in the SQL text itself, not parameterized) —
    # check the last state write, not the first.
    update_calls = [c for c in conn.execute.call_args_list if "UPDATE items" in c.args[0]]
    assert "CONFIRMED" in update_calls[-1].args[0]

    insert_calls = [
        c for c in conn.execute.call_args_list if "INSERT INTO conversations" in c.args[0]
    ]
    assert len(insert_calls) == 1

    mock_sms.assert_called_once()
    assert "pay rent" in mock_sms.call_args.args[2]


def test_complete_email_item_auto_confirms(client):
    """Step 15 — a fully-resolved email action (recipient already present
    at extraction) stages action_type/email_recipient/email_draft in
    resolved_fields exactly as before; the outbound text is converse()'s
    reply_text, mocked here rather than asserted on for exact wording. V1
    polish: auto-confirms immediately, same as any other complete item."""
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
        patch("resolver_svc.main.publish"),
        patch("resolver_svc.main._send_sms") as mock_sms,
        patch("resolver_svc.main.converse") as mock_converse,
        _no_duplicate(),
    ):
        mock_converse.return_value = ConversationTurnResult(
            still_missing=[], reply_text="bet, sending sarah the delay email now."
        )
        resp = client.post("/pubsub/push", json=_push_envelope(extracted))

    assert resp.status_code == 200
    assert resp.json()["status"] == "confirmed"
    mock_sms.assert_called_once()

    insert_calls = [
        c for c in conn.execute.call_args_list if "INSERT INTO conversations" in c.args[0]
    ]
    # (user_id, item_id, exchange_count, pending_fields, resolved_fields) —
    # resolved_fields (the Json wrapper) is the last param.
    resolved_fields = insert_calls[0].args[1][-1].obj
    assert resolved_fields["action_type"] == "email"
    assert resolved_fields["email_recipient"] == "sarah@example.com"


def test_low_confidence_complete_item_still_auto_confirms(client):
    """state-machine.md §1.2's "Resolved gap": low confidence alone (no
    missing fields) isn't a field-completeness problem — never a
    manufactured clarifying question about nothing. V1 polish: it
    auto-confirms exactly like any other complete item; there's no more
    confirmation-card safety net to fall back on for it."""
    extracted = _extracted_message(confidence=0.4)
    conn = _mock_connection()
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main.publish"),
        patch("resolver_svc.main._send_sms") as mock_sms,
        patch("resolver_svc.main.converse") as mock_converse,
        _no_duplicate(),
    ):
        mock_converse.return_value = ConversationTurnResult(
            still_missing=[], reply_text="bet, pay rent locked in for friday 2pm."
        )
        resp = client.post("/pubsub/push", json=_push_envelope(extracted))
    assert resp.json()["status"] == "confirmed"
    mock_sms.assert_called_once()


# --- /pubsub/push, chat path (is_actionable=False) ------------------------


def test_chat_message_uses_converse_reply_with_history_context(client):
    """Real bug, found live: a plain "betski" sent right after a task had
    just auto-committed got a context-blind "hey! what's up?" back —
    extractor-svc's own chat_reply had zero conversation history to work
    with (ADR 0003's untrusted-input boundary). The reply now comes from
    converse()'s is_chat mode, which has _recent_history like every other
    reply in this service."""
    extracted = _extracted_message(
        is_actionable=False,
        type=None,
        title=None,
        summary=None,
        due_at=None,
        effort_minutes=None,
        confidence=None,
        missing_fields=[],
        raw_text="Betski",
    )
    conn = _mock_connection()
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main._send_sms") as mock_sms,
        patch("resolver_svc.main.converse") as mock_converse,
    ):
        mock_converse.return_value = ConversationTurnResult(reply_text="bet 👍")
        resp = client.post("/pubsub/push", json=_push_envelope(extracted))

    assert resp.status_code == 200
    assert resp.json() == {"status": "chatted", "item_id": str(extracted.item_id)}

    mock_converse.assert_called_once()
    call_kwargs = mock_converse.call_args.kwargs
    assert call_kwargs["is_chat"] is True
    assert call_kwargs["latest_reply"] == "Betski"

    mock_sms.assert_called_once_with(extracted.user_id, "+15551234567", "bet 👍")
    update_calls = [c for c in conn.execute.call_args_list if "UPDATE items" in c.args[0]]
    assert "CHATTED" in update_calls[0].args[0]


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
        patch("resolver_svc.main.publish"),
        patch("resolver_svc.main._send_sms") as mock_sms,
        patch("resolver_svc.main.converse") as mock_converse,
    ):
        mock_converse.return_value = ConversationTurnResult(
            still_missing=[], reply_text="bet, pay rent locked in for friday 2pm."
        )
        resp = client.post("/pubsub/push", json=_push_envelope(extracted))

    assert resp.status_code == 200
    assert resp.json()["status"] != "already_processed"
    mock_sms.assert_called_once()


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
    assert result == ["Assignment due, due Wed 26 Aug, 7:00 PM"]


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


def test_duplicate_n_reply_no_missing_fields_auto_confirms(client):
    """DENY proceeds to the completeness check as if no match existed;
    v1 polish, same as the fresh-item path — nothing missing means it
    auto-commits right there, no separate confirmation round trip."""
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
        patch("resolver_svc.main.publish") as mock_publish,
        patch("resolver_svc.main._send_sms") as mock_sms,
        patch("resolver_svc.main.converse") as mock_converse,
    ):
        mock_converse.side_effect = [
            ConversationTurnResult(intent="DENY", reply_text="got it, keeping separate."),
            ConversationTurnResult(
                still_missing=[], reply_text="bet, pay rent locked in for friday 2pm."
            ),
        ]
        resp = client.post(
            "/reply", json={"user_id": user_id, "item_id": item_id, "text": "nah different"}
        )

    assert resp.status_code == 200
    assert resp.json() == {"status": "confirmed", "item_id": item_id}
    mock_publish.assert_called_once()
    assert mock_publish.call_args.args[0] == "items-confirmed"
    mock_sms.assert_called_once()
    assert "pay rent" in mock_sms.call_args.args[2]
    update_calls = [c for c in conn.execute.call_args_list if "UPDATE items" in c.args[0]]
    assert "CONFIRMED" in update_calls[0].args[0]


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



# --- single time-of reminder (v1 simplification) -------------------------


def test_compute_reminder_time_equals_due_at():
    """v1 simplification, user-directed: the one remaining SMS reminder
    fires AT due_at itself — no offset, no effort/task-event distinction
    in the math at all."""
    from resolver_svc.main import _compute_reminder_time

    assert _compute_reminder_time("2026-09-04T18:00:00") == datetime(2026, 9, 4, 18, 0)


def test_compute_reminder_time_none_when_due_at_missing():
    from resolver_svc.main import _compute_reminder_time

    assert _compute_reminder_time(None) is None


def test_confirm_and_publish_computes_reminder_time_and_flips_state():
    """_confirm_and_publish is the only path into CONFIRMED now (v1 polish,
    module docstring) — this is the reminder-time computation the old
    AFFIRM-reply path used to do, unit-tested directly against the helper
    rather than through a full /reply round trip."""
    from resolver_svc.main import _confirm_and_publish

    item_id, user_id = uuid4(), uuid4()
    conn = MagicMock()
    resolved_fields = {"due_at": "2026-09-04T18:00:00", "action_type": "calendar"}

    with patch("resolver_svc.main.publish") as mock_publish:
        _confirm_and_publish(
            conn, item_id, user_id, "obligation", "Pay rent", "Pay rent by Friday.",
            15, resolved_fields,
        )

    mock_publish.assert_called_once()
    topic, confirmed = mock_publish.call_args.args
    assert topic == "items-confirmed"
    assert confirmed.reminder_at == datetime(2026, 9, 4, 18, 0)

    update_calls = [c for c in conn.execute.call_args_list if "UPDATE items" in c.args[0]]
    assert len(update_calls) == 1
    assert "CONFIRMED" in update_calls[0].args[0]
    assert update_calls[0].args[1] == (str(item_id),)


# --- event title/duration clarification (event-only) ----------------------
# title and effort_minutes rejoin due_at/email_recipient's "ask, don't
# guess" family, but only ever for a scheduled event with no identifying
# detail or stated length — a task's title/effort_minutes are always
# already filled by extractor-svc and never revisited here.


def test_merge_effort_minutes_uses_filled_value():
    from resolver_svc.main import _merge_effort_minutes

    result = ConversationTurnResult(effort_minutes_filled=True, effort_minutes=60, reply_text="x")
    assert _merge_effort_minutes(None, result) == 60


def test_merge_effort_minutes_noop_when_not_filled():
    from resolver_svc.main import _merge_effort_minutes

    result = ConversationTurnResult(effort_minutes_filled=False, reply_text="x")
    assert _merge_effort_minutes(30, result) == 30


def test_persist_effort_minutes_fill_writes_items_row():
    from resolver_svc.main import _persist_effort_minutes_fill

    conn = MagicMock()
    result = ConversationTurnResult(effort_minutes_filled=True, effort_minutes=60, reply_text="x")
    _persist_effort_minutes_fill(conn, "item-1", result)
    conn.execute.assert_called_once()
    sql, params = conn.execute.call_args[0]
    assert "UPDATE items SET effort_minutes" in sql
    assert params == (60, "item-1")


def test_persist_effort_minutes_fill_noop_when_not_filled():
    from resolver_svc.main import _persist_effort_minutes_fill

    conn = MagicMock()
    result = ConversationTurnResult(effort_minutes_filled=False, reply_text="x")
    _persist_effort_minutes_fill(conn, "item-1", result)
    conn.execute.assert_not_called()


def test_merge_title_uses_filled_value():
    from resolver_svc.main import _merge_title

    result = ConversationTurnResult(title_filled=True, title="Meeting with Sarah", reply_text="x")
    assert _merge_title(None, result) == "Meeting with Sarah"


def test_merge_title_noop_when_not_filled():
    from resolver_svc.main import _merge_title

    result = ConversationTurnResult(title_filled=False, reply_text="x")
    assert _merge_title("Meeting", result) == "Meeting"


def test_persist_title_fill_writes_items_row():
    from resolver_svc.main import _persist_title_fill

    conn = MagicMock()
    result = ConversationTurnResult(title_filled=True, title="Call with landlord", reply_text="x")
    _persist_title_fill(conn, "item-1", result)
    conn.execute.assert_called_once()
    sql, params = conn.execute.call_args[0]
    assert "UPDATE items SET title" in sql
    assert params == ("Call with landlord", "item-1")


def test_event_missing_title_and_duration_starts_clarification(client):
    """A bare 'I have a meeting at 5pm' — extractor-svc leaves both title
    and effort_minutes null and flags both missing (event-only rule)."""
    extracted = _extracted_message(
        title=None,
        due_at=datetime(2026, 9, 4, 17, 0),
        effort_minutes=None,
        is_scheduled_event=True,
        missing_fields=["title", "effort_minutes"],
    )
    conn = _mock_connection()
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main.converse") as mock_converse,
        patch("resolver_svc.main._send_sms"),
        _no_duplicate(),
    ):
        mock_converse.return_value = ConversationTurnResult(
            still_missing=["title", "effort_minutes"],
            reply_text="what's it for, and how long do you think it'll run?",
        )
        resp = client.post("/pubsub/push", json=_push_envelope(extracted))

    assert resp.status_code == 200
    assert resp.json() == {"status": "clarifying", "item_id": str(extracted.item_id)}


def test_event_title_and_duration_reply_persists_and_auto_confirms(client):
    """The reply that fills both missing pieces in one shot auto-commits
    the item (v1 polish — nothing left missing) and writes both back to
    the items row — they have no conversations.resolved_fields scratchpad
    slot, unlike due_at/email_recipient, so the items table is the only
    place they can live pre-commit."""
    item_id, user_id = str(uuid4()), str(uuid4())
    conn = _mock_connection(
        item_row=("obligation", None, "A meeting.", None, True, "CLARIFYING"),
        conversation_row=(
            ["title", "effort_minutes"],
            {"due_at": "2026-09-04T17:00:00", "action_type": "calendar"},
            0,
        ),
    )
    with (
        patch("resolver_svc.main.get_connection", return_value=conn),
        patch("resolver_svc.main.publish") as mock_publish,
        patch("resolver_svc.main._send_sms"),
        patch("resolver_svc.main.converse") as mock_converse,
    ):
        mock_converse.return_value = ConversationTurnResult(
            title_filled=True,
            title="Meeting with Sarah",
            effort_minutes_filled=True,
            effort_minutes=60,
            still_missing=[],
            reply_text="got it — meeting with Sarah, locked in for 5pm.",
        )
        resp = client.post(
            "/reply", json={"user_id": user_id, "item_id": item_id, "text": "with Sarah, an hour"}
        )

    assert resp.status_code == 200
    assert resp.json() == {"status": "confirmed", "item_id": item_id}
    mock_publish.assert_called_once()
    assert mock_publish.call_args.args[0] == "items-confirmed"
    update_calls = [c for c in conn.execute.call_args_list if "UPDATE items SET" in c.args[0]]
    title_calls = [c for c in update_calls if "title" in c.args[0]]
    effort_calls = [c for c in update_calls if "effort_minutes" in c.args[0]]
    assert title_calls and title_calls[0].args[1] == ("Meeting with Sarah", item_id)
    assert effort_calls and effort_calls[0].args[1] == (60, item_id)


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
