"""docs/design plan's web division (Phases 3-5) — the one signed-token
implementation shared by scripts/approve_waitlist.py, registration-svc,
and (later) dashboard-svc. No I/O."""

import time

import jwt
import pytest
from obligation_engine_shared.tokens import InvalidToken, mint_signed_token, verify_signed_token

KEY = "test-signing-key"


def test_round_trips_payload_and_purpose():
    token = mint_signed_token({"phone_e164": "+15551234567"}, "registration", KEY, ttl_seconds=60)
    claims = verify_signed_token(token, "registration", KEY)
    assert claims["phone_e164"] == "+15551234567"
    assert claims["purpose"] == "registration"


def test_wrong_purpose_rejected():
    token = mint_signed_token({"phone_e164": "+15551234567"}, "registration", KEY, ttl_seconds=60)
    with pytest.raises(InvalidToken):
        verify_signed_token(token, "oauth-session", KEY)


def test_expired_token_rejected():
    token = mint_signed_token({"phone_e164": "+15551234567"}, "registration", KEY, ttl_seconds=-1)
    with pytest.raises(InvalidToken):
        verify_signed_token(token, "registration", KEY)


def test_wrong_signing_key_rejected():
    token = mint_signed_token({"phone_e164": "+15551234567"}, "registration", KEY, ttl_seconds=60)
    with pytest.raises(InvalidToken):
        verify_signed_token(token, "registration", "a-different-key")


def test_tampered_token_rejected():
    # Flips a character in the middle of the token (solidly inside the
    # payload segment), not the last character of the whole string —
    # base64url's trailing padding bits mean a flipped *last* character
    # can occasionally decode to the exact same underlying bytes,
    # making that version of this test flaky (found via a real
    # intermittent failure, not by inspection).
    token = mint_signed_token({"phone_e164": "+15551234567"}, "registration", KEY, ttl_seconds=60)
    mid = len(token) // 2
    tampered_char = "A" if token[mid] != "A" else "B"
    tampered = token[:mid] + tampered_char + token[mid + 1 :]
    with pytest.raises(InvalidToken):
        verify_signed_token(tampered, "registration", KEY)


def test_missing_purpose_claim_rejected():
    now = int(time.time())
    # a hand-built token with no purpose claim at all — e.g. from some
    # other signer entirely, not just a different purpose value
    claims = {"phone_e164": "+15551234567", "iat": now, "exp": now + 60}
    token = jwt.encode(claims, KEY, algorithm="HS256")
    with pytest.raises(InvalidToken):
        verify_signed_token(token, "registration", KEY)
