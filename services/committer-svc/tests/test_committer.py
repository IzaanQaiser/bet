"""Unit tests — DB, Secret Manager, and the Calendar API call all mocked,
per docs/engineering/test-plan.md step 6."""

import base64
from datetime import datetime
from unittest.mock import MagicMock, patch
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from obligation_engine_shared.schemas import ConfirmedItemMessage


def _push_envelope(message) -> dict:
    return {"message": {"data": base64.b64encode(message.model_dump_json().encode()).decode()}}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "test-client-secret")
    from committer_svc.main import app

    return TestClient(app)


def _mock_connection(*, already_committed=False, user_row=None):
    """already_committed feeds the idempotency guard's _already_committed()
    check (SELECT 1 FROM obligations/latents WHERE item_id=...) at the top
    of /pubsub/push — False (the default) lets every pre-existing test
    reach the real commit logic unchanged; tests exercising the guard
    itself override it. Keyed on the target table, not items.state — see
    _already_committed()'s own docstring for why (a real bug found
    verifying step 14's accept path)."""

    def execute_side_effect(sql, params=None):
        result = MagicMock()
        if "FROM obligations" in sql or "FROM latents" in sql:
            result.fetchone.return_value = (1,) if already_committed else None
        elif "FROM users" in sql:
            result.fetchone.return_value = user_row
        else:
            result.fetchone.return_value = None
        return result

    conn = MagicMock()
    conn.execute.side_effect = execute_side_effect
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn


def _confirmed_message(**overrides):
    defaults = dict(
        item_id=uuid4(),
        user_id=uuid4(),
        type="obligation",
        title="Pay rent",
        summary="Pay rent by Friday.",
        due_at=datetime(2026, 8, 28, 14, 0),
        effort_minutes=15,
        action_type="calendar",
        email_draft=None,
    )
    defaults.update(overrides)
    return ConfirmedItemMessage(**defaults)


def _mock_secret_client(refresh_token="refresh-token-value"):
    secret_client = MagicMock()
    secret_client.access_secret_version.return_value.payload.data = refresh_token.encode()
    return secret_client


def test_obligation_branch_calls_calendar_write(client):
    confirmed = _confirmed_message()
    conn = _mock_connection(
        user_row=("projects/p/secrets/user-refresh-token-x/versions/latest", "America/Los_Angeles")
    )
    calendar_response = MagicMock()
    calendar_response.json.return_value = {"id": "gcal-event-123"}

    with (
        patch("committer_svc.main.get_connection", return_value=conn),
        patch("committer_svc.main._secret_client", return_value=_mock_secret_client()),
        patch("committer_svc.main.AuthorizedSession") as mock_session_cls,
    ):
        mock_session_cls.return_value.post.return_value = calendar_response
        resp = client.post("/pubsub/push", json=_push_envelope(confirmed))

    assert resp.status_code == 200
    assert resp.json() == {"status": "committed", "item_id": str(confirmed.item_id)}

    mock_session_cls.return_value.post.assert_called_once()
    _, kwargs = mock_session_cls.return_value.post.call_args
    assert kwargs["json"]["summary"] == "Pay rent"
    # v1 simplification: the 30-min-before lead that used to be a second
    # SMS now lives only as the Calendar event's own native reminder.
    assert kwargs["json"]["reminders"] == {
        "useDefault": False,
        "overrides": [{"method": "popup", "minutes": 30}],
    }

    # call 0 is the idempotency guard's items.state check; 1 is the SELECT
    # in _user_credentials; 2 is the ADR 0009 placeholder-lookup SELECT
    # (no placeholder here, so it falls straight to a fresh Calendar
    # POST); 3 is the obligations INSERT; 4 is the items UPDATE.
    insert_sql, insert_params = conn.execute.call_args_list[3][0]
    assert "INSERT INTO obligations" in insert_sql
    assert insert_params[2] == "gcal-event-123"  # calendar_event_id

    update_sql, update_params = conn.execute.call_args_list[4][0]
    assert "state = 'COMMITTED'" in update_sql
    assert update_params == (confirmed.type, str(confirmed.item_id))


