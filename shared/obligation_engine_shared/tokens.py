"""Signed, short-lived tokens for the web division's registration/session
flows (docs/design plan, Phases 3-5). One signing key
(`web-session-signing-key`, Secret Manager), one shared shape, reused by
scripts/approve_waitlist.py (mints the initial registration link),
registration-svc (verifies it, then mints/verifies its own intermediate
tokens across the OTP -> OAuth handoff), and Phase 5's dashboard-svc
(verifies its own login session tokens). A `purpose` claim keeps the
different token kinds from being interchangeable — a registration token
can never be replayed as a session token, since each caller only accepts
its own expected purpose.
"""

import time

import jwt


class InvalidToken(Exception):
    pass


def mint_signed_token(payload: dict, purpose: str, signing_key: str, ttl_seconds: int) -> str:
    now = int(time.time())
    claims = {**payload, "purpose": purpose, "iat": now, "exp": now + ttl_seconds}
    return jwt.encode(claims, signing_key, algorithm="HS256")


def verify_signed_token(token: str, purpose: str, signing_key: str) -> dict:
    try:
        claims = jwt.decode(token, signing_key, algorithms=["HS256"])
    except jwt.PyJWTError as e:
        raise InvalidToken(str(e)) from e
    if claims.get("purpose") != purpose:
        raise InvalidToken(f"expected purpose={purpose!r}, got {claims.get('purpose')!r}")
    return claims
