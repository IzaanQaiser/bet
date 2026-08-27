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
                                summary, effort_minutes)
            VALUES (%s, 'sms', now(), %s, 'COMMITTED', 'latent',
                    'Rewrite the ingest pipeline in Rust', 'Someday, no rush.', 120)
            RETURNING id
            """,
            (str(user_id), created_at),
        ).fetchone()
        item_id = row[0]
        conn.execute("INSERT INTO latents (item_id) VALUES (%s)", (str(item_id),))
        conn.commit()
    return item_id


def _insert_committed_obligation(user_id, due_at, effort_minutes=15, is_scheduled_event=False):
    """reminder_1_at/reminder_2_at mirror resolver-svc's own
    _compute_reminder_times — real production formula, not a test-only
    shortcut: one universal rule now, task or event, user-directed —
    30 minutes before due_at, and at due_at itself. effort_minutes no
    longer factors into reminder timing at all (still stored on items,
    purely for Calendar event sizing)."""
    reminder_1_at = due_at - timedelta(minutes=30)
    reminder_2_at = due_at
    with get_connection() as conn:
        row = conn.execute(
            """
            INSERT INTO items (user_id, raw_channel, ingested_at, state, type, title,
                                effort_minutes, is_scheduled_event)
            VALUES (%s, 'sms', now(), 'COMMITTED', 'obligation', 'Pay rent', %s, %s)
            RETURNING id
            """,
            (str(user_id), effort_minutes, is_scheduled_event),
        ).fetchone()
        item_id = row[0]
        conn.execute(
            "INSERT INTO obligations (item_id, due_at, reminder_1_at, reminder_2_at) "
            "VALUES (%s, %s, %s, %s)",
            (str(item_id), due_at, reminder_1_at, reminder_2_at),
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


def test_dispatch_run_recomputes_next_fit_and_upserts_placeholder(client, test_user):
    """ADR 0009 replaced the old revival_score/'at most one per run'
    engine — the sweep now recomputes every non-dormant committed
    latent's next_fit_start unconditionally and, via the mocked
    committer_client, would upsert a real [idea]-tagged placeholder for
    each one that got a real slot. No SMS is sent by /dispatch itself
    anymore — that only happens when /latents/{item_id}/fire's own Cloud
    Task actually arrives."""
    user_id, _phone = test_user
    today = datetime.now(UTC).astimezone(ZoneInfo(TZ_NAME)).date()
    old_enough = datetime.combine(today - timedelta(days=18), time(12, 0), tzinfo=UTC)
    item_id = _insert_committed_latent(user_id, old_enough)

    def mock_events_range(session, start, end, tz_name):
        return _mock_events_by_range(start, end, tz_name, forward_events=7)

    def upsert_side_effect(item_uuid, *_args, **_kwargs):
        # /dispatch iterates every real linked user in the shared dev DB,
        # not just this test's — a real bug, found running this test for
        # real, wrote this mock's fake "evt-1" into another real user's
        # actual latents row. Only fake-succeed for this test's own item;
        # raising for every other item makes _recompute_and_reschedule's
        # own except-and-skip path leave every other user's real row
        # completely untouched (verified: no DB write happens on that
        # branch), rather than trying to filter after the fact.
        if item_uuid == item_id:
            return "evt-1"
        raise RuntimeError("not this test's item — refuse to touch real data")

    p1, p2, p3 = _patched_calendar(mock_events_range)
    with (
        p1, p2, p3,
        patch("dispatcher_svc.main._send_sms") as mock_sms,
        patch("dispatcher_svc.main.committer_client") as mock_committer,
        patch("dispatcher_svc.main.tasks_client") as mock_tasks,
    ):
        mock_committer.upsert_placeholder.side_effect = upsert_side_effect
        resp = client.post("/dispatch")
    assert resp.status_code == 200
    mock_sms.assert_not_called()
    this_items_upserts = [
        c for c in mock_committer.upsert_placeholder.call_args_list if c.args[0] == item_id
    ]
    assert len(this_items_upserts) == 1
    this_items_tasks = [
        c for c in mock_tasks.enqueue_fire_task.call_args_list if c.args[0] == item_id
    ]
    assert len(this_items_tasks) == 1

    with get_connection() as conn:
        next_fit_start, placeholder_event_id = conn.execute(
            "SELECT next_fit_start, placeholder_event_id FROM latents WHERE item_id = %s",
            (str(item_id),),
        ).fetchone()
    assert next_fit_start is not None
    assert placeholder_event_id == "evt-1"


def test_dispatch_run_sends_reminder_and_marks_idempotent(client, test_user):
    """due_at already 2 minutes in the past: both the flat 30-min-before
    and the at-due-time reminder have already passed, so a single run
    fires both — real DB-backed idempotency for each slot independently,
    not just one shared reminder_sent_at."""
    user_id, phone = test_user
    due_at = datetime.now(UTC) - timedelta(minutes=2)
    item_id = _insert_committed_obligation(user_id, due_at)

    def mock_events_range(session, start, end, tz_name):
        return _mock_events_by_range(start, end, tz_name, forward_events=7)

    p1, p2, p3 = _patched_calendar(mock_events_range)
    with p1, p2, p3, patch("dispatcher_svc.main._send_sms") as mock_sms:
        client.post("/dispatch")

    reminder_calls = [c for c in mock_sms.call_args_list if "⏰" in c.kwargs["body"]]
    assert len(reminder_calls) == 2

    with get_connection() as conn:
        reminder_1_sent_at, reminder_2_sent_at = conn.execute(
            "SELECT reminder_1_sent_at, reminder_2_sent_at FROM obligations WHERE item_id = %s",
            (str(item_id),),
        ).fetchone()
    assert reminder_1_sent_at is not None
    assert reminder_2_sent_at is not None

    # Second run: already reminded (both slots), must not send again.
    p1, p2, p3 = _patched_calendar(mock_events_range)
    with p1, p2, p3, patch("dispatcher_svc.main._send_sms") as mock_sms_second_run:
        client.post("/dispatch")
    reminder_calls_second = [
        c for c in mock_sms_second_run.call_args_list if "⏰" in c.kwargs["body"]
    ]
    assert len(reminder_calls_second) == 0


def test_dispatch_reminders_fires_a_same_day_event_start_with_no_calendar_calls(client, test_user):
    """Real bug, reported live: a same-day 9pm meeting never got a
    reminder text at all — /dispatch only runs twice a day (7am/1pm), so
    the only run that could have caught a 9pm reminder was the *next*
    day's 7am one, hours after the meeting. /dispatch/reminders is the
    infrequent safety-net fallback (the precise path is committer-svc's
    Cloud Task -> /dispatch/reminders/fire, tested separately below); this
    hits the real endpoint with NO Calendar mocking at all — if it touched
    the Calendar API like /dispatch does, this test would fail on a real
    network/credential error, which is itself proof it doesn't."""
    user_id, phone = test_user
    due_at = datetime.now(UTC) + timedelta(minutes=2)
    item_id = _insert_committed_obligation(
        user_id, due_at, effort_minutes=30, is_scheduled_event=True
    )

    with patch("dispatcher_svc.main._send_sms") as mock_sms:
        resp = client.post("/dispatch/reminders")

    assert resp.status_code == 200
    reminder_calls = [c for c in mock_sms.call_args_list if "⏰" in c.kwargs["body"]]
    assert len(reminder_calls) == 1
    assert "starts" in reminder_calls[0].kwargs["body"]

    with get_connection() as conn:
        reminder_1_sent_at = conn.execute(
            "SELECT reminder_1_sent_at FROM obligations WHERE item_id = %s", (str(item_id),)
        ).fetchone()[0]
    assert reminder_1_sent_at is not None


