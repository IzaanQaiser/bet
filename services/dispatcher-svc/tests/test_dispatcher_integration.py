"""Integration tests against the real dev Postgres (via the Cloud SQL Auth
Proxy) — docs/engineering/test-plan.md step 8. Calendar and Twilio are
mocked (a real Calendar-backed run is the required manual verification,
per the test plan — mocks can't validate real OAuth/API behavior); no
Pub/Sub emulator needed, dispatcher-svc doesn't publish in this step's
scope (the accept-path publish, agent-contracts.md §4.4, is deferred —
see main.py's module docstring).

Requires DB_USER, DB_HOST, DB_PORT set. Skipped automatically otherwise.
"""

import os
import uuid
from datetime import UTC, datetime, time, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from dispatcher_svc.capacity_engine import Event
from fastapi.testclient import TestClient
from obligation_engine_shared.db import get_connection

pytestmark = pytest.mark.skipif(
    "DB_USER" not in os.environ,
    reason="requires a live Cloud SQL Auth Proxy connection",
)

TZ_NAME = "America/Toronto"

# Trailing baseline days: a 5-hour meeting → booked=300min/day (matches
# capacity-engine.md §6's stated rolling_mean=300 exactly).
BUSY_TRAILING_DAY = [Event(start=time(9, 0), end=time(14, 0))]
# Forward candidate days: capacity-engine.md §6's exact worked-example
# calendar (a 3h meeting + a 30min meeting) → booked=210min, one 180min
# free block, matching fit_score=0.875 / revival_score≈0.633 for an
# 18-day-old deep 120min latent.
LIGHT_FORWARD_DAY = [
    Event(start=time(9, 0), end=time(12, 0)),
    Event(start=time(15, 0), end=time(15, 30)),
]


def _mock_events_by_range(start, end, tz_name, *, forward_events):
    """Returns BUSY_TRAILING_DAY for every day except `start`'s date
    forward — matches how the real fetch is called: once for the 14-day
    trailing range (ending today), once for the 7-day forward range
    (starting today). Distinguished by range length, not by which range
    it structurally is, since both calls share the boundary day (today)."""
    span = (end - start).days
    day_events = LIGHT_FORWARD_DAY if span == forward_events - 1 else BUSY_TRAILING_DAY
    return {start + timedelta(days=i): day_events for i in range((end - start).days + 1)}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DB_USER", os.environ["DB_USER"])
    monkeypatch.setenv("TWILIO_API_KEY_SECRET", "test-not-a-real-secret")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "test-client-secret")
    from dispatcher_svc.main import app

    return TestClient(app)


@pytest.fixture
def test_user():
    phone = f"+1555{uuid.uuid4().int % 10**7:07d}"
    with get_connection() as conn:
        row = conn.execute(
            "INSERT INTO users (phone_e164, timezone, google_refresh_token_ref) "
            "VALUES (%s, %s, %s) RETURNING id",
            (phone, TZ_NAME, "projects/p/secrets/user-refresh-token-fake/versions/latest"),
        ).fetchone()
        user_id = row[0]
        conn.commit()
    yield user_id, phone
    with get_connection() as conn:
        conn.execute("DELETE FROM suggestions WHERE user_id = %s", (str(user_id),))
        conn.execute("DELETE FROM capacity_snapshots WHERE user_id = %s", (str(user_id),))
        conn.execute(
            "DELETE FROM obligations WHERE item_id IN (SELECT id FROM items WHERE user_id = %s)",
            (str(user_id),),
        )
        conn.execute(
            "DELETE FROM latents WHERE item_id IN (SELECT id FROM items WHERE user_id = %s)",
            (str(user_id),),
        )
        conn.execute("DELETE FROM items WHERE user_id = %s", (str(user_id),))
        conn.execute("DELETE FROM users WHERE id = %s", (str(user_id),))
        conn.commit()


def _insert_committed_latent(user_id, created_at):
    with get_connection() as conn:
        row = conn.execute(
            """
            INSERT INTO items (user_id, raw_channel, ingested_at, created_at, state, type, title,
                                effort_minutes, focus_depth)
            VALUES (%s, 'sms', now(), %s, 'COMMITTED', 'latent',
                    'Rewrite the ingest pipeline in Rust', 120, 'deep')
            RETURNING id
            """,
            (str(user_id), created_at),
        ).fetchone()
        item_id = row[0]
        conn.execute("INSERT INTO latents (item_id) VALUES (%s)", (str(item_id),))
        conn.commit()
    return item_id


