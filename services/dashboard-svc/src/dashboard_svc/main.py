"""dashboard-svc — the web division's per-user read/write API (docs/design
plan, Phase 5). Phone+OTP login (Twilio Verify — same mechanism and the
same Verify Service registration-svc's OTP step already uses), a ~7-day
session JWT, then endpoints scoped to the caller's own rows via the
session's user_id claim — never a path parameter, so there's no way to
ask for another user's data by editing a URL. GCP IAM keeps sa-dashboard
mostly SELECT at the table level (infrastructure.md §2's split), with a
narrow UPDATE on items for the one write path (DELETE /me/items/{id});
the per-caller row scoping below is the software half of that invariant,
same as every other service in this project.
"""

import logging
import os
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from google.auth.transport.requests import AuthorizedSession
from google.cloud import secretmanager, tasks_v2
from google.oauth2.credentials import Credentials
from obligation_engine_shared.db import get_connection
from obligation_engine_shared.tokens import InvalidToken, mint_signed_token, verify_signed_token
from pydantic import BaseModel
from twilio.rest import Client as TwilioClient

logger = logging.getLogger("dashboard_svc")

app = FastAPI()

# Same identifiers every other service in this project reads from env —
# never hardcoded (infrastructure.md §4.1's "not a secret, still not
# committed" treatment).
TWILIO_FROM_NUMBER = "+14152365420"
SESSION_TTL_SECONDS = 7 * 24 * 60 * 60
MESSAGES_DEFAULT_LIMIT = 50
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"
CALENDAR_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"

# Same Cloud Tasks queue/OIDC-as-sa-dispatcher pattern committer-svc
# already uses for reminders and per-idea next-fit — see capacity-
# engine.md §6.1 for why a direct Cloud Task, not Pub/Sub, is the
# deliberate exception here.
TASKS_LOCATION = "us-central1"
TASKS_QUEUE = "reminders"

_raw_origins = os.environ.get("ALLOWED_ORIGINS", "")
_allowed_origins = [o.strip() for o in _raw_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "Authorization"],
)


def _twilio_verify_service():
    client = TwilioClient(
        os.environ["TWILIO_API_KEY_SID"],
        os.environ["TWILIO_API_KEY_SECRET"],
        os.environ["TWILIO_ACCOUNT_SID"],
    )
    return client.verify.v2.services(os.environ["TWILIO_VERIFY_SERVICE_SID"])


def _signing_key() -> str:
    return os.environ["WEB_SESSION_SIGNING_KEY"]


def current_user_id(authorization: str | None = Header(default=None)) -> UUID:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing session")
    token = authorization.removeprefix("Bearer ")
    try:
        claims = verify_signed_token(token, "dashboard-session", _signing_key())
    except InvalidToken:
        raise HTTPException(status_code=401, detail="invalid or expired session") from None
    return UUID(claims["user_id"])


class AuthStartRequest(BaseModel):
    phone_e164: str


@app.post("/auth/start")
async def auth_start(payload: AuthStartRequest):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE phone_e164 = %s", (payload.phone_e164,)
        ).fetchone()
    if row is None:
        # Same shape as any other rejection here — not "no such user",
        # which would let this endpoint be used to test which phone
        # numbers are registered.
        raise HTTPException(status_code=404, detail="not found")

    _twilio_verify_service().verifications.create(to=payload.phone_e164, channel="sms")
    return {"status": "sent"}


class AuthVerifyRequest(BaseModel):
    phone_e164: str
    code: str


@app.post("/auth/verify")
async def auth_verify(payload: AuthVerifyRequest):
    check = _twilio_verify_service().verification_checks.create(
        to=payload.phone_e164, code=payload.code
    )
    if check.status != "approved":
        raise HTTPException(status_code=400, detail="incorrect code")

    with get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE phone_e164 = %s", (payload.phone_e164,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="not found")

    session_token = mint_signed_token(
        {"user_id": str(row[0])}, "dashboard-session", _signing_key(), SESSION_TTL_SECONDS
    )
    return {"session_token": session_token}


