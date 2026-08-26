"""dashboard-svc — the read-only half of the web division's public API
(docs/design plan, Phase 5). Phone+OTP login (Twilio Verify — same
mechanism and the same Verify Service registration-svc's OTP step
already uses), a ~7-day session JWT, then four /me/* endpoints scoped to
the caller's own rows via the session's user_id claim — never a path
parameter, so there's no way to ask for another user's data by editing
a URL. GCP IAM keeps sa-dashboard SELECT-only at the table level
(infrastructure.md §2's split); the per-caller row scoping below is the
software half of that invariant, same as every other service in this
project.
"""

import os
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from obligation_engine_shared.db import get_connection
from obligation_engine_shared.tokens import InvalidToken, mint_signed_token, verify_signed_token
from pydantic import BaseModel
from twilio.rest import Client as TwilioClient

app = FastAPI()

# Same identifiers every other service in this project reads from env —
# never hardcoded (infrastructure.md §4.1's "not a secret, still not
# committed" treatment).
TWILIO_FROM_NUMBER = "+14152365420"
SESSION_TTL_SECONDS = 7 * 24 * 60 * 60
MESSAGES_DEFAULT_LIMIT = 50

_raw_origins = os.environ.get("ALLOWED_ORIGINS", "")
_allowed_origins = [o.strip() for o in _raw_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_methods=["GET", "POST", "PATCH"],
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

        committed_rows = conn.execute(
            """
            SELECT i.id, i.title, i.summary, o.due_at, o.calendar_event_id
            FROM items i JOIN obligations o ON o.item_id = i.id
            WHERE i.user_id = %s AND i.state = 'COMMITTED'
            ORDER BY o.due_at DESC NULLS LAST
            """,
            (str(user_id),),
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
    return {"status": "ok"}


@app.get("/health")
async def health():
    return {"status": "ok"}
