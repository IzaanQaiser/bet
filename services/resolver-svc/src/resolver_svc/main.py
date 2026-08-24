"""resolver-svc — step 12: dedupe via embeddings, on top of step 10's
real clarification loop and step 9's real confirmation.

state-machine.md §1.1: on entering EXTRACTED, before the completeness
check ever runs, check for a duplicate — a cheap dedupe_hash exact match
first (no embedding call needed), then a text-embedding-004 cosine
search over item_embeddings for this user. similarity >= 0.92 (or an
exact hash match) routes to DUPLICATE_SUSPECTED and asks "is this the
same as X?" — never silently merged (ADR 0003). A 0.82-0.92 match
against an existing *latent* is folded into the eventual confirmation
card as a non-blocking thread-attach offer instead of its own stage.

Every path that can reach AWAITING_CONFIRMATION carries a possible
thread-attach candidate forward through conversations.resolved_fields
(the documented scratchpad, data-model.md §2.4) under `_thread_attach_*`
keys, since the offer is decided once at the initial dedupe check but
may need to be rendered much later — after a full clarification
exchange, or after the user says a dedupe match "N, it's different."
Likewise `_dedupe_match_item_id`/`_dedupe_match_title` carry the
matched item across the DUPLICATE_SUSPECTED Y/N round trip; there is no
dedicated column for either, matching how `due_at` already had nowhere
else to live pre-commit (data-model.md §2.4's original resolved bug).

Still deliberately not built, carried over from step 10: a correction
reply during AWAITING_CONFIRMATION (a reply that isn't Y/N/A) is still
just logged, no action taken — it needs its own field-targeting
heuristic (agent-contracts.md §3.2's "cheap heuristic" paragraph) that
doesn't reuse the due_at-only clarification model cleanly.

Step 13 adds an idempotency guard at the top of /pubsub/push — a real
bug found in step 11's live testing: a concurrent Pub/Sub redelivery of
the same items.extracted message during a slow cold start raced this
handler and 500'd on ADK's InMemorySessionService (its deterministic
session id isn't safe against two in-flight handlers for the same
item). That crash happened to be harmless purely by luck — the winning
request had already sent its SMS first — not because anything actually
guarded against it.

The guard checks for an existing `conversations` row, not `items.state`
— an earlier draft checked state, and a second real bug (found
verifying this step, not hypothetical) showed why that's wrong:
`_write_item()` commits the state transition in its own transaction,
separate from the later `conversations` INSERT. A genuine failure
between those two writes (reproduced with a deliberately-bad user_id)
left the item stuck at a post-RECEIVED state with no conversation ever
created — a state-only guard would then swallow every future
redelivery as "already done" forever, so the message never reaches 5
delivery attempts and never reaches `dead_letters` at all, defeating
the exact mechanism this step exists to build. The `conversations` row
is only ever written as the last DB step of every success path
(data-model.md §2.4: created unconditionally, the instant processing
actually finishes), so its existence is the one true completion signal.
"""

import logging
import os
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from obligation_engine_shared.db import get_connection
from obligation_engine_shared.pubsub import decode_push_envelope, publish
from obligation_engine_shared.reply_classifier import classify_reply
from obligation_engine_shared.schemas import (
    ConfirmedItemMessage,
    ExtractedItemMessage,
    RoutedReplyMessage,
)
from psycopg.types.json import Json
from twilio.rest import Client as TwilioClient

from resolver_svc.clarification import clarify
from resolver_svc.dedupe import (
    DedupeResult,
    classify_match,
    compute_dedupe_hash,
    embed,
    vector_literal,
)
from resolver_svc.templates import (
    render_attached,
    render_cancelled,
    render_confirmation_card,
    render_dedupe_question,
    render_merged,
    render_needs_review,
)

logger = logging.getLogger("resolver_svc")
app = FastAPI()

CONFIDENCE_THRESHOLD = 0.75  # state-machine.md §1.2
MAX_EXCHANGES = 3  # state-machine.md §1.2