_IN_PROGRESS_STATES = (
    "RECEIVED",
    "EXTRACTED",
    "DUPLICATE_SUSPECTED",
    "CLARIFYING",
    "NEEDS_REVIEW",
    "AWAITING_CONFIRMATION",
    "CONFIRMED",
)
_OTHER_STATES = ("CANCELLED", "MERGED", "FAILED")


@app.get("/me/items")
async def me_items(user_id: UUID = Depends(current_user_id)):
    with get_connection() as conn:
        in_progress_rows = conn.execute(
            """
            SELECT id, title, summary, state, updated_at
            FROM items
            WHERE user_id = %s AND state = ANY(%s)
            ORDER BY updated_at DESC
            """,
            (str(user_id), list(_IN_PROGRESS_STATES)),
        ).fetchall()
        item_ids = [str(r[0]) for r in in_progress_rows]
        conversation_rows = (
            conn.execute(
                """
                SELECT DISTINCT ON (item_id) item_id, pending_fields, last_message_at
                FROM conversations
                WHERE item_id = ANY(%s)
                ORDER BY item_id, last_message_at DESC
                """,
                (item_ids,),
            ).fetchall()
            if item_ids
            else []
        )
        conversations_by_item = {
            str(r[0]): {"pending_fields": r[1], "last_message_at": r[2]} for r in conversation_rows
        }

        # A committed idea (type='latent') never gets an obligations row —
        # it lives in latents instead, keyed the same way by item_id — so
        # an inner JOIN obligations alone silently excludes every "someday"
        # idea from this list. Real bug, found live: an idea the user was
        # just told "i've got that down for you" for was nowhere to be
        # found here. UNION ALL in the latent side with due_at/
        # calendar_event_id as NULL. i.type is included (not inferred from
        # a null due_at on the frontend — an email-action obligation can
        # legitimately have a null due_at too, so that heuristic isn't
        # reliable) so the frontend can group ideas separately from real
        # committed obligations.
        # next_fit_start is only ever meaningful for an idea (a real
        # obligation has no next_fit_start row at all) — NULL on the
        # obligation branch, real values from latents on the other.
        committed_rows = conn.execute(
            """
            SELECT i.id, i.title, i.summary, o.due_at, o.calendar_event_id, i.effort_minutes,
                   i.type, NULL::timestamptz
            FROM items i JOIN obligations o ON o.item_id = i.id
            WHERE i.user_id = %s AND i.state = 'COMMITTED'
            UNION ALL
            SELECT i.id, i.title, i.summary, NULL::timestamptz, NULL::text, i.effort_minutes,
                   i.type, l.next_fit_start
            FROM items i JOIN latents l ON l.item_id = i.id
            WHERE i.user_id = %s AND i.state = 'COMMITTED'
            ORDER BY 4 DESC NULLS LAST
            """,
            (str(user_id), str(user_id)),
        ).fetchall()

        other_rows = conn.execute(
            """
            SELECT id, title, state, updated_at
            FROM items
            WHERE user_id = %s AND state = ANY(%s)
            ORDER BY updated_at DESC
            """,
            (str(user_id), list(_OTHER_STATES)),
        ).fetchall()

    in_progress = []
    for r in in_progress_rows:
        conv = conversations_by_item.get(str(r[0]), {})
        last_message_at = conv.get("last_message_at")
        in_progress.append(
            {
                "id": str(r[0]),
                "title": r[1],
                "summary": r[2],
                "state": r[3],
                "updated_at": r[4].isoformat(),
                "pending_fields": conv.get("pending_fields"),
                "last_message_at": last_message_at.isoformat() if last_message_at else None,
            }
        )

    return {
        "in_progress": in_progress,
        "committed": [
            {
                "id": str(r[0]),
                "title": r[1],
                "summary": r[2],
                "due_at": r[3].isoformat() if r[3] else None,
                "calendar_event_id": r[4],
                "effort_minutes": r[5],
                "type": r[6],
                "next_fit_start": r[7].isoformat() if r[7] else None,
            }
            for r in committed_rows
        ],
        "other": [
            {"id": str(r[0]), "title": r[1], "state": r[2], "updated_at": r[3].isoformat()}
            for r in other_rows
        ],
    }


