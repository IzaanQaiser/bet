"""registration-svc — the write-capable half of the web division's public
API (docs/design plan). Phase 2 scope was just `POST /waitlist/join` — no
verification of any kind, since nothing privileged happens until a
product-owner-run script approves a specific number (Phase 3). Phase 4
adds the real registration completion flow this module now also carries:
Twilio Verify OTP (the first point phone ownership is actually proven,
deliberately deferred from waitlist-join), then Google OAuth consent,
ending in a real `users` row and a real per-user refresh-token secret —
mirroring scripts/bootstrap_oauth_token.py's create_secret/
add_secret_version pattern, just triggered by a browser redirect instead
of a CLI arg.

The four endpoints are deliberately stateless server-side — each step's
state (phone number, then + timezone) travels in a signed token
(obligation_engine_shared.tokens, `web-session-signing-key`) rather than
a server-side session store, so any registration-svc instance can handle
any step without shared session state between them.
"""

import os
import re
import time
from collections import defaultdict, deque
from urllib.parse import urlencode
from uuid import uuid4

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from google.cloud import secretmanager
from obligation_engine_shared.db import get_connection
from obligation_engine_shared.tokens import InvalidToken, mint_signed_token, verify_signed_token
from pydantic import BaseModel, field_validator
from twilio.rest import Client as TwilioClient

app = FastAPI()

GOOGLE_OAUTH_SCOPES = (
    "https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/gmail.send"
)
OTP_SESSION_TTL_SECONDS = 15 * 60
OAUTH_STATE_TTL_SECONDS = 10 * 60

# ITU E.164: a leading +, then 1-15 digits, first digit non-zero.
_E164_RE = re.compile(r"^\+[1-9]\d{1,14}$")

_raw_origins = os.environ.get("ALLOWED_ORIGINS", "")
_allowed_origins = [o.strip() for o in _raw_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_methods=["POST"],
    allow_headers=["Content-Type"],
)

# Cheap, per-instance-only insurance against garbage-number spam — not a
# distributed limiter (Cloud Run can run more than one instance), just
# enough friction that hammering a single instance's /waitlist/join isn't
# free. A real abuse problem would need something shared (Redis/Firestore),
# not worth building for a table with nothing sensitive behind it yet.
_RATE_LIMIT = 5
_RATE_WINDOW_SECONDS = 60
_recent_requests: dict[str, deque[float]] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    # Cloud Run terminates TLS at the edge and forwards through a proxy —
    # same reasoning as ingest-svc's _public_url — so the real client IP is
    # the first hop in X-Forwarded-For, not request.client.host.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _rate_limited(ip: str) -> bool:
    now = time.monotonic()
    window = _recent_requests[ip]
    while window and now - window[0] > _RATE_WINDOW_SECONDS:
        window.popleft()
    if len(window) >= _RATE_LIMIT:
        return True
    window.append(now)
    return False


class WaitlistJoinRequest(BaseModel):
    phone_e164: str
    name: str

    @field_validator("phone_e164")
    @classmethod
    def _validate_e164(cls, v: str) -> str:
        if not _E164_RE.match(v):
            raise ValueError("phone_e164 must be E.164 format, e.g. +15551234567")
        return v

    @field_validator("name")
    @classmethod
    def _require_nonblank_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name is required")
        return v