# Plain config, not secrets — same treatment as every other Twilio
# identifier in this project (infrastructure.md §4.1). Only the API key
# secret goes through Secret Manager, via env.
TWILIO_ACCOUNT_SID = "AC3292d4a7944b87b2fe3db562856e32bd"
TWILIO_API_KEY_SID = "SK7a7912d15fea946956ab8bbae8214bce"
TWILIO_FROM_NUMBER = "+14152365420"


def _twilio_client() -> TwilioClient:
    return TwilioClient(TWILIO_API_KEY_SID, os.environ["TWILIO_API_KEY_SECRET"], TWILIO_ACCOUNT_SID)


def _send_sms(to: str, body: str) -> None:
    _twilio_client().messages.create(to=to, from_=TWILIO_FROM_NUMBER, body=body)


def _write_item(extracted: ExtractedItemMessage, state: str) -> None:
    dedupe_hash = compute_dedupe_hash(extracted.title, extracted.summary)
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE items
            SET type = %s, title = %s, summary = %s, effort_minutes = %s,
                focus_depth = %s, confidence = %s, dedupe_hash = %s, state = %s,
                updated_at = now()
            WHERE id = %s
            """,
            (
                extracted.type,
                extracted.title,
                extracted.summary,
                extracted.effort_minutes,
                extracted.focus_depth,
                extracted.confidence,
                dedupe_hash,
                state,
                str(extracted.item_id),
            ),
        )
        conn.commit()


def _user_phone_and_timezone(conn, user_id) -> tuple[str, str]:
    row = conn.execute(
        "SELECT phone_e164, timezone FROM users WHERE id = %s", (str(user_id),)
    ).fetchone()
    return row[0], row[1]


def _initial_resolved_fields(extracted: ExtractedItemMessage) -> dict:
    """Builds resolved_fields for a freshly-extracted item — every path
    that creates a NEW conversations row from an ExtractedItemMessage
    uses this (data-model.md §2.4/§2.7's scratchpad). due_at stages here
    per the original design; step 15 adds action_type and, for an email
    action, email_recipient/email_draft — none of these have an items
    column either. action_type (and the email fields) are only staged
    for an obligation — omitted entirely for a latent, so a later
    `.get("action_type")` correctly reads back None, matching
    ConfirmedItemMessage's "null for a latent" convention."""
    fields: dict = {}
    if extracted.due_at:
        fields["due_at"] = extracted.due_at.isoformat()
    if extracted.type == "obligation":
        fields["action_type"] = extracted.action_type
        if extracted.action_type == "email":
            if extracted.email_recipient:
                fields["email_recipient"] = extracted.email_recipient
            if extracted.email_draft:
                fields["email_draft"] = extracted.email_draft
    return fields


def _check_duplicate(extracted: ExtractedItemMessage) -> DedupeResult:
    dedupe_hash = compute_dedupe_hash(extracted.title, extracted.summary)

    with get_connection() as conn:
        exact = conn.execute(
            "SELECT id, title FROM items "
            "WHERE user_id = %s AND dedupe_hash = %s AND id != %s LIMIT 1",
            (str(extracted.user_id), dedupe_hash, str(extracted.item_id)),
        ).fetchone()
        if exact is not None:
            return DedupeResult(duplicate_item_id=exact[0], duplicate_title=exact[1])

        vector = vector_literal(embed(extracted.title, extracted.summary))
        match = conn.execute(
            """
            SELECT i.id, i.title, i.type, 1 - (e.embedding <=> %s::vector) AS similarity
            FROM item_embeddings e JOIN items i ON i.id = e.item_id
            WHERE i.user_id = %s
            ORDER BY e.embedding <=> %s::vector
            LIMIT 1
            """,
            (vector, str(extracted.user_id), vector),
        ).fetchone()
        conn.execute(
            "INSERT INTO item_embeddings (item_id, embedding) VALUES (%s, %s::vector)",
            (str(extracted.item_id), vector),
        )
        conn.commit()

    if match is None:
        return DedupeResult()
    match_id, match_title, match_type, similarity = match
    return classify_match(similarity, match_type, match_id, match_title)


def _finalize_awaiting_confirmation(
    conn, item_id, item_type, title, summary, effort_minutes, resolved_fields, thread_attach_title
) -> str:
    conn.execute(
        "UPDATE items SET state = 'AWAITING_CONFIRMATION', updated_at = now() WHERE id = %s",
        (str(item_id),),
    )
    due_at_iso = resolved_fields.get("due_at")
    return render_confirmation_card(
        item_type,
        title,
        summary,
        datetime.fromisoformat(due_at_iso) if due_at_iso else None,
        effort_minutes,
        resolved_fields.get("action_type"),
        email_recipient=resolved_fields.get("email_recipient"),
        email_draft=resolved_fields.get("email_draft"),
        thread_attach_title=thread_attach_title,
    )


async def _start_duplicate_suspected(extracted: ExtractedItemMessage, dedupe: DedupeResult) -> None:
    _write_item(extracted, "DUPLICATE_SUSPECTED")

    resolved_fields = _initial_resolved_fields(extracted)
    resolved_fields["_dedupe_match_item_id"] = str(dedupe.duplicate_item_id)
    resolved_fields["_dedupe_match_title"] = dedupe.duplicate_title
    if dedupe.thread_attach_item_id:
        resolved_fields["_thread_attach_item_id"] = str(dedupe.thread_attach_item_id)
        resolved_fields["_thread_attach_title"] = dedupe.thread_attach_title

    with get_connection() as conn:
        conn.execute(
            "INSERT INTO conversations (user_id, item_id, pending_fields, resolved_fields) "
            "VALUES (%s, %s, %s, %s)",
            (
                str(extracted.user_id),
                str(extracted.item_id),
                extracted.missing_fields,
                Json(resolved_fields),
            ),
        )
        phone, _tz = _user_phone_and_timezone(conn, extracted.user_id)
        conn.commit()

    _send_sms(phone, render_dedupe_question(dedupe.duplicate_title))
    logger.info(
        "DUPLICATE_SUSPECTED item_id=%s matched_item_id=%s",
        extracted.item_id,
        dedupe.duplicate_item_id,
    )


async def _start_clarification(
    extracted: ExtractedItemMessage, thread_attach: tuple | None = None
) -> None:
    _write_item(extracted, "CLARIFYING")

    with get_connection() as conn:
        phone, tz_name = _user_phone_and_timezone(conn, extracted.user_id)

    now_local = datetime.now(UTC).astimezone(ZoneInfo(tz_name))
    result = await clarify(
        session_id=f"{extracted.item_id}-0",
        now_local=now_local,
        tz_name=tz_name,
        title=extracted.title,
        missing_fields=extracted.missing_fields,
        latest_reply=None,
    )
    resolved_fields = _initial_resolved_fields(extracted)
    if result.due_at_filled and result.due_at:
        resolved_fields["due_at"] = result.due_at
    if result.email_recipient_filled and result.email_recipient:
        resolved_fields["email_recipient"] = result.email_recipient
    if thread_attach:
        resolved_fields["_thread_attach_item_id"] = str(thread_attach[0])
        resolved_fields["_thread_attach_title"] = thread_attach[1]

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO conversations
                (user_id, item_id, exchange_count, pending_fields, resolved_fields)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                str(extracted.user_id),
                str(extracted.item_id),
                1 if result.question else 0,
                result.still_missing,
                Json(resolved_fields),
            ),
        )
        conn.commit()

    if result.question:
        _send_sms(phone, result.question)
        logger.info("CLARIFYING item_id=%s sent question 1/%d", extracted.item_id, MAX_EXCHANGES)
        return

    # Turn 1 always has latest_reply=None, so the model should never
    # resolve still_missing to empty here — handled anyway rather than
    # assumed, since it's one extra branch, not extra complexity.
    with get_connection() as conn:
        body = _finalize_awaiting_confirmation(
            conn,
            extracted.item_id,
            extracted.type,
            extracted.title,
            extracted.summary,
            extracted.effort_minutes,
            resolved_fields,
            thread_attach[1] if thread_attach else None,
        )
        conn.commit()
    _send_sms(phone, body)
    logger.info("AWAITING_CONFIRMATION item_id=%s (resolved on first pass)", extracted.item_id)