def test_dispatch_reminders_fire_sends_the_one_named_slot(client, test_user):
    """The actual precise mechanism: committer-svc's Cloud Task hits this
    directly with a specific item_id/slot, not a batch scan. Real DB,
    real endpoint, no Calendar mocking (proof it never touches Calendar,
    same reasoning as the poll-fallback test above)."""
    user_id, phone = test_user
    due_at = datetime.now(UTC) + timedelta(hours=1)
    item_id = _insert_committed_obligation(user_id, due_at, effort_minutes=60)

    with patch("dispatcher_svc.main._send_sms") as mock_sms:
        resp = client.post("/dispatch/reminders/fire", json={"item_id": str(item_id), "slot": 1})

    assert resp.status_code == 200
    assert resp.json()["status"] == "sent"
    mock_sms.assert_called_once()
    assert "⏰" in mock_sms.call_args.kwargs["body"]

    with get_connection() as conn:
        reminder_1_sent_at, reminder_2_sent_at = conn.execute(
            "SELECT reminder_1_sent_at, reminder_2_sent_at FROM obligations WHERE item_id = %s",
            (str(item_id),),
        ).fetchone()
    assert reminder_1_sent_at is not None
    assert reminder_2_sent_at is None  # only the named slot fired


def test_dispatch_reminders_fire_skips_stale_scheduled_for(client, test_user):
    """Real bug, found designing calendar-sync-svc's two-way sync: without
    this check, a due_at change would leave the *old* task still armed
    for the *old* instant — it would fire, send at the wrong time, and
    mark reminder_1_sent_at, silently blocking the correct later task."""
    user_id, phone = test_user
    due_at = datetime.now(UTC) + timedelta(hours=1)
    item_id = _insert_committed_obligation(user_id, due_at, effort_minutes=60)
    wrong_scheduled_for = (due_at - timedelta(hours=5)).isoformat()  # not the real reminder_1_at

    with patch("dispatcher_svc.main._send_sms") as mock_sms:
        resp = client.post(
            "/dispatch/reminders/fire",
            json={"item_id": str(item_id), "slot": 1, "scheduled_for": wrong_scheduled_for},
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "stale_task_skipped"
    mock_sms.assert_not_called()

    with get_connection() as conn:
        reminder_1_sent_at = conn.execute(
            "SELECT reminder_1_sent_at FROM obligations WHERE item_id = %s", (str(item_id),)
        ).fetchone()[0]
    assert reminder_1_sent_at is None  # not marked sent — the real task can still fire correctly


def test_dispatch_reminders_fire_is_idempotent_on_redelivery(client, test_user):
    """Cloud Tasks delivers at-least-once — a redelivered task for an
    already-sent slot must be a real no-op against the DB, not a second
    text."""
    user_id, phone = test_user
    due_at = datetime.now(UTC) + timedelta(hours=1)
    item_id = _insert_committed_obligation(user_id, due_at, effort_minutes=60)

    with patch("dispatcher_svc.main._send_sms"):
        client.post("/dispatch/reminders/fire", json={"item_id": str(item_id), "slot": 1})

    with patch("dispatcher_svc.main._send_sms") as mock_sms_second:
        resp = client.post("/dispatch/reminders/fire", json={"item_id": str(item_id), "slot": 1})

    assert resp.status_code == 200
    assert resp.json()["status"] == "already_sent"
    mock_sms_second.assert_not_called()


def test_dispatch_reminders_fire_skips_a_deleted_item(client, test_user):
    """A task enqueued at commit time can fire long after the user
    deletes the item — the fire endpoint must check real current state,
    not just blindly trust the task existed."""
    user_id, phone = test_user
    due_at = datetime.now(UTC) + timedelta(hours=1)
    item_id = _insert_committed_obligation(user_id, due_at, effort_minutes=60)
    with get_connection() as conn:
        conn.execute("UPDATE items SET state = 'CANCELLED' WHERE id = %s", (str(item_id),))
        conn.commit()

    with patch("dispatcher_svc.main._send_sms") as mock_sms:
        resp = client.post("/dispatch/reminders/fire", json={"item_id": str(item_id), "slot": 1})

    assert resp.status_code == 200
    assert resp.json()["status"] == "skipped"
    mock_sms.assert_not_called()


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

    # One consistent local "tomorrow" for scheduled_for, the mocked
    # Calendar events, and the final assertion — computed once so this
    # doesn't flake near UTC/local-date boundaries.
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).astimezone(ZoneInfo(TZ_NAME)).date()
    scheduled_for = datetime.combine(tomorrow, time(12, 0), tzinfo=ZoneInfo(TZ_NAME))

    with get_connection() as conn:
        # snapshot_id is nullable (ADR 0009, migrations/0020) — a
        # fire-time suggestion has no capacity_snapshots row to attach to.
        conn.execute(
            "INSERT INTO suggestions (item_id, user_id, snapshot_id, scheduled_for) "
            "VALUES (%s, %s, NULL, %s)",
            (str(item_id), str(user_id), scheduled_for),
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
