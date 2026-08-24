"""resolver-svc — step 5 stub (temporary, auto-confirm). Per docs/engineering/
test-plan.md step 5: consumes items.extracted, writes the extracted fields
onto the items row, and — only when missing_fields is already empty —
immediately publishes items.confirmed. No dedupe check, no clarification
loop, no confirmation SMS: those are the real resolver (docs/architecture/
agent-contracts.md §3, built in steps 9-10), which replaces this file
rather than extending it. This exists so committer-svc (step 6) and the
capacity engine (steps 7-8) have real confirmed items to work against
before the real gate is built.
"""

import logging

from fastapi import FastAPI, HTTPException, Request
from obligation_engine_shared.db import get_connection
from obligation_engine_shared.pubsub import decode_push_envelope, publish
from obligation_engine_shared.schemas import ConfirmedItemMessage, ExtractedItemMessage

logger = logging.getLogger("resolver_svc")
app = FastAPI()


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


@app.post("/pubsub/push")
async def pubsub_push(request: Request):
    envelope = await request.json()
    try:
        extracted = decode_push_envelope(envelope, ExtractedItemMessage)
    except Exception:
        logger.exception("malformed push envelope, could not decode ExtractedItemMessage")
        raise HTTPException(status_code=500, detail="malformed envelope") from None

    target_state = "EXTRACTED" if extracted.missing_fields else "CONFIRMED"
    try:
        _write_item(extracted, target_state)
    except Exception:
        logger.exception("failed to write extracted fields item_id=%s", extracted.item_id)
        raise HTTPException(status_code=500, detail="db write failed") from None

    if extracted.missing_fields:
        logger.info(
            "item left in EXTRACTED (stub does not clarify) item_id=%s missing_fields=%s",
            extracted.item_id,
            extracted.missing_fields,
        )
        return {"status": "left_in_extracted", "item_id": str(extracted.item_id)}

    confirmed = ConfirmedItemMessage(
        item_id=extracted.item_id,
        user_id=extracted.user_id,
        type=extracted.type,
        title=extracted.title,
        summary=extracted.summary,
        due_at=extracted.due_at,
        effort_minutes=extracted.effort_minutes,
        action_type="calendar" if extracted.type == "obligation" else None,
        email_draft=None,
    )
    try:
        publish("items-confirmed", confirmed)
    except Exception:
        logger.exception("publish failed item_id=%s", extracted.item_id)
        raise HTTPException(status_code=500, detail="publish failed") from None

    logger.info("AUTO-CONFIRMED (stub, no gate) item_id=%s", extracted.item_id)
    return {"status": "confirmed", "item_id": str(extracted.item_id)}


@app.get("/health")
async def health():
    return {"status": "ok"}