def test_calendar_branch_localizes_due_at_before_insert(client):
    """Real bug, found live: a naive due_at (Gemini reasons in local terms,
    no UTC offset attached, per agent-contracts.md §1) used to go straight
    into the obligations INSERT unchanged — Postgres then silently
    interpreted it as UTC on the timestamptz column, storing "9pm local"
    as if it meant "9pm UTC". A real committed 9pm meeting showed as 5pm
    on the dashboard (America/Toronto is UTC-4 in August) even though the
    real Calendar event — a separate write, already correctly localized —
    was fine. due_at must carry the user's real timezone before the INSERT,
    same as it already did for the Calendar API payload."""
    confirmed = _confirmed_message(due_at=datetime(2026, 8, 28, 14, 0))
    conn = _mock_connection(
        user_row=("projects/p/secrets/user-refresh-token-x/versions/latest", "America/Los_Angeles")
    )
    calendar_response = MagicMock()
    calendar_response.json.return_value = {"id": "gcal-event-123"}

    with (
        patch("committer_svc.main.get_connection", return_value=conn),
        patch("committer_svc.main._secret_client", return_value=_mock_secret_client()),
        patch("committer_svc.main.AuthorizedSession") as mock_session_cls,
    ):
        mock_session_cls.return_value.post.return_value = calendar_response
        resp = client.post("/pubsub/push", json=_push_envelope(confirmed))

    assert resp.status_code == 200
    _insert_sql, insert_params = conn.execute.call_args_list[3][0]
    assert insert_params[1] == datetime(2026, 8, 28, 14, 0, tzinfo=ZoneInfo("America/Los_Angeles"))


def test_calendar_branch_persists_reminder_time(client):
    confirmed = _confirmed_message(reminder_at=datetime(2026, 8, 28, 14, 0))
    conn = _mock_connection(
        user_row=("projects/p/secrets/user-refresh-token-x/versions/latest", "America/Los_Angeles")
    )
    calendar_response = MagicMock()
    calendar_response.json.return_value = {"id": "gcal-event-123"}

    with (
        patch("committer_svc.main.get_connection", return_value=conn),
        patch("committer_svc.main._secret_client", return_value=_mock_secret_client()),
        patch("committer_svc.main.AuthorizedSession") as mock_session_cls,
    ):
        mock_session_cls.return_value.post.return_value = calendar_response
        resp = client.post("/pubsub/push", json=_push_envelope(confirmed))

    assert resp.status_code == 200
    insert_sql, insert_params = conn.execute.call_args_list[3][0]
    assert "reminder_at" in insert_sql
    # Real bug, found live: this used to be inserted still-naive, which
    # Postgres then silently interpreted as UTC on a timestamptz column —
    # a real committed item's reminder time (and due_at) landed hours off
    # from what the user actually said. Must be tz-aware before the INSERT.
    tz = ZoneInfo("America/Los_Angeles")
    assert insert_params[4] == datetime(2026, 8, 28, 14, 0, tzinfo=tz)


def test_calendar_branch_enqueues_a_reminder_task(client):
    """Real gap, found live: dispatcher-svc's own /dispatch only runs
    twice a day, too coarse for a same-day reminder. committer-svc must
    schedule a precise Cloud Task at the exact (already-localized)
    reminder instant, instead of relying on polling."""
    confirmed = _confirmed_message(reminder_at=datetime(2026, 8, 28, 14, 0))
    conn = _mock_connection(
        user_row=("projects/p/secrets/user-refresh-token-x/versions/latest", "America/Los_Angeles")
    )
    calendar_response = MagicMock()
    calendar_response.json.return_value = {"id": "gcal-event-123"}

    with (
        patch("committer_svc.main.get_connection", return_value=conn),
        patch("committer_svc.main._secret_client", return_value=_mock_secret_client()),
        patch("committer_svc.main.AuthorizedSession") as mock_session_cls,
        patch("committer_svc.main._enqueue_reminder_task") as mock_enqueue,
    ):
        mock_session_cls.return_value.post.return_value = calendar_response
        resp = client.post("/pubsub/push", json=_push_envelope(confirmed))

    assert resp.status_code == 200
    tz = ZoneInfo("America/Los_Angeles")
    mock_enqueue.assert_called_once_with(confirmed.item_id, datetime(2026, 8, 28, 14, 0, tzinfo=tz))


