"""ingest-svc — the single Twilio-facing webhook (docs/architecture/overview.md §2).

Step 3 scope only: validate the webhook signature, INSERT the RECEIVED row,
publish to items.raw. Text-only — media handling is step 11. The inbound-SMS
routing described in state-machine.md §4 (forwarding replies to resolver-svc/
dispatcher-svc) isn't implemented yet either: those services don't exist
until steps 5 and 8, so every message is treated as new for now.
"""

import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Request
from obligation_engine_shared.db import get_connection
from obligation_engine_shared.pubsub import publish
from obligation_engine_shared.schemas import RawItemMessage
from twilio.request_validator import RequestValidator

app = FastAPI()


def _public_url(request: Request) -> str:
    # Cloud Run terminates TLS at the edge and forwards internally as plain
    # HTTP; Twilio signs the public https:// URL, so reconstruct it rather
    # than trust request.url's scheme, which would read as http.
    host = request.headers.get("host", request.url.hostname)
    return f"https://{host}{request.url.path}"


def _validate_signature(request: Request, form: dict[str, str]) -> None:
    validator = RequestValidator(os.environ["TWILIO_AUTH_TOKEN"])
    signature = request.headers.get("X-Twilio-Signature", "")
    if not validator.validate(_public_url(request), form, signature):
        raise HTTPException(status_code=403, detail="invalid Twilio signature")


def _resolve_user_id(conn, phone_e164: str) -> UUID:
    row = conn.execute("SELECT id FROM users WHERE phone_e164 = %s", (phone_e164,)).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail="unknown sender — no bootstrapped user for this number",
        )
    return row[0]


@app.post("/webhook/sms")
async def sms_webhook(request: Request):
    form = dict(await request.form())
    _validate_signature(request, form)

    from_number = form.get("From")
    if not from_number:
        raise HTTPException(status_code=400, detail="missing From")

    item_id = uuid4()
    text = form.get("Body")

    with get_connection() as conn:
        user_id = _resolve_user_id(conn, from_number)
        conn.execute(
            """
            INSERT INTO items (id, user_id, raw_channel, raw_media_uri, ingested_at, state)
            VALUES (%s, %s, 'sms', NULL, now(), 'RECEIVED')
            """,
            (str(item_id), str(user_id)),
        )
        conn.commit()

    publish(
        "items-raw",
        RawItemMessage(
            item_id=item_id,
            user_id=user_id,
            media_uri=None,
            mime_type=None,
            text=text,
            received_at=datetime.now(UTC),
        ),
    )
    return {"status": "received", "item_id": str(item_id)}


@app.get("/health")
async def health():
    return {"status": "ok"}
