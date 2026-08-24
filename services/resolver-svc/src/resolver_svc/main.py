"""resolver-svc — step 9: the real AWAITING_CONFIRMATION gate, replacing
step 5's auto-confirm stub. Consumes items.extracted; a complete AND
confident item (state-machine.md §1.2's threshold, confidence >= 0.75)
gets a real confirmation card sent via SMS and waits for a real Y/N
reply (POST /reply, called synchronously by ingest-svc's routing check,
state-machine.md §4) instead of auto-confirming. An incomplete or
low-confidence item is still left in EXTRACTED exactly as the stub
already handled it — the real clarification loop is step 10.

A reply that isn't Y/N (a correction) is logged but not acted on yet —
full correction-handling reuses the clarification call built in step 10,
per test-plan.md's step 9 scope note.
"""

import logging
import os

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

from resolver_svc.templates import render_cancelled, render_confirmation_card

logger = logging.getLogger("resolver_svc")
app = FastAPI()

CONFIDENCE_THRESHOLD = 0.75  # state-machine.md §1.2

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


def _user_phone(conn, user_id) -> str:
    row = conn.execute("SELECT phone_e164 FROM users WHERE id = %s", (str(user_id),)).fetchone()
    return row[0]


@app.post("/pubsub/push")
async def pubsub_push(request: Request):
    envelope = await request.json()
    try:
        extracted = decode_push_envelope(envelope, ExtractedItemMessage)
    except Exception:
        logger.exception("malformed push envelope, could not decode ExtractedItemMessage")
        raise HTTPException(status_code=500, detail="malformed envelope") from None

    complete_and_confident = (
        not extracted.missing_fields and extracted.confidence >= CONFIDENCE_THRESHOLD
    )
    target_state = "AWAITING_CONFIRMATION" if complete_and_confident else "EXTRACTED"
    try:
        _write_item(extracted, target_state)
    except Exception:
        logger.exception("failed to write extracted fields item_id=%s", extracted.item_id)
        raise HTTPException(status_code=500, detail="db write failed") from None

    if not complete_and_confident:
        logger.info(
            "item left in EXTRACTED (real clarification is step 10) item_id=%s "
            "missing_fields=%s confidence=%s",
            extracted.item_id,
            extracted.missing_fields,
            extracted.confidence,
        )
        return {"status": "left_in_extracted", "item_id": str(extracted.item_id)}

    # due_at has no items column (data-model.md §2.4) — staged here so
    # the eventual Y reply (a separate request, no shared memory with
    # this one) can reconstruct it. agent-contracts.md §3.2's "creates
    # the conversations row unconditionally" note.
    resolved_fields = {"due_at": extracted.due_at.isoformat()} if extracted.due_at else {}
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO conversations (user_id, item_id, resolved_fields) VALUES (%s, %s, %s)",
                (str(extracted.user_id), str(extracted.item_id), Json(resolved_fields)),
            )
            phone = _user_phone(conn, extracted.user_id)
            conn.commit()
    except Exception:
        logger.exception("failed to open conversation item_id=%s", extracted.item_id)
        raise HTTPException(status_code=500, detail="conversation open failed") from None

    action_type = "calendar" if extracted.type == "obligation" else None
    body = render_confirmation_card(
        extracted.type,
        extracted.title,
        extracted.summary,
        extracted.due_at,
        extracted.effort_minutes,
        action_type,
    )
    try:
        _send_sms(phone, body)
    except Exception:
        logger.exception("failed to send confirmation card item_id=%s", extracted.item_id)
        raise HTTPException(status_code=500, detail="sms send failed") from None

    logger.info("AWAITING_CONFIRMATION item_id=%s sent confirmation card", extracted.item_id)
    return {"status": "awaiting_confirmation", "item_id": str(extracted.item_id)}


@app.post("/reply")
async def reply(payload: RoutedReplyMessage):
    classification = classify_reply(payload.text)

    try:
        with get_connection() as conn:
            item_row = conn.execute(
                "SELECT type, title, summary, effort_minutes FROM items WHERE id = %s",
                (str(payload.item_id),),
            ).fetchone()
            if item_row is None:
                raise HTTPException(status_code=404, detail="unknown item_id")
            item_type, title, summary, effort_minutes = item_row
            phone = _user_phone(conn, payload.user_id)

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
                    action_type="calendar" if item_type == "obligation" else None,
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

    # OTHER — a correction. Full handling reuses the clarification call
    # (step 10); for now, log distinctly and take no action, per
    # test-plan.md's step 9 scope note.
    logger.info(
        "reply outside Y/N, not yet handled item_id=%s text=%r", payload.item_id, payload.text
    )
    return {"status": "unhandled_reply", "item_id": str(payload.item_id)}


@app.get("/health")
async def health():
    return {"status": "ok"}