@app.post("/pubsub/push")
async def pubsub_push(request: Request):
    envelope = await request.json()
    try:
        extracted = decode_push_envelope(envelope, ExtractedItemMessage)
    except Exception:
        logger.exception("malformed push envelope, could not decode ExtractedItemMessage")
        raise HTTPException(status_code=500, detail="malformed envelope") from None

    with get_connection() as conn:
        convo_exists = conn.execute(
            "SELECT 1 FROM conversations WHERE item_id = %s LIMIT 1", (str(extracted.item_id),)
        ).fetchone()
    if convo_exists is not None:
        # A concurrent redelivery of the same items.extracted message,
        # arriving after the first delivery already finished — the real
        # race found in step 11 (ADK's InMemorySession crashed on the
        # second create_session() call, which happened to 500 before any
        # visible harm, but that was luck, not a guard). Checked against
        # the conversations row rather than items.state != 'RECEIVED':
        # _write_item() commits the state transition in its own earlier
        # transaction, separate from the conversations INSERT — a real
        # failure found empirically verifying this step, where the
        # conversations write itself failed (a bad user_id reference),
        # left the item stuck at a post-RECEIVED state with the
        # conversation never created. A state-only check would have
        # swallowed every future redelivery as "already done" forever,
        # silently defeating the dead-letter path this step exists to
        # build. The conversations row is only ever written as the last
        # DB step of every success path (data-model.md §2.4: created
        # unconditionally, the moment resolver-svc actually finishes),
        # so its existence is the one true completion signal.
        logger.info("skipping already-processed item_id=%s (redelivery)", extracted.item_id)
        return {"status": "already_processed", "item_id": str(extracted.item_id)}

    try:
        dedupe = _check_duplicate(extracted)
    except Exception:
        logger.exception("dedupe check failed item_id=%s", extracted.item_id)
        raise HTTPException(status_code=500, detail="dedupe check failed") from None

    if dedupe.duplicate_item_id is not None:
        try:
            await _start_duplicate_suspected(extracted, dedupe)
        except Exception:
            logger.exception("failed to start duplicate-suspected item_id=%s", extracted.item_id)
            raise HTTPException(status_code=500, detail="duplicate check start failed") from None
        return {"status": "duplicate_suspected", "item_id": str(extracted.item_id)}

    thread_attach = (
        (dedupe.thread_attach_item_id, dedupe.thread_attach_title)
        if dedupe.thread_attach_item_id
        else None
    )

    if extracted.missing_fields:
        try:
            await _start_clarification(extracted, thread_attach)
        except Exception:
            logger.exception("failed to start clarification item_id=%s", extracted.item_id)
            raise HTTPException(status_code=500, detail="clarification start failed") from None
        return {"status": "clarifying", "item_id": str(extracted.item_id)}

    # Complete extraction — low confidence alone (with no missing fields)
    # doesn't get a manufactured clarifying question about nothing; the
    # confirmation card's own "or send a correction" is the safety net
    # for it (state-machine.md §1.2's "Resolved gap" note).
    try:
        _write_item(extracted, "AWAITING_CONFIRMATION")
    except Exception:
        logger.exception("failed to write extracted fields item_id=%s", extracted.item_id)
        raise HTTPException(status_code=500, detail="db write failed") from None

    resolved_fields = _initial_resolved_fields(extracted)
    if thread_attach:
        resolved_fields["_thread_attach_item_id"] = str(thread_attach[0])
        resolved_fields["_thread_attach_title"] = thread_attach[1]
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO conversations (user_id, item_id, resolved_fields) VALUES (%s, %s, %s)",
                (str(extracted.user_id), str(extracted.item_id), Json(resolved_fields)),
            )
            phone, _tz = _user_phone_and_timezone(conn, extracted.user_id)
            conn.commit()
    except Exception:
        logger.exception("failed to open conversation item_id=%s", extracted.item_id)
        raise HTTPException(status_code=500, detail="conversation open failed") from None

    body = render_confirmation_card(
        extracted.type,
        extracted.title,
        extracted.summary,
        extracted.due_at,
        extracted.effort_minutes,
        resolved_fields.get("action_type"),
        email_recipient=resolved_fields.get("email_recipient"),
        email_draft=resolved_fields.get("email_draft"),
        thread_attach_title=thread_attach[1] if thread_attach else None,
    )
    try:
        _send_sms(phone, body)
    except Exception:
        logger.exception("failed to send confirmation card item_id=%s", extracted.item_id)
        raise HTTPException(status_code=500, detail="sms send failed") from None

    logger.info("AWAITING_CONFIRMATION item_id=%s sent confirmation card", extracted.item_id)
    return {"status": "awaiting_confirmation", "item_id": str(extracted.item_id)}