def test_calendar_branch_skips_a_reminder_already_overdue_at_commit(client):
    """Real bug, found live: confirming a meeting 2 minutes before it
    started meant reminder_at was already in the past by commit time.
    Cloud Tasks doesn't hold a past schedule_time, it fires immediately,
    which would land the reminder at the same instant as confirmation.

    Second real bug, found live right after: skipping the enqueue alone
    left reminder_sent_at NULL forever, so /dispatch/reminders' own
    fallback poll (unable to tell "genuinely missed" apart from
    "deliberately never scheduled") fired it late anyway. The skipped
    reminder must be marked sent at insert time so the fallback's own
    IS NULL idempotency check leaves it alone."""
    from datetime import UTC, timedelta

    now = datetime.now(UTC)
    confirmed = _confirmed_message(reminder_at=now - timedelta(minutes=1))  # already overdue
    conn = _mock_connection(
        user_row=("projects/p/secrets/user-refresh-token-x/versions/latest", "America/Los_Angeles")
    )
    calendar_response = MagicMock()
    calendar_response.json.return_value = {"id": "gcal-event-123"}

    with (
        patch("committer_svc.main.get_connection", return_value=conn),
        patch("committer_svc.main._secret_client", return_value=_mock_secret_client()),
        patch("committer_svc.main.AuthorizedSession") as mock_session_cls,
        patch("committer_svc.main._enqueue_reminder_task") as mock_enqueue,
    ):
        mock_session_cls.return_value.post.return_value = calendar_response
        resp = client.post("/pubsub/push", json=_push_envelope(confirmed))

    assert resp.status_code == 200
    mock_enqueue.assert_not_called()

    insert_sql, insert_params = conn.execute.call_args_list[3][0]
    assert "reminder_sent_at" in insert_sql
    assert insert_params[5] is not None  # marked closed out immediately, not left pending


def test_calendar_branch_no_reminder_task_when_time_absent(client):
    """A latent-turned-obligation or any commit with no reminder time
    (null) enqueues nothing — nothing to schedule."""
    confirmed = _confirmed_message()  # reminder_at defaults to None
    conn = _mock_connection(
        user_row=("projects/p/secrets/user-refresh-token-x/versions/latest", "America/Los_Angeles")
    )
    calendar_response = MagicMock()
    calendar_response.json.return_value = {"id": "gcal-event-123"}

    with (
        patch("committer_svc.main.get_connection", return_value=conn),
        patch("committer_svc.main._secret_client", return_value=_mock_secret_client()),
        patch("committer_svc.main.AuthorizedSession") as mock_session_cls,
        patch("committer_svc.main._enqueue_reminder_task") as mock_enqueue,
    ):
        mock_session_cls.return_value.post.return_value = calendar_response
        resp = client.post("/pubsub/push", json=_push_envelope(confirmed))

    assert resp.status_code == 200
    mock_enqueue.assert_not_called()


def test_latent_branch_does_not_call_calendar(client):
    confirmed = _confirmed_message(
        type="latent", due_at=None, action_type=None, title="Learn pottery", summary="Someday."
    )
    conn = _mock_connection()

    with (
        patch("committer_svc.main.get_connection", return_value=conn),
        patch("committer_svc.main.AuthorizedSession") as mock_session_cls,
        patch("committer_svc.main._enqueue_next_fit_task") as mock_enqueue_next_fit,
    ):
        resp = client.post("/pubsub/push", json=_push_envelope(confirmed))

    assert resp.status_code == 200
    mock_session_cls.assert_not_called()
    mock_enqueue_next_fit.assert_called_once_with(confirmed.item_id)

    # call 0 is the idempotency guard's items.state check.
    insert_sql = conn.execute.call_args_list[1][0][0]
    assert "INSERT INTO latents" in insert_sql
    update_sql = conn.execute.call_args_list[2][0][0]
    assert "state = 'COMMITTED'" in update_sql


