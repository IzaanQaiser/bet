"""committer-svc — the only service with write access to Calendar/Gmail
(ADR 0003). Consumes items.confirmed, branches on type: an obligation gets
a real Calendar event plus an obligations row; a latent gets no external
call, just a latents row. Either way, items.state becomes COMMITTED only
after the external write (if any) succeeds — a Calendar failure leaves the
item recoverable, not silently marked done (test-plan.md step 6).

Step 13 adds dead-letter persistence (infrastructure.md §2.1's "Resolved
gap": nothing else has both DB access and a reason to own this table) —
POST /pubsub/dlq, subscribed three times (once per .dlq topic, each with
its own ?stage= query param — see scripts/deploy.sh), writes a
dead_letters row and sets items.state='FAILED'. Verified empirically
against real Pub/Sub before writing this (not the emulator — confirmed
separately it doesn't implement push redelivery/dead-lettering at all):
a dead-lettered message's data is the original payload unchanged, and
Pub/Sub attaches the real delivery count as an attribute — see
obligation_engine_shared.pubsub.decode_dead_letter_envelope.

Also adds an idempotency guard on /pubsub/push, to catch the same class
of concurrent-redelivery race found for real in step 11's resolver-svc
testing. The guard is keyed on _already_committed() — whether the row
this exact message type would write (obligations for type="obligation",
latents for type="latent") already exists — not on items.state. An
earlier draft checked items.state != 'CONFIRMED', and a real bug found
verifying step 14's accept path against real infra showed why that's
wrong: dispatcher-svc's accept publish for a latent arrives with
items.state already 'COMMITTED' (from that item's *original* commit as
a latent) — a second, legitimate pass through this endpoint for the
same item, not a redelivery of anything. The state-only guard silently
swallowed it: no obligations row was ever written, no Calendar event
created, no error logged — the accept just vanished. See
_already_committed()'s docstring and state-machine.md §2.3 for the full
writeup.

Step 14 also exposed a smaller, related real gap: _commit_obligation's
items UPDATE only ever set state, never type — harmless for every path
built before this one, since a resolver-confirmed item's type never
changes between EXTRACTED and COMMITTED. dispatcher-svc's accept path is
the first caller that actually needs type to change (a latent becoming
an obligation) — committer-svc "has no way to tell the two apart, and
doesn't need to" per that doc, so the fix is to just always write
confirmed.type here, a no-op for the pre-existing path and correct for
the new one.
"""

import logging
import os
from datetime import timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from google.auth.transport.requests import AuthorizedSession
from google.cloud import secretmanager
from google.oauth2.credentials import Credentials
from obligation_engine_shared.db import get_connection
from obligation_engine_shared.pubsub import decode_dead_letter_envelope, decode_push_envelope
from obligation_engine_shared.schemas import ConfirmedItemMessage
from psycopg.types.json import Json

logger = logging.getLogger("committer_svc")
app = FastAPI()

CALENDAR_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"


def _secret_client() -> secretmanager.SecretManagerServiceClient:
    return secretmanager.SecretManagerServiceClient()


def _user_credentials(user_id) -> tuple[Credentials, str]:
    """Returns (Credentials, timezone) for the given user, or raises if the
    user has no linked Google account — real error, no fallback (per PRD
    §15: never write to the calendar on inference alone, and there's
    nothing to infer a missing OAuth grant with)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT google_refresh_token_ref, timezone FROM users WHERE id = %s",
            (str(user_id),),
        ).fetchone()
    if row is None or row[0] is None:
        raise RuntimeError(f"user {user_id} has no linked Google account")
    refresh_token_ref, timezone = row

    refresh_token = (
        _secret_client().access_secret_version(name=refresh_token_ref).payload.data.decode()
    )
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GOOGLE_OAUTH_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
        scopes=[CALENDAR_SCOPE],
    )
    return creds, timezone


def _write_calendar_event(
    confirmed: ConfirmedItemMessage, timezone: str, creds: Credentials
) -> str:
    due_at = confirmed.due_at
    if due_at.tzinfo is None:
        # Gemini may reason in local terms with no UTC offset attached — a
        # naive due_at is the user's local time, not UTC (agent-contracts.md
        # §1's "Resolved gap" note). Attach the zone, don't convert into it.
        due_at = due_at.replace(tzinfo=ZoneInfo(timezone))
    end_at = due_at + timedelta(minutes=confirmed.effort_minutes)

    session = AuthorizedSession(creds)
    response = session.post(
        CALENDAR_EVENTS_URL,
        json={
            "summary": confirmed.title,
            "description": confirmed.summary,
            "start": {"dateTime": due_at.isoformat(), "timeZone": timezone},
            "end": {"dateTime": end_at.isoformat(), "timeZone": timezone},
        },
    )
    response.raise_for_status()
    return response.json()["id"]


def _commit_obligation(confirmed: ConfirmedItemMessage) -> None:
    if confirmed.action_type != "calendar":
        # action_type == "email" is the step 15 stretch (ADR 0008) — not
        # built yet, and nothing in the pipeline sets it today. Fail loudly
        # rather than silently treating it as a calendar write.
        raise NotImplementedError(f"action_type={confirmed.action_type!r} not yet implemented")

    creds, timezone = _user_credentials(confirmed.user_id)
    calendar_event_id = _write_calendar_event(confirmed, timezone, creds)

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO obligations (item_id, due_at, calendar_event_id, action_type)
            VALUES (%s, %s, %s, %s)
            """,
            (str(confirmed.item_id), confirmed.due_at, calendar_event_id, confirmed.action_type),
        )
        conn.execute(
            "UPDATE items SET type = %s, state = 'COMMITTED', updated_at = now() WHERE id = %s",
            (confirmed.type, str(confirmed.item_id)),
        )
        conn.commit()


