"""Unit tests — DB mocked out, per docs/engineering/test-plan.md's existing
house style (unittest.mock.patch, resolver-svc/tests/test_resolver.py is
the reference). Covers docs/design plan Phase 2's stated acceptance cases:
valid join, duplicate join is a no-op not an error, malformed phone
rejected — plus the rate limiter added alongside them.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


def _mock_connection():
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    from registration_svc.main import _recent_requests

    _recent_requests.clear()
    yield
    _recent_requests.clear()


@pytest.fixture
def client():
    from registration_svc.main import app

    return TestClient(app, base_url="https://registration.example.com")


def test_valid_join_inserts_row(client):
    mock_conn = _mock_connection()
    with patch("registration_svc.main.get_connection", return_value=mock_conn):
        resp = client.post("/waitlist/join", json={"phone_e164": "+15551234567", "name": "Sarah"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    sql, params = mock_conn.execute.call_args[0]
    assert "INSERT INTO waitlist" in sql
    assert "ON CONFLICT (phone_e164) DO NOTHING" in sql
    assert params == ("+15551234567", "Sarah")
    mock_conn.commit.assert_called_once()


def test_join_without_name_is_optional(client):
    mock_conn = _mock_connection()
    with patch("registration_svc.main.get_connection", return_value=mock_conn):
        resp = client.post("/waitlist/join", json={"phone_e164": "+15551234567"})
    assert resp.status_code == 200
    params = mock_conn.execute.call_args[0][1]
    assert params == ("+15551234567", None)


def test_duplicate_join_is_a_no_op_not_an_error(client):
    """ON CONFLICT DO NOTHING means the DB layer never errors on a repeat
    join — nothing here distinguishes a fresh insert from a conflict, by
    design (idempotent, and doesn't leak whether a number already joined)."""
    mock_conn = _mock_connection()
    with patch("registration_svc.main.get_connection", return_value=mock_conn):
        first = client.post("/waitlist/join", json={"phone_e164": "+15551234567"})
        second = client.post("/waitlist/join", json={"phone_e164": "+15551234567"})
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json() == {"status": "ok"}


@pytest.mark.parametrize(
    "bad_phone",
    ["5551234567", "+1", "not-a-phone", "+0123456789", ""],
)
def test_malformed_phone_rejected(client, bad_phone):
    with patch("registration_svc.main.get_connection") as mock_get_conn:
        resp = client.post("/waitlist/join", json={"phone_e164": bad_phone})
    assert resp.status_code == 422
    mock_get_conn.assert_not_called()


def test_blank_name_normalized_to_none(client):
    mock_conn = _mock_connection()
    with patch("registration_svc.main.get_connection", return_value=mock_conn):
        resp = client.post("/waitlist/join", json={"phone_e164": "+15551234567", "name": "   "})
    assert resp.status_code == 200
    params = mock_conn.execute.call_args[0][1]
    assert params == ("+15551234567", None)


def test_rate_limit_blocks_after_threshold(client):
    mock_conn = _mock_connection()
    with patch("registration_svc.main.get_connection", return_value=mock_conn):
        for i in range(5):
            resp = client.post("/waitlist/join", json={"phone_e164": f"+1555000{i:04d}"})
            assert resp.status_code == 200
        blocked = client.post("/waitlist/join", json={"phone_e164": "+15550009999"})
    assert blocked.status_code == 429


def test_rate_limit_is_per_ip(client):
    mock_conn = _mock_connection()
    with patch("registration_svc.main.get_connection", return_value=mock_conn):
        for i in range(5):
            client.post(
                "/waitlist/join",
                json={"phone_e164": f"+1555000{i:04d}"},
                headers={"X-Forwarded-For": "1.1.1.1"},
            )
        other_ip = client.post(
            "/waitlist/join",
            json={"phone_e164": "+15550009999"},
            headers={"X-Forwarded-For": "2.2.2.2"},
        )
    assert other_ip.status_code == 200


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
