"""ingest-svc — the single Twilio-facing webhook (docs/architecture/overview.md §2).

Step 3 scope: validate the webhook signature, INSERT the RECEIVED row,
publish to items.raw. Text-only — media handling is step 11.

Step 9 adds the inbound-SMS routing check (state-machine.md §4): before
treating a message as new, check for an open conversation (an item in
CLARIFYING or AWAITING_CONFIRMATION) for that user, and forward to
resolver-svc's /reply instead. The second branch of that routing table
— forwarding a suggestion Y/N/Later reply to dispatcher-svc — is not
built yet; dispatcher-svc has no accept-path endpoint until the
feedback-loop step, so that branch would have nothing to call. Not a
silent gap: nothing sends a suggestion that could be replied to in this
deployment's current state either, so it can't be hit for real yet.
"""

import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

import requests
from fastapi import FastAPI, HTTPException, Request
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.id_token import fetch_id_token
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


def _open_conversation_item_id(conn, user_id: UUID) -> UUID | None:
    row = conn.execute(
        """
        SELECT c.item_id FROM conversations c
        JOIN items i ON i.id = c.item_id
        WHERE c.user_id = %s AND i.state IN ('CLARIFYING', 'AWAITING_CONFIRMATION')
        ORDER BY c.last_message_at DESC LIMIT 1
        """,
        (str(user_id),),
    ).fetchone()
    return row[0] if row else None


def _forward_to_resolver(user_id: UUID, item_id: UUID, text: str) -> None:
    resolver_url = os.environ["RESOLVER_SVC_URL"]
    id_token = fetch_id_token(GoogleAuthRequest(), resolver_url)
    response = requests.post(
        f"{resolver_url}/reply",
        json={"user_id": str(user_id), "item_id": str(item_id), "text": text},
        headers={"Authorization": f"Bearer {id_token}"},
        timeout=30,
    )
    response.raise_for_status()


@app.post("/webhook/sms")
async def sms_webhook(request: Request):
    form = dict(await request.form())
    _validate_signature(request, form)

    from_number = form.get("From")
    if not from_number:
        raise HTTPException(status_code=400, detail="missing From")
    text = form.get("Body")

    item_id: UUID | None = None
    with get_connection() as conn:
        user_id = _resolve_user_id(conn, from_number)
        open_item_id = _open_conversation_item_id(conn, user_id)
        if open_item_id is None:
            item_id = uuid4()
            conn.execute(
                """
                INSERT INTO items (id, user_id, raw_channel, raw_media_uri, ingested_at, state)
                VALUES (%s, %s, 'sms', NULL, now(), 'RECEIVED')
                """,
                (str(item_id), str(user_id)),
            )
            conn.commit()

    if open_item_id is not None:
        _forward_to_resolver(user_id, open_item_id, text or "")
        return {"status": "routed_to_resolver", "item_id": str(open_item_id)}

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