def _commit_latent(confirmed: ConfirmedItemMessage) -> None:
    with get_connection() as conn:
        conn.execute("INSERT INTO latents (item_id) VALUES (%s)", (str(confirmed.item_id),))
        conn.execute(
            "UPDATE items SET state = 'COMMITTED', updated_at = now() WHERE id = %s",
            (str(confirmed.item_id),),
        )
        conn.commit()


def _already_committed(conn, confirmed: ConfirmedItemMessage) -> bool:
    """Keyed on the row this exact message type would write, not on
    items.state — state='COMMITTED' is not itself proof this message was
    already handled. A real bug, found verifying step 14's accept path
    against real infra: dispatcher-svc's accept publish arrives for an
    item already state='COMMITTED' from its *original* latent commit
    (state-machine.md §2.3 — this is a second, legitimate pass through
    this endpoint for the same item, not a redelivery). A blanket
    `state != 'CONFIRMED'` guard silently swallowed it — no obligations
    row was ever written, no error logged, the accept just vanished."""
    if confirmed.type == "obligation":
        row = conn.execute(
            "SELECT 1 FROM obligations WHERE item_id = %s LIMIT 1", (str(confirmed.item_id),)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT 1 FROM latents WHERE item_id = %s LIMIT 1", (str(confirmed.item_id),)
        ).fetchone()
    return row is not None


@app.post("/pubsub/push")
async def pubsub_push(request: Request):
    envelope = await request.json()
    try:
        confirmed = decode_push_envelope(envelope, ConfirmedItemMessage)
    except Exception:
        logger.exception("malformed push envelope, could not decode ConfirmedItemMessage")
        raise HTTPException(status_code=500, detail="malformed envelope") from None

    with get_connection() as conn:
        already_committed = _already_committed(conn, confirmed)
    if already_committed:
        # A concurrent redelivery of the same items.confirmed message,
        # arriving after the first delivery already wrote the row this
        # exact message type would write. Not an error: ack it as a
        # no-op rather than retrying or double-writing Calendar.
        logger.info(
            "skipping already-committed item_id=%s type=%s (redelivery)",
            confirmed.item_id,
            confirmed.type,
        )
        return {"status": "already_processed", "item_id": str(confirmed.item_id)}

    try:
        if confirmed.type == "latent":
            _commit_latent(confirmed)
        else:
            _commit_obligation(confirmed)
    except Exception:
        logger.exception("commit failed item_id=%s", confirmed.item_id)
        raise HTTPException(status_code=500, detail="commit failed") from None

    logger.info("COMMITTED item_id=%s type=%s", confirmed.item_id, confirmed.type)
    return {"status": "committed", "item_id": str(confirmed.item_id)}


@app.post("/pubsub/dlq")
async def pubsub_dlq(request: Request, stage: str):
    envelope = await request.json()
    try:
        payload, retry_count = decode_dead_letter_envelope(envelope)
        item_id = payload["item_id"]
    except Exception:
        # Genuinely malformed — not even a decodable dead-lettered message.
        # Nothing sensible to record against an item_id we can't read; log
        # and ack rather than retry something that will never parse.
        logger.exception("malformed dead-letter envelope on stage=%s", stage)
        return {"status": "unparseable_dead_letter"}

    try:
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO dead_letters (item_id, stage, payload, error, retry_count)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    item_id,
                    stage,
                    Json(payload),
                    f"exceeded max delivery attempts on {stage}",
                    retry_count,
                ),
            )
            conn.execute(
                "UPDATE items SET state = 'FAILED', updated_at = now() WHERE id = %s",
                (item_id,),
            )
            conn.commit()
    except Exception:
        logger.exception("failed to persist dead letter item_id=%s stage=%s", item_id, stage)
        raise HTTPException(status_code=500, detail="dead letter persistence failed") from None

    logger.warning("FAILED item_id=%s stage=%s retry_count=%d", item_id, stage, retry_count)
    return {"status": "dead_lettered", "item_id": item_id}


@app.get("/health")
async def health():
    return {"status": "ok"}