def test_enqueue_next_fit_task_targets_dispatcher_immediately(monkeypatch):
    """No schedule_time set (unlike _enqueue_reminder_task) — this fires as
    soon as Cloud Tasks can dispatch it, not at some future instant."""
    from committer_svc.main import _enqueue_next_fit_task

    monkeypatch.setenv("GCP_PROJECT_ID", "obligation-engine-hack")
    monkeypatch.setenv("DISPATCHER_SVC_URL", "https://dispatcher-svc.example.run.app")
    item_id = uuid4()

    with patch("committer_svc.main.tasks_v2.CloudTasksClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.queue_path.return_value = "projects/p/locations/us-central1/queues/reminders"
        _enqueue_next_fit_task(item_id)

    mock_client.create_task.assert_called_once()
    task = mock_client.create_task.call_args.kwargs["task"]
    assert task["http_request"]["url"] == (
        f"https://dispatcher-svc.example.run.app/latents/{item_id}/next-fit"
    )
    assert task["http_request"]["oidc_token"]["service_account_email"] == (
        "sa-dispatcher@obligation-engine-hack.iam.gserviceaccount.com"
    )
    assert "schedule_time" not in task


def test_enqueue_next_fit_task_swallows_failure(monkeypatch):
    from committer_svc.main import _enqueue_next_fit_task

    monkeypatch.setenv("GCP_PROJECT_ID", "obligation-engine-hack")
    monkeypatch.setenv("DISPATCHER_SVC_URL", "https://dispatcher-svc.example.run.app")

    with patch("committer_svc.main.tasks_v2.CloudTasksClient", side_effect=RuntimeError("boom")):
        _enqueue_next_fit_task(uuid4())  # does not raise


def test_calendar_failure_does_not_mark_committed(client):
    confirmed = _confirmed_message()
    conn = _mock_connection(
        user_row=("projects/p/secrets/user-refresh-token-x/versions/latest", "America/Los_Angeles")
    )

    with (
        patch("committer_svc.main.get_connection", return_value=conn),
        patch("committer_svc.main._secret_client", return_value=_mock_secret_client()),
        patch("committer_svc.main.AuthorizedSession") as mock_session_cls,
    ):
        mock_session_cls.return_value.post.side_effect = RuntimeError("Calendar API 500")
        resp = client.post("/pubsub/push", json=_push_envelope(confirmed))

    assert resp.status_code == 500
    # The idempotency guard's state check, the credentials SELECT, and
    # the ADR 0009 placeholder-lookup SELECT all happened — the calendar
    # call raised before the obligations INSERT / items UPDATE were ever
    # reached.
    assert conn.execute.call_count == 3
    assert "SELECT" in conn.execute.call_args_list[1][0][0]
    assert "SELECT" in conn.execute.call_args_list[2][0][0]


# --- ADR 0009: placeholder promotion + PUT/DELETE endpoints ------------


def _mock_promotion_connection(*, placeholder_event_id, user_row):
    """A resurfaced latent's accept path: _already_committed's own
    'FROM obligations' check must report not-yet-committed, while the
    ADR 0009 placeholder-lookup SELECT (also 'FROM latents', like
    _already_committed's own latent-branch check) must return a real
    existing placeholder id — distinct enough intents that _mock_connection's
    single already_committed flag can't express both at once."""

    def execute_side_effect(sql, params=None):
        result = MagicMock()
        if "FROM obligations" in sql:
            result.fetchone.return_value = None  # not already committed
        elif "SELECT placeholder_event_id FROM latents" in sql:
            result.fetchone.return_value = (placeholder_event_id,)
        elif "FROM users" in sql:
            result.fetchone.return_value = user_row
        else:
            result.fetchone.return_value = None
        return result

    conn = MagicMock()
    conn.execute.side_effect = execute_side_effect
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn


def test_accept_promotes_existing_placeholder_in_place(client):
    """A resurfaced latent already has a real [idea]-tagged placeholder
    on the calendar (dispatcher-svc's accept path always confirms at the
    placeholder's own slot) — promote that same event via PATCH instead
    of POSTing a duplicate, and clear the now-stale placeholder columns."""
    confirmed = _confirmed_message()
    conn = _mock_promotion_connection(
        placeholder_event_id="evt-placeholder",
        user_row=("projects/p/secrets/user-refresh-token-x/versions/latest", "America/Los_Angeles"),
    )
    patch_response = MagicMock()
    patch_response.status_code = 200
    patch_response.json.return_value = {"id": "evt-placeholder"}

    with (
        patch("committer_svc.main.get_connection", return_value=conn),
        patch("committer_svc.main._secret_client", return_value=_mock_secret_client()),
        patch("committer_svc.main.AuthorizedSession") as mock_session_cls,
    ):
        mock_session_cls.return_value.patch.return_value = patch_response
        resp = client.post("/pubsub/push", json=_push_envelope(confirmed))

    assert resp.status_code == 200
    mock_session_cls.return_value.patch.assert_called_once()
    mock_session_cls.return_value.post.assert_not_called()  # no duplicate event

    clear_calls = [
        c for c in conn.execute.call_args_list
        if "UPDATE latents" in c.args[0] and "placeholder_event_id = NULL" in c.args[0]
    ]
    assert len(clear_calls) == 1


def test_accept_falls_back_to_create_when_placeholder_was_deleted(client):
    """The user (or some other process) deleted the placeholder event by
    hand — the promotion PATCH 404s, so this must fall back to a fresh
    POST rather than failing the whole commit."""
    confirmed = _confirmed_message()
    conn = _mock_promotion_connection(
        placeholder_event_id="evt-gone",
        user_row=("projects/p/secrets/user-refresh-token-x/versions/latest", "America/Los_Angeles"),
    )
    patch_response = MagicMock()
    patch_response.status_code = 404
    post_response = MagicMock()
    post_response.json.return_value = {"id": "gcal-event-fresh"}

    with (
        patch("committer_svc.main.get_connection", return_value=conn),
        patch("committer_svc.main._secret_client", return_value=_mock_secret_client()),
        patch("committer_svc.main.AuthorizedSession") as mock_session_cls,
    ):
        mock_session_cls.return_value.patch.return_value = patch_response
        mock_session_cls.return_value.post.return_value = post_response
        resp = client.post("/pubsub/push", json=_push_envelope(confirmed))

    assert resp.status_code == 200
    mock_session_cls.return_value.post.assert_called_once()


def test_upsert_placeholder_creates_when_no_existing_event(client):
    with (
        patch(
            "committer_svc.main._user_credentials",
            return_value=(MagicMock(), "America/Vancouver"),
        ),
        patch("committer_svc.main.AuthorizedSession") as mock_session_cls,
    ):
        mock_session_cls.return_value.post.return_value.json.return_value = {"id": "new-evt"}
        resp = client.put(
            f"/latents/{uuid4()}/placeholder",
            json={
                "user_id": str(uuid4()), "title": "Nerf gun turret",
                "start": "2026-08-27T09:00:00", "effort_minutes": 240,
                "existing_event_id": None,
            },
        )

    assert resp.status_code == 200
    assert resp.json() == {"event_id": "new-evt"}
    mock_session_cls.return_value.post.assert_called_once()
    sent_json = mock_session_cls.return_value.post.call_args.kwargs["json"]
    assert sent_json["summary"] == "[idea] Nerf gun turret"
    # Idea placeholders get the same native Calendar reminder as a real
    # obligation event, "all events and ideas" per the v1 ask.
    assert sent_json["reminders"] == {
        "useDefault": False,
        "overrides": [{"method": "popup", "minutes": 30}],
    }


def test_upsert_placeholder_moves_when_existing_event_given(client):
    with (
        patch(
            "committer_svc.main._user_credentials",
            return_value=(MagicMock(), "America/Vancouver"),
        ),
        patch("committer_svc.main.AuthorizedSession") as mock_session_cls,
    ):
        patch_response = MagicMock()
        patch_response.status_code = 200
        patch_response.json.return_value = {"id": "same-evt"}
        mock_session_cls.return_value.patch.return_value = patch_response
        resp = client.put(
            f"/latents/{uuid4()}/placeholder",
            json={
                "user_id": str(uuid4()), "title": "Nerf gun turret",
                "start": "2026-08-27T09:00:00", "effort_minutes": 240,
                "existing_event_id": "same-evt",
            },
        )

    assert resp.status_code == 200
    assert resp.json() == {"event_id": "same-evt"}
    mock_session_cls.return_value.post.assert_not_called()


def test_delete_placeholder_calls_calendar_delete(client):
    with (
        patch(
            "committer_svc.main._user_credentials",
            return_value=(MagicMock(), "America/Vancouver"),
        ),
        patch("committer_svc.main.AuthorizedSession") as mock_session_cls,
    ):
        mock_session_cls.return_value.delete.return_value.status_code = 204
        resp = client.request(
            "DELETE",
            f"/latents/{uuid4()}/placeholder",
            params={"user_id": str(uuid4()), "event_id": "evt-to-delete"},
        )

    assert resp.status_code == 200
    mock_session_cls.return_value.delete.assert_called_once()


def test_no_linked_google_account_fails_without_writing(client):
    confirmed = _confirmed_message()
    conn = _mock_connection(user_row=None)

    with patch("committer_svc.main.get_connection", return_value=conn):
        resp = client.post("/pubsub/push", json=_push_envelope(confirmed))

    assert resp.status_code == 500


def test_email_missing_recipient_fails_loudly(client):
    """Should never happen for real — resolver-svc/dispatcher-svc only
    ever publish action_type="email" with both fields already resolved
    (agent-contracts.md §2.1/§3.2) — but if it somehow did, this must
    fail loudly rather than silently send a blank/unaddressed email."""
    confirmed = _confirmed_message(
        action_type="email", email_draft="Draft body.", email_recipient=None
    )
    conn = _mock_connection(
        user_row=("projects/p/secrets/user-refresh-token-x/versions/latest", "America/Los_Angeles")
    )

    with (
        patch("committer_svc.main.get_connection", return_value=conn),
        patch("committer_svc.main.AuthorizedSession") as mock_session_cls,
    ):
        resp = client.post("/pubsub/push", json=_push_envelope(confirmed))

    assert resp.status_code == 500
    mock_session_cls.assert_not_called()


def test_email_branch_sends_via_gmail_and_writes_obligation(client):
    confirmed = _confirmed_message(
        action_type="email",
        due_at=None,
        email_recipient="sarah@example.com",
        email_draft="Hi Sarah,\n\nConfirming the delay.\n\nThanks",
    )
    conn = _mock_connection(
        user_row=("projects/p/secrets/user-refresh-token-x/versions/latest", "America/Los_Angeles")
    )
    gmail_response = MagicMock()
    gmail_response.json.return_value = {"id": "gmail-msg-123"}

    with (
        patch("committer_svc.main.get_connection", return_value=conn),
        patch("committer_svc.main._secret_client", return_value=_mock_secret_client()),
        patch("committer_svc.main.AuthorizedSession") as mock_session_cls,
    ):
        mock_session_cls.return_value.post.return_value = gmail_response
        resp = client.post("/pubsub/push", json=_push_envelope(confirmed))

    assert resp.status_code == 200
    assert resp.json() == {"status": "committed", "item_id": str(confirmed.item_id)}

    mock_session_cls.return_value.post.assert_called_once()
    args, kwargs = mock_session_cls.return_value.post.call_args
    assert args[0] == "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
    raw = kwargs["json"]["raw"]
    decoded = base64.urlsafe_b64decode(raw).decode()
    assert "sarah@example.com" in decoded
    assert "Confirming the delay" in decoded

    # call 0 is the idempotency guard; 1 is the credentials SELECT;
    # 2 is the obligations INSERT; 3 is the items UPDATE.
    insert_sql, insert_params = conn.execute.call_args_list[2][0]
    assert "INSERT INTO obligations" in insert_sql
    assert "email_draft" in insert_sql
    assert insert_params[2] == "email"  # action_type
    assert insert_params[3] == "Hi Sarah,\n\nConfirming the delay.\n\nThanks"  # email_draft

    update_sql, update_params = conn.execute.call_args_list[3][0]
    assert "state = 'COMMITTED'" in update_sql
    assert update_params == ("obligation", str(confirmed.item_id))


def test_email_redelivery_after_success_is_a_noop(client):
    """_already_committed() generalizes to the email branch with no
    email-specific code — an obligations row existing is enough,
    regardless of which branch originally wrote it."""
    confirmed = _confirmed_message(
        action_type="email", email_recipient="sarah@example.com", email_draft="Hi Sarah."
    )
    conn = _mock_connection(already_committed=True)

    with (
        patch("committer_svc.main.get_connection", return_value=conn),
        patch("committer_svc.main.AuthorizedSession") as mock_session_cls,
    ):
        resp = client.post("/pubsub/push", json=_push_envelope(confirmed))

    assert resp.status_code == 200
    assert resp.json() == {"status": "already_processed", "item_id": str(confirmed.item_id)}
    mock_session_cls.assert_not_called()


def test_malformed_envelope_returns_500_for_retry(client):
    resp = client.post("/pubsub/push", json={"message": {"data": "not-valid-base64json"}})
    assert resp.status_code == 500


# --- idempotency guard (step 13, refined by a real bug found in step 14) --


def test_redelivered_already_committed_item_is_a_noop(client):
    """The real bug class found in step 11: a concurrent Pub/Sub
    redelivery of the same items.confirmed message must not create a
    second real Calendar event."""
    confirmed = _confirmed_message()
    conn = _mock_connection(already_committed=True)

    with (
        patch("committer_svc.main.get_connection", return_value=conn),
        patch("committer_svc.main.AuthorizedSession") as mock_session_cls,
    ):
        resp = client.post("/pubsub/push", json=_push_envelope(confirmed))

    assert resp.status_code == 200
    assert resp.json() == {"status": "already_processed", "item_id": str(confirmed.item_id)}
    mock_session_cls.assert_not_called()
    assert conn.execute.call_count == 1  # only the already-committed check


def test_accepted_latent_is_not_blocked_by_its_own_prior_commit(client):
    """The real bug found verifying step 14's accept path: an item that
    was already COMMITTED once as a latent must not be treated as
    already-processed when it legitimately comes through a second time
    as an accepted obligation — _already_committed() checks the
    obligations table specifically (empty here), not items.state or the
    latents table (which does have a row, from the original commit)."""
    confirmed = _confirmed_message(type="obligation")  # dispatcher-svc's accept publish
    conn = _mock_connection(
        already_committed=False,  # no obligations row yet, even though latents has one
        user_row=("projects/p/secrets/user-refresh-token-x/versions/latest", "America/Los_Angeles"),
    )
    calendar_response = MagicMock()
    calendar_response.json.return_value = {"id": "gcal-event-456"}

    with (
        patch("committer_svc.main.get_connection", return_value=conn),
        patch("committer_svc.main._secret_client", return_value=_mock_secret_client()),
        patch("committer_svc.main.AuthorizedSession") as mock_session_cls,
    ):
        mock_session_cls.return_value.post.return_value = calendar_response
        resp = client.post("/pubsub/push", json=_push_envelope(confirmed))

    assert resp.status_code == 200
    assert resp.json() == {"status": "committed", "item_id": str(confirmed.item_id)}
    mock_session_cls.return_value.post.assert_called_once()


# --- /pubsub/dlq (step 13) -------------------------------------------------


def _dlq_envelope(payload: dict, retry_count: int = 5) -> dict:
    import base64
    import json

    data = base64.b64encode(json.dumps(payload).encode()).decode()
    return {
        "message": {
            "data": data,
            "attributes": {"CloudPubSubDeadLetterSourceDeliveryCount": str(retry_count)},
            "messageId": "123",
        },
        "subscription": "projects/p/subscriptions/items-raw-dlq-committer-push",
    }


def test_dlq_writes_dead_letter_row_and_marks_failed(client):
    item_id = str(uuid4())
    payload = {"item_id": item_id, "user_id": str(uuid4()), "text": "hi"}
    conn = _mock_connection()

    with patch("committer_svc.main.get_connection", return_value=conn):
        resp = client.post(
            "/pubsub/dlq?stage=items-raw", json=_dlq_envelope(payload, retry_count=5)
        )

    assert resp.status_code == 200
    assert resp.json() == {"status": "dead_lettered", "item_id": item_id}

    insert_sql, insert_params = conn.execute.call_args_list[0][0]
    assert "INSERT INTO dead_letters" in insert_sql
    assert insert_params[0] == item_id
    assert insert_params[1] == "items-raw"
    assert insert_params[4] == 5  # retry_count

    update_sql, update_params = conn.execute.call_args_list[1][0]
    assert "state = 'FAILED'" in update_sql
    assert update_params[0] == item_id


def test_dlq_malformed_envelope_acked_not_retried(client):
    """A dead-lettered message that can't even be parsed has no item_id
    to record against — logged and acked (200), not retried forever."""
    conn = _mock_connection()
    with patch("committer_svc.main.get_connection", return_value=conn):
        resp = client.post(
            "/pubsub/dlq?stage=items-raw",
            json={"message": {"data": "not-valid-base64json", "attributes": {}}},
        )
    assert resp.status_code == 200
    conn.execute.assert_not_called()


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
