"""Integration tests against the real dev Postgres (via the Cloud SQL Auth
Proxy) — docs/engineering/test-plan.md steps 8 and 14. Calendar and
Twilio are mocked throughout (a real Calendar-backed run is the required
manual verification, per the test plan — mocks can't validate real
OAuth/API behavior). Step 14's accept-path test also needs the real
Pub/Sub emulator, since it's the first thing in this file that actually
publishes.

Requires DB_USER, DB_HOST, DB_PORT set. Skipped automatically otherwise.
"""

import json
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
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest0000000000000000000000000")
    monkeypatch.setenv("TWILIO_API_KEY_SID", "SKtest0000000000000000000000000")
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
    # summary included from step 14 on — a real committed latent always
    # has one (extractor-svc's schema requires it, never optional), and
    # the accept path (unlike step 8's suggestion template) actually
    # needs it to build a real ConfirmedItemMessage.
    with get_connection() as conn:
        row = conn.execute(
            """
            INSERT INTO items (user_id, raw_channel, ingested_at, created_at, state, type, title,
                                summary, effort_minutes, focus_depth)
            VALUES (%s, 'sms', now(), %s, 'COMMITTED', 'latent',
                    'Rewrite the ingest pipeline in Rust', 'Someday, no rush.', 120, 'deep')
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


# --- step 14: accept-path full cycle --------------------------------------


@pytest.fixture
def confirmed_subscription():
    from google.cloud import pubsub_v1

    subscriber = pubsub_v1.SubscriberClient()
    project_id = os.environ["GCP_PROJECT_ID"]
    topic_path = subscriber.topic_path(project_id, "items-confirmed")
    sub_name = f"test-dispatcher-reply-{uuid.uuid4().hex[:8]}"
    sub_path = subscriber.subscription_path(project_id, sub_name)
    subscriber.create_subscription(name=sub_path, topic=topic_path)
    yield subscriber, sub_path
    subscriber.delete_subscription(subscription=sub_path)


def test_accept_path_full_cycle(client, test_user, confirmed_subscription):
    """SURFACED -> Y -> items.confirmed published with type flipped to
    obligation and due_at computed as the real current block start,
    capped at the block length (state-machine.md §2.3). committer-svc
    consuming this correctly is covered by its own test suite — this
    test's boundary is dispatcher-svc's real publish."""
    user_id, _phone = test_user
    subscriber, sub_path = confirmed_subscription
    item_id = _insert_committed_latent(
        user_id, datetime.now(UTC) - timedelta(days=18, hours=1)
    )

    # One consistent local "tomorrow" for the snapshot row, the mocked
    # Calendar events, and the final assertion — computed once so this
    # doesn't flake near UTC/local-date boundaries.
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).astimezone(ZoneInfo(TZ_NAME)).date()

    with get_connection() as conn:
        snapshot_row = conn.execute(
            """
            INSERT INTO capacity_snapshots
                (user_id, date, free_minutes, largest_contiguous_block,
                 fragmentation_index, load_delta)
            VALUES (%s, %s, 180, 180, 0.0, -0.3)
            RETURNING id
            """,
            (str(user_id), tomorrow),
        ).fetchone()
        snapshot_id = snapshot_row[0]
        conn.execute(
            "INSERT INTO suggestions (item_id, user_id, snapshot_id) VALUES (%s, %s, %s)",
            (str(item_id), str(user_id), snapshot_id),
        )
        conn.commit()

    # A single 3h free block from 12:00-15:00 — matches capacity-engine.md
    # §6's worked example exactly (a 120min deep item fits under it).
    events_by_day = {
        tomorrow: [
            Event(start=time(9, 0), end=time(12, 0)),
            Event(start=time(15, 0), end=time(18, 0)),
        ]
    }

    with (
        patch("dispatcher_svc.main.user_credentials", return_value=MagicMock()),
        patch("dispatcher_svc.main.AuthorizedSession", return_value=MagicMock()),
        patch("dispatcher_svc.main.fetch_events_for_range", return_value=events_by_day),
        patch("dispatcher_svc.main._send_sms") as mock_sms,
    ):
        resp = client.post(
            "/reply", json={"user_id": str(user_id), "item_id": str(item_id), "text": "y"}
        )

    assert resp.status_code == 200
    assert resp.json() == {"status": "accepted", "item_id": str(item_id)}
    mock_sms.assert_called_once()

    with get_connection() as conn:
        item_type = conn.execute(
            "SELECT type FROM items WHERE id = %s", (str(item_id),)
        ).fetchone()[0]
        outcome = conn.execute(
            "SELECT outcome FROM suggestions WHERE item_id = %s", (str(item_id),)
        ).fetchone()[0]
    # items.type only flips once committer-svc consumes the message and
    # writes it (this step's own "Resolved gap" fix) — not yet at this
    # point, since nothing here runs committer-svc. Still 'latent' here
    # is correct; the outcome column is dispatcher-svc's own write.
    assert item_type == "latent"
    assert outcome == "accepted"

    pulled = subscriber.pull(subscription=sub_path, max_messages=1, timeout=15)
    assert len(pulled.received_messages) == 1
    confirmed_data = json.loads(pulled.received_messages[0].message.data)
    subscriber.acknowledge(
        subscription=sub_path, ack_ids=[m.ack_id for m in pulled.received_messages]
    )
    assert confirmed_data["item_id"] == str(item_id)
    assert confirmed_data["type"] == "obligation"
    assert confirmed_data["action_type"] == "calendar"
    assert confirmed_data["effort_minutes"] == 120  # fits under the 180min block untouched
    assert confirmed_data["due_at"].startswith(tomorrow.isoformat() + "T12:00:00")
