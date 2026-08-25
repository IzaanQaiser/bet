"""registration-svc — the write-capable half of the web division's public
API (docs/design plan, Phase 2). Step 1 scope: just `POST /waitlist/join`,
deliberately the lightest possible endpoint — no verification of any kind,
since nothing privileged happens until a product-owner-run script approves
a specific number (Phase 3). Phase 4 adds the OAuth/Twilio-Verify
registration flow to this same service.
"""

import os
import re
import time
from collections import defaultdict, deque

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from obligation_engine_shared.db import get_connection
from pydantic import BaseModel, field_validator

app = FastAPI()

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


@app.get("/health")
async def health():
    return {"status": "ok"}