async def _handle_clarification_reply(
    conn, item_id, phone, tz_name, title, item_type, summary, effort_minutes, latest_reply
) -> dict:
    convo_row = conn.execute(
        "SELECT pending_fields, resolved_fields, exchange_count FROM conversations "
        "WHERE item_id = %s ORDER BY last_message_at DESC LIMIT 1",
        (str(item_id),),
    ).fetchone()
    pending_fields, resolved_fields, exchange_count = convo_row
    thread_attach_title = resolved_fields.get("_thread_attach_title")

    now_local = datetime.now(UTC).astimezone(ZoneInfo(tz_name))
    result = await clarify(
        session_id=f"{item_id}-{exchange_count}",
        now_local=now_local,
        tz_name=tz_name,
        title=title,
        missing_fields=pending_fields,
        latest_reply=latest_reply,
    )
    if result.due_at_filled and result.due_at:
        resolved_fields = {**resolved_fields, "due_at": result.due_at}
    if result.email_recipient_filled and result.email_recipient:
        resolved_fields = {**resolved_fields, "email_recipient": result.email_recipient}

    if result.still_missing:
        if exchange_count < MAX_EXCHANGES:
            next_count = exchange_count + 1
            conn.execute(
                """
                UPDATE conversations
                SET exchange_count = %s, pending_fields = %s, resolved_fields = %s,
                    last_message_at = now()
                WHERE item_id = %s
                """,
                (next_count, result.still_missing, Json(resolved_fields), str(item_id)),
            )
            conn.commit()
            _send_sms(phone, result.question)
            logger.info(
                "CLARIFYING item_id=%s sent question %d/%d", item_id, next_count, MAX_EXCHANGES
            )
            return {"status": "clarifying", "item_id": str(item_id)}

        conn.execute(
            "UPDATE items SET state = 'NEEDS_REVIEW', updated_at = now() WHERE id = %s",
            (str(item_id),),
        )
        conn.execute(
            "UPDATE conversations SET resolved_fields = %s, last_message_at = now() "
            "WHERE item_id = %s",
            (Json(resolved_fields), str(item_id)),
        )
        conn.commit()
        _send_sms(phone, render_needs_review(title))
        logger.info("NEEDS_REVIEW item_id=%s (exhausted %d exchanges)", item_id, MAX_EXCHANGES)
        return {"status": "needs_review", "item_id": str(item_id)}

    body = _finalize_awaiting_confirmation(
        conn, item_id, item_type, title, summary, effort_minutes,
        resolved_fields, thread_attach_title,
    )
    conn.execute(
        "UPDATE conversations SET resolved_fields = %s, pending_fields = %s, "
        "last_message_at = now() WHERE item_id = %s",
        (Json(resolved_fields), [], str(item_id)),
    )
    conn.commit()
    _send_sms(phone, body)
    logger.info("AWAITING_CONFIRMATION item_id=%s (clarification resolved)", item_id)
    return {"status": "awaiting_confirmation", "item_id": str(item_id)}


