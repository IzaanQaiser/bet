"""committer-svc — the only service with write access to Calendar/Gmail
(ADR 0003). Consumes items.confirmed, branches on type: an obligation gets
a real Calendar event plus an obligations row; a latent gets no external
call, just a latents row. Either way, items.state becomes COMMITTED only
after the external write (if any) succeeds — a Calendar failure leaves the
item recoverable, not silently marked done (test-plan.md step 6).

Dead-letter persistence (subscribing to the three .dlq topics) is step 13
scope, not built here yet — see infrastructure.md §2.1's "Resolved gap"
note for why committer-svc is the eventual owner of that table.
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
from obligation_engine_shared.pubsub import decode_push_envelope
from obligation_engine_shared.schemas import ConfirmedItemMessage

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
            "UPDATE items SET state = 'COMMITTED', updated_at = now() WHERE id = %s",
            (str(confirmed.item_id),),
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


@app.post("/pubsub/push")
async def pubsub_push(request: Request):
    envelope = await request.json()
    try:
        confirmed = decode_push_envelope(envelope, ConfirmedItemMessage)
    except Exception:
        logger.exception("malformed push envelope, could not decode ConfirmedItemMessage")
        raise HTTPException(status_code=500, detail="malformed envelope") from None

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


@app.get("/health")
async def health():
    return {"status": "ok"}
