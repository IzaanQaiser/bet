"""resolver-svc — step 10: the real clarification loop, replacing step
9's "leave incomplete items in EXTRACTED, do nothing" placeholder.

An item with missing_fields (in practice, always just ["due_at"] — see
clarification.py's module docstring) now gets a real clarifying question
via SMS instead of stalling. Up to 3 exchanges (state-machine.md §1.2);
if still incomplete after the 3rd, the item terminates at NEEDS_REVIEW
with no 4th question. A complete/confident item still goes straight to
AWAITING_CONFIRMATION exactly as step 9 built it.

Still deliberately not built: a correction reply during
AWAITING_CONFIRMATION (a reply that isn't Y/N) is still just logged, no
action taken. Step 9's comment called this "step 10's job"; on closer
look it needs its own field-targeting heuristic (agent-contracts.md
§3.2's "cheap heuristic" paragraph) that doesn't reuse this step's
due_at-only clarification model cleanly, so it's deferred again here,
explicitly, rather than bolted on halfway.
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
from resolver_svc.templates import (
    render_cancelled,
    render_confirmation_card,
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
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE items
            SET type = %s, title = %s, summary = %s, effort_minutes = %s,
                focus_depth = %s, confidence = %s, state = %s, updated_at = now()
            WHERE id = %s
            """,
            (
                extracted.type,
                extracted.title,
                extracted.summary,
                extracted.effort_minutes,
                extracted.focus_depth,
                extracted.confidence,
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


def _action_type(item_type: str) -> str | None:
    return "calendar" if item_type == "obligation" else None


async def _start_clarification(extracted: ExtractedItemMessage) -> None:
    _write_item(extracted, "CLARIFYING")

    with get_connection() as conn:
        phone, tz_name = _user_phone_and_timezone(conn, extracted.user_id)

    now_local = datetime.now(UTC).astimezone(ZoneInfo(tz_name))
    result = await clarify(
        session_id=f"{extracted.item_id}-0",
        now_local=now_local,
        tz_name=tz_name,
        title=extracted.title,
        latest_reply=None,
    )
    resolved_fields = {"due_at": result.due_at} if result.due_at_filled and result.due_at else {}

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
    _write_item(extracted, "AWAITING_CONFIRMATION")
    body = render_confirmation_card(
        extracted.type,
        extracted.title,
        extracted.summary,
        result.due_at and datetime.fromisoformat(result.due_at),
        extracted.effort_minutes,
        _action_type(extracted.type),
    )
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

    if extracted.missing_fields:
        try:
            await _start_clarification(extracted)
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

    resolved_fields = {"due_at": extracted.due_at.isoformat()} if extracted.due_at else {}
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
        _action_type(extracted.type),
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
    _pending_fields, resolved_fields, exchange_count = convo_row

    now_local = datetime.now(UTC).astimezone(ZoneInfo(tz_name))
    result = await clarify(
        session_id=f"{item_id}-{exchange_count}",
        now_local=now_local,
        tz_name=tz_name,
        title=title,
        latest_reply=latest_reply,
    )
    if result.due_at_filled and result.due_at:
        resolved_fields = {**resolved_fields, "due_at": result.due_at}

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

    conn.execute(
        "UPDATE items SET state = 'AWAITING_CONFIRMATION', updated_at = now() WHERE id = %s",
        (str(item_id),),
    )
    conn.execute(
        "UPDATE conversations SET resolved_fields = %s, pending_fields = %s, "
        "last_message_at = now() WHERE item_id = %s",
        (Json(resolved_fields), [], str(item_id)),
    )
    conn.commit()
    due_at = resolved_fields.get("due_at")
    body = render_confirmation_card(
        item_type,
        title,
        summary,
        datetime.fromisoformat(due_at) if due_at else None,
        effort_minutes,
        _action_type(item_type),
    )
    _send_sms(phone, body)
    logger.info("AWAITING_CONFIRMATION item_id=%s (clarification resolved)", item_id)
    return {"status": "awaiting_confirmation", "item_id": str(item_id)}


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
                    action_type=_action_type(item_type),
                    email_draft=None,
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