@app.post("/waitlist/join")
async def waitlist_join(payload: WaitlistJoinRequest, request: Request):
    if _rate_limited(_client_ip(request)):
        raise HTTPException(status_code=429, detail="too many requests, try again shortly")

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO waitlist (phone_e164, name) VALUES (%s, %s)
            ON CONFLICT (phone_e164) DO NOTHING
            """,
            (payload.phone_e164, payload.name),
        )
        conn.commit()

    return {"status": "ok"}


def _twilio_verify_service():
    # Not secrets by Twilio's own credential model — a SID can't
    # authenticate anything without its paired secret (infrastructure.md
    # §4.1). Read from env anyway, not hardcoded: keeps them out of source
    # scans entirely rather than relying on a reviewer knowing that
    # distinction.
    client = TwilioClient(
        os.environ["TWILIO_API_KEY_SID"],
        os.environ["TWILIO_API_KEY_SECRET"],
        os.environ["TWILIO_ACCOUNT_SID"],
    )
    return client.verify.v2.services(os.environ["TWILIO_VERIFY_SERVICE_SID"])


class RegistrationTokenRequest(BaseModel):
    token: str


class VerifyOtpRequest(BaseModel):
    token: str
    code: str


@app.post("/register/verify-start")
async def register_verify_start(payload: RegistrationTokenRequest):
    signing_key = os.environ["WEB_SESSION_SIGNING_KEY"]
    try:
        claims = verify_signed_token(payload.token, "registration", signing_key)
    except InvalidToken:
        raise HTTPException(status_code=400, detail="invalid or expired registration link")
    phone = claims["phone_e164"]

    with get_connection() as conn:
        row = conn.execute(
            "SELECT approved_at FROM waitlist WHERE phone_e164 = %s", (phone,)
        ).fetchone()
    if row is None or row[0] is None:
        raise HTTPException(status_code=400, detail="this number is not approved")

    _twilio_verify_service().verifications.create(to=phone, channel="sms")
    return {"status": "sent"}


@app.post("/register/verify-otp")
async def register_verify_otp(payload: VerifyOtpRequest):
    signing_key = os.environ["WEB_SESSION_SIGNING_KEY"]
    try:
        claims = verify_signed_token(payload.token, "registration", signing_key)
    except InvalidToken:
        raise HTTPException(status_code=400, detail="invalid or expired registration link")
    phone = claims["phone_e164"]

    check = _twilio_verify_service().verification_checks.create(to=phone, code=payload.code)
    if check.status != "approved":
        raise HTTPException(status_code=400, detail="incorrect code")

    oauth_session_token = mint_signed_token(
        {"phone_e164": phone}, "oauth-session", signing_key, OTP_SESSION_TTL_SECONDS
    )
    return {"oauth_session_token": oauth_session_token}


@app.get("/register/oauth-start")
async def register_oauth_start(token: str, timezone: str):
    signing_key = os.environ["WEB_SESSION_SIGNING_KEY"]
    try:
        claims = verify_signed_token(token, "oauth-session", signing_key)
    except InvalidToken:
        raise HTTPException(
            status_code=400, detail="invalid or expired session, restart registration"
        )
    phone = claims["phone_e164"]

    state = mint_signed_token(
        {"phone_e164": phone, "timezone": timezone},
        "oauth-callback",
        signing_key,
        OAUTH_STATE_TTL_SECONDS,
    )
    params = {
        "client_id": os.environ["GOOGLE_OAUTH_CLIENT_ID_WEB"],
        "redirect_uri": os.environ["OAUTH_REDIRECT_URI"],
        "response_type": "code",
        "scope": GOOGLE_OAUTH_SCOPES,
        "access_type": "offline",
        # Forces reconsent so Google issues a refresh_token on every grant,
        # not just the first — the exact gotcha bootstrap_oauth_token.py's
        # own docstring documents for the CLI flow; equally true here.
        "prompt": "consent",
        "state": state,
    }
    auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"
    return RedirectResponse(auth_url, status_code=302)


def _register_redirect(query: str) -> RedirectResponse:
    # oauth-callback is only ever reached via a real top-level browser
    # navigation (Google's own redirect), never a fetch/XHR — a raw JSON
    # error response here is dead-end UX no matter which branch fires, so
    # every outcome (success or failure) lands back on /register with a
    # query param instead of a bare HTTPException.
    web_base_url = os.environ.get("WEB_BASE_URL", "https://izaanqaiser.github.io/bet")
    return RedirectResponse(f"{web_base_url}/register?{query}", status_code=302)


@app.get("/register/oauth-callback")
async def register_oauth_callback(code: str, state: str):
    signing_key = os.environ["WEB_SESSION_SIGNING_KEY"]
    try:
        claims = verify_signed_token(state, "oauth-callback", signing_key)
    except InvalidToken:
        return _register_redirect("error=session_expired")
    phone = claims["phone_e164"]
    tz = claims["timezone"]

    with get_connection() as conn:
        existing = conn.execute("SELECT id FROM users WHERE phone_e164 = %s", (phone,)).fetchone()
    if existing is not None:
        return _register_redirect("already=1")

    token_response = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": code,
            "client_id": os.environ["GOOGLE_OAUTH_CLIENT_ID_WEB"],
            "client_secret": os.environ["GOOGLE_OAUTH_CLIENT_SECRET_WEB"],
            "redirect_uri": os.environ["OAUTH_REDIRECT_URI"],
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    token_response.raise_for_status()
    refresh_token = token_response.json().get("refresh_token")
    if not refresh_token:
        return _register_redirect("error=no_refresh_token")

    # Same create_secret/add_secret_version pattern bootstrap_oauth_token.py
    # uses for the CLI flow, same secret naming — just with a freshly
    # generated user_id instead of one the caller already has, since the
    # users row doesn't exist yet at this point.
    user_id = uuid4()
    project_id = os.environ["GCP_PROJECT_ID"]
    secret_client = secretmanager.SecretManagerServiceClient()
    secret = secret_client.create_secret(
        request={
            "parent": f"projects/{project_id}",
            "secret_id": f"user-refresh-token-{user_id}",
            "secret": {"replication": {"automatic": {}}},
        }
    )
    secret_client.add_secret_version(
        request={"parent": secret.name, "payload": {"data": refresh_token.encode()}}
    )
    refresh_token_ref = f"{secret.name}/versions/latest"

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO users (id, phone_e164, timezone, google_refresh_token_ref)
            VALUES (%s, %s, %s, %s)
            """,
            (str(user_id), phone, tz, refresh_token_ref),
        )
        conn.commit()

    return _register_redirect("done=1")


@app.get("/health")
async def health():
    return {"status": "ok"}