async def _handle_duplicate_reply(
    conn, item_id, phone, tz_name, title, item_type, summary, effort_minutes, text
) -> dict:
    convo_row = conn.execute(
        "SELECT pending_fields, resolved_fields FROM conversations WHERE item_id = %s "
        "ORDER BY last_message_at DESC LIMIT 1",
        (str(item_id),),
    ).fetchone()
    pending_fields, resolved_fields = convo_row
    classification = classify_reply(text)

    if classification == "Y":
        match_title = resolved_fields.get("_dedupe_match_title") or "that item"
        conn.execute(
            "UPDATE items SET state = 'MERGED', updated_at = now() WHERE id = %s",
            (str(item_id),),
        )
        conn.commit()
        _send_sms(phone, render_merged(match_title))
        logger.info(
            "MERGED item_id=%s into=%s", item_id, resolved_fields.get("_dedupe_match_item_id")
        )
        return {"status": "merged", "item_id": str(item_id)}

    if classification == "N":
        # "N" proceeds to the completeness check as if no match existed
        # (state-machine.md §1.1 point 2) — pending_fields here is the
        # original missing_fields staged by _start_duplicate_suspected.
        thread_attach_title = resolved_fields.get("_thread_attach_title")

        if pending_fields:
            now_local = datetime.now(UTC).astimezone(ZoneInfo(tz_name))
            result = await clarify(
                session_id=f"{item_id}-0", now_local=now_local, tz_name=tz_name,
                title=title, missing_fields=pending_fields, latest_reply=None,
            )
            if result.due_at_filled and result.due_at:
                resolved_fields = {**resolved_fields, "due_at": result.due_at}
            if result.email_recipient_filled and result.email_recipient:
                resolved_fields = {**resolved_fields, "email_recipient": result.email_recipient}

            if result.question:
                conn.execute(
                    "UPDATE items SET state = 'CLARIFYING', updated_at = now() WHERE id = %s",
                    (str(item_id),),
                )
                conn.execute(
                    "UPDATE conversations SET exchange_count = 1, pending_fields = %s, "
                    "resolved_fields = %s, last_message_at = now() WHERE item_id = %s",
                    (result.still_missing, Json(resolved_fields), str(item_id)),
                )
                conn.commit()
                _send_sms(phone, result.question)
                logger.info(
                    "CLARIFYING item_id=%s sent question 1/%d (post-dedupe)", item_id, MAX_EXCHANGES
                )
                return {"status": "clarifying", "item_id": str(item_id)}

            body = _finalize_awaiting_confirmation(
                conn, item_id, item_type, title, summary, effort_minutes,
                resolved_fields, thread_attach_title,
            )
            conn.execute(
                "UPDATE conversations SET pending_fields = %s, resolved_fields = %s, "
                "last_message_at = now() WHERE item_id = %s",
                ([], Json(resolved_fields), str(item_id)),
            )
            conn.commit()
            _send_sms(phone, body)
            logger.info("AWAITING_CONFIRMATION item_id=%s (post-dedupe, resolved)", item_id)
            return {"status": "awaiting_confirmation", "item_id": str(item_id)}

        body = _finalize_awaiting_confirmation(
            conn, item_id, item_type, title, summary, effort_minutes,
            resolved_fields, thread_attach_title,
        )
        conn.commit()
        _send_sms(phone, body)
        logger.info("AWAITING_CONFIRMATION item_id=%s (post-dedupe, no missing fields)", item_id)
        return {"status": "awaiting_confirmation", "item_id": str(item_id)}

    logger.info("dedupe reply outside Y/N, not yet handled item_id=%s text=%r", item_id, text)
    return {"status": "unhandled_reply", "item_id": str(item_id)}