def _insert_committed_obligation(user_id, due_at):
    with get_connection() as conn:
        row = conn.execute(
            """
            INSERT INTO items (user_id, raw_channel, ingested_at, state, type, title,
                                effort_minutes, focus_depth)
            VALUES (%s, 'sms', now(), 'COMMITTED', 'obligation', 'Pay rent', 15, 'shallow')
            RETURNING id
            """,
            (str(user_id),),
        ).fetchone()
        item_id = row[0]
        conn.execute(
            "INSERT INTO obligations (item_id, due_at) VALUES (%s, %s)", (str(item_id), due_at)
        )
        conn.commit()
    return item_id


def _patched_calendar(mock_events_range):
    return (
        patch("dispatcher_svc.main.user_credentials", return_value=MagicMock()),
        patch("dispatcher_svc.main.AuthorizedSession", return_value=MagicMock()),
        patch("dispatcher_svc.main.fetch_events_for_range", side_effect=mock_events_range),
    )


def test_dispatch_run_produces_7_snapshots(client, test_user):
    user_id, _phone = test_user

    def mock_events_range(session, start, end, tz_name):
        return _mock_events_by_range(start, end, tz_name, forward_events=7)

    p1, p2, p3 = _patched_calendar(mock_events_range)
    with p1, p2, p3, patch("dispatcher_svc.main._send_sms"):
        resp = client.post("/dispatch")
    assert resp.status_code == 200

    with get_connection() as conn:
        count = conn.execute(
            "SELECT count(*) FROM capacity_snapshots WHERE user_id = %s", (str(user_id),)
        ).fetchone()[0]
    assert count == 7


def test_dispatch_run_sends_at_most_one_suggestion(client, test_user):
    user_id, phone = test_user
    today = datetime.now(UTC).astimezone(ZoneInfo(TZ_NAME)).date()
    old_enough = datetime.combine(today - timedelta(days=18), time(12, 0), tzinfo=UTC)

    # Two latents, both old enough and both deep/120min — both would clear
    # the threshold against the light forward day if scored independently.
    _insert_committed_latent(user_id, old_enough)
    _insert_committed_latent(user_id, old_enough)

    def mock_events_range(session, start, end, tz_name):
        return _mock_events_by_range(start, end, tz_name, forward_events=7)

    p1, p2, p3 = _patched_calendar(mock_events_range)
    with p1, p2, p3, patch("dispatcher_svc.main._send_sms") as mock_sms:
        resp = client.post("/dispatch")
    assert resp.status_code == 200

    suggestion_calls = [
        c for c in mock_sms.call_args_list if "Want it on the calendar?" in c.kwargs["body"]
    ]
    assert len(suggestion_calls) == 1

    with get_connection() as conn:
        count = conn.execute(
            "SELECT count(*) FROM suggestions WHERE user_id = %s", (str(user_id),)
        ).fetchone()[0]
    assert count == 1


def test_dispatch_run_sends_reminder_and_marks_idempotent(client, test_user):
    user_id, phone = test_user
    due_at = datetime.now(UTC) + timedelta(hours=2)
    item_id = _insert_committed_obligation(user_id, due_at)

    def mock_events_range(session, start, end, tz_name):
        return _mock_events_by_range(start, end, tz_name, forward_events=7)

    p1, p2, p3 = _patched_calendar(mock_events_range)
    with p1, p2, p3, patch("dispatcher_svc.main._send_sms") as mock_sms:
        client.post("/dispatch")

    reminder_calls = [c for c in mock_sms.call_args_list if "is due" in c.kwargs["body"]]
    assert len(reminder_calls) == 1

    with get_connection() as conn:
        reminder_sent_at = conn.execute(
            "SELECT reminder_sent_at FROM obligations WHERE item_id = %s", (str(item_id),)
        ).fetchone()[0]
    assert reminder_sent_at is not None

    # Second run: already reminded, must not send again.
    p1, p2, p3 = _patched_calendar(mock_events_range)
    with p1, p2, p3, patch("dispatcher_svc.main._send_sms") as mock_sms_second_run:
        client.post("/dispatch")
    reminder_calls_second = [
        c for c in mock_sms_second_run.call_args_list if "is due" in c.kwargs["body"]
    ]
    assert len(reminder_calls_second) == 0