@app.get("/me/messages")
async def me_messages(
    user_id: UUID = Depends(current_user_id),
    limit: int = Query(default=MESSAGES_DEFAULT_LIMIT, ge=1, le=200),
):
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT direction, body, created_at FROM messages WHERE user_id = %s "
            "ORDER BY created_at DESC LIMIT %s",
            (str(user_id), limit),
        ).fetchall()
    # DESC-limited to get the most recent page, then reversed so the
    # response itself reads oldest-first, matching how a chat transcript
    # is displayed.
    rows.reverse()
    return {
        "messages": [
            {"direction": r[0], "body": r[1], "created_at": r[2].isoformat()} for r in rows
        ]
    }


@app.get("/me/suggestions")
async def me_suggestions(user_id: UUID = Depends(current_user_id)):
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT s.id, i.title, s.outcome, s.sent_at, s.responded_at
            FROM suggestions s JOIN items i ON i.id = s.item_id
            WHERE s.user_id = %s
            ORDER BY s.sent_at DESC
            """,
            (str(user_id),),
        ).fetchall()
    return {
        "suggestions": [
            {
                "id": str(r[0]),
                "title": r[1],
                "outcome": r[2],
                "sent_at": r[3].isoformat(),
                "responded_at": r[4].isoformat() if r[4] else None,
            }
            for r in rows
        ]
    }


@app.get("/me/profile")
async def me_profile_get(user_id: UUID = Depends(current_user_id)):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT phone_e164, timezone, working_hours_start, working_hours_end "
            "FROM users WHERE id = %s",
            (str(user_id),),
        ).fetchone()
    return {
        "phone_e164": row[0],
        "timezone": row[1],
        "working_hours_start": row[2].isoformat(),
        "working_hours_end": row[3].isoformat(),
    }


class ProfileUpdateRequest(BaseModel):
    timezone: str | None = None
    working_hours_start: str | None = None
    working_hours_end: str | None = None


def _enqueue_next_fit_recompute(user_id: UUID) -> None:
    """User-directed real bug fix: changing working hours previously left
    every already-committed idea's next_fit_start stale until the next
    twice-daily /dispatch sweep — a user who just widened their working
    hours to fit an idea in today saw no change at all. Fires an
    immediate (no schedule_time) Cloud Task at dispatcher-svc's
    POST /users/{user_id}/next-fit, same reminders-queue/OIDC-as-
    sa-dispatcher pattern committer-svc already uses. Best-effort, not
    fatal to the profile save: a failed enqueue just means the stale
    slots persist until the next sweep instead of updating within
    seconds."""
    try:
        project_id = os.environ["GCP_PROJECT_ID"]
        dispatcher_url = os.environ["DISPATCHER_SVC_URL"]
        url = f"{dispatcher_url}/users/{user_id}/next-fit"
        dispatcher_sa = f"sa-dispatcher@{project_id}.iam.gserviceaccount.com"
        client = tasks_v2.CloudTasksClient()
        parent = client.queue_path(project_id, TASKS_LOCATION, TASKS_QUEUE)
        client.create_task(
            parent=parent,
            task={
                "http_request": {
                    "http_method": tasks_v2.HttpMethod.POST,
                    "url": url,
                    "oidc_token": {"service_account_email": dispatcher_sa, "audience": url},
                },
            },
        )
    except Exception:
        logger.exception(
            "failed to enqueue next-fit recompute user_id=%s (stale until next sweep)", user_id
        )


@app.patch("/me/profile")
async def me_profile_patch(
    payload: ProfileUpdateRequest, user_id: UUID = Depends(current_user_id)
):
    if payload.timezone is not None:
        try:
            ZoneInfo(payload.timezone)
        except ZoneInfoNotFoundError:
            raise HTTPException(status_code=400, detail="unknown timezone") from None

    fields: dict[str, str] = {}
    if payload.timezone is not None:
        fields["timezone"] = payload.timezone
    if payload.working_hours_start is not None:
        fields["working_hours_start"] = payload.working_hours_start
    if payload.working_hours_end is not None:
        fields["working_hours_end"] = payload.working_hours_end
    if not fields:
        raise HTTPException(status_code=400, detail="nothing to update")

    set_clause = ", ".join(f"{col} = %s" for col in fields)
    with get_connection() as conn:
        conn.execute(
            f"UPDATE users SET {set_clause} WHERE id = %s",  # noqa: S608 — column names are our own fixed keys, not user input
            (*fields.values(), str(user_id)),
        )
        conn.commit()

    if "working_hours_start" in fields or "working_hours_end" in fields:
        _enqueue_next_fit_recompute(user_id)
    return {"status": "ok"}


def _user_calendar_credentials(refresh_token_ref: str) -> Credentials:
    client = secretmanager.SecretManagerServiceClient()
    refresh_token = client.access_secret_version(name=refresh_token_ref).payload.data.decode()
    return Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GOOGLE_OAUTH_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
        scopes=[CALENDAR_SCOPE],
    )


@app.delete("/me/items/{item_id}")
async def delete_item(item_id: UUID, user_id: UUID = Depends(current_user_id)):
    """Clears an item out of the caller's own dashboard — works for both
    an in-progress "agent memory" entry and a committed calendar item.
    Real feedback this exists for: deleting an event directly in Google
    Calendar doesn't tell this system anything on its own — calendar-
    sync-svc (ADR-0009-adjacent two-way sync) is the *other* direction
    for that; this endpoint handles delete-here-first. Best-effort
    deletes the real Calendar event too, not just our own row. Soft-
    deletes (state='CANCELLED'), doesn't remove the row — obligations/
    conversations/suggestions rows still reference it.

    Real bug, found while building two-way sync: this only ever looked up
    obligations.calendar_event_id — deleting an *idea* here never deleted
    its real [idea]-tagged placeholder at all, only its DB row. Fixed by
    also checking latents.placeholder_event_id (an item is one or the
    other, never both, so at most one of the two is ever non-null).
    """
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT i.user_id, o.calendar_event_id, l.placeholder_event_id,
                   u.google_refresh_token_ref
            FROM items i
            JOIN users u ON u.id = i.user_id
            LEFT JOIN obligations o ON o.item_id = i.id
            LEFT JOIN latents l ON l.item_id = i.id
            WHERE i.id = %s
            """,
            (str(item_id),),
        ).fetchone()
    if row is None or row[0] != user_id:
        # Same as every other cross-user lookup here — a 404, not a 403,
        # so this never confirms whether the item exists for someone else.
        raise HTTPException(status_code=404, detail="not found")

    _, calendar_event_id, placeholder_event_id, refresh_token_ref = row
    event_id = calendar_event_id or placeholder_event_id
    if event_id and refresh_token_ref:
        try:
            session = AuthorizedSession(_user_calendar_credentials(refresh_token_ref))
            resp = session.delete(f"{CALENDAR_EVENTS_URL}/{event_id}")
            # 404/410: already gone — e.g. the user deleted it directly in
            # Google Calendar, which is exactly the case calendar-sync-svc
            # exists to reconcile from the other direction. Goal state,
            # not a failure.
            if resp.status_code not in (200, 204, 404, 410):
                resp.raise_for_status()
        except Exception:
            # Best-effort: a Calendar API hiccup shouldn't block the user
            # from clearing this item out of their own dashboard.
            pass

    with get_connection() as conn:
        conn.execute("UPDATE items SET state = 'CANCELLED' WHERE id = %s", (str(item_id),))
        if placeholder_event_id:
            conn.execute(
                "UPDATE latents SET next_fit_start = NULL, placeholder_event_id = NULL "
                "WHERE item_id = %s",
                (str(item_id),),
            )
        conn.commit()
    return {"status": "ok"}


@app.get("/health")
async def health():
    return {"status": "ok"}