@app.post("/reply")
async def reply(payload: RoutedReplyMessage):
    try:
        with get_connection() as conn:
            item_row = conn.execute(
                "SELECT type, title, summary, effort_minutes, state FROM items WHERE id = %s",
                (str(payload.item_id),),
            ).fetchone()
            if item_row is None:
                raise HTTPException(status_code=404, detail="unknown item_id")
            item_type, title, summary, effort_minutes, state = item_row
            phone, tz_name = _user_phone_and_timezone(conn, payload.user_id)

            if state == "DUPLICATE_SUSPECTED":
                return await _handle_duplicate_reply(
                    conn, payload.item_id, phone, tz_name, title, item_type, summary,
                    effort_minutes, payload.text,
                )

            if state == "CLARIFYING":
                return await _handle_clarification_reply(
                    conn,
                    payload.item_id,
                    phone,
                    tz_name,
                    title,
                    item_type,
                    summary,
                    effort_minutes,
                    payload.text,
                )

            if state != "AWAITING_CONFIRMATION":
                logger.warning(
                    "reply routed for item_id=%s in unexpected state=%s", payload.item_id, state
                )
                return {"status": "unexpected_state", "item_id": str(payload.item_id)}

            classification = classify_reply(payload.text)

            if classification == "Y":
                convo_row = conn.execute(
                    "SELECT resolved_fields FROM conversations WHERE item_id = %s "
                    "ORDER BY last_message_at DESC LIMIT 1",
                    (str(payload.item_id),),
                ).fetchone()
                resolved_fields = convo_row[0] if convo_row else {}
                confirmed = ConfirmedItemMessage(
                    item_id=payload.item_id,
                    user_id=payload.user_id,
                    type=item_type,
                    title=title,
                    summary=summary,
                    due_at=resolved_fields.get("due_at"),
                    effort_minutes=effort_minutes,
                    action_type=resolved_fields.get("action_type"),
                    email_recipient=resolved_fields.get("email_recipient"),
                    email_draft=resolved_fields.get("email_draft"),
                )
                publish("items-confirmed", confirmed)
                conn.execute(
                    "UPDATE items SET state = 'CONFIRMED', updated_at = now() WHERE id = %s",
                    (str(payload.item_id),),
                )
                conn.commit()
                logger.info("CONFIRMED item_id=%s (real Y reply)", payload.item_id)
                return {"status": "confirmed", "item_id": str(payload.item_id)}

            if classification == "N":
                conn.execute(
                    "UPDATE items SET state = 'CANCELLED', updated_at = now() WHERE id = %s",
                    (str(payload.item_id),),
                )
                conn.commit()
                _send_sms(phone, render_cancelled())
                logger.info("CANCELLED item_id=%s (real N reply)", payload.item_id)
                return {"status": "cancelled", "item_id": str(payload.item_id)}

            if classification == "ATTACH":
                convo_row = conn.execute(
                    "SELECT resolved_fields FROM conversations WHERE item_id = %s "
                    "ORDER BY last_message_at DESC LIMIT 1",
                    (str(payload.item_id),),
                ).fetchone()
                resolved_fields = convo_row[0] if convo_row else {}
                target_id = resolved_fields.get("_thread_attach_item_id")
                target_title = resolved_fields.get("_thread_attach_title")
                if target_id:
                    conn.execute(
                        "UPDATE items SET parent_item_id = %s, updated_at = now() WHERE id = %s",
                        (target_id, str(payload.item_id)),
                    )
                    conn.commit()
                    _send_sms(phone, render_attached(target_title))
                    logger.info(
                        "thread-attached item_id=%s to=%s", payload.item_id, target_id
                    )
                    return {"status": "attached", "item_id": str(payload.item_id)}
                # No candidate on record for this item — falls through to
                # the generic unhandled-reply logging below, same as any
                # other stray text during AWAITING_CONFIRMATION.
    except HTTPException:
        raise
    except Exception:
        logger.exception("reply handling failed item_id=%s", payload.item_id)
        raise HTTPException(status_code=500, detail="reply handling failed") from None

    # OTHER during AWAITING_CONFIRMATION — a correction. Not built (see
    # module docstring); logged distinctly, no action taken.
    logger.info(
        "reply outside Y/N, not yet handled item_id=%s text=%r", payload.item_id, payload.text
    )
    return {"status": "unhandled_reply", "item_id": str(payload.item_id)}


@app.get("/health")
async def health():
    return {"status": "ok"}
