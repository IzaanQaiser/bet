"""Integration tests against the real dev Postgres (via the Cloud SQL Auth
Proxy) — docs/engineering/test-plan.md step 6. No Pub/Sub emulator needed
here — committer-svc is the terminal write step, it never publishes.
Endpoint invoked directly (matching resolver-svc's own integration test
pattern), same push-envelope shape a real subscription would deliver.

The Calendar API itself is mocked here (real verification is manual, per
the test plan — mocks can't validate real OAuth/API behavior); Secret
Manager is also mocked, since minting a real refresh token requires the
one-time manual OAuth bootstrap covered in infrastructure.md §4.

Requires DB_USER, DB_HOST, DB_PORT set. Skipped automatically otherwise.
"""

import base64
import os
import uuid
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from obligation_engine_shared.db import get_connection
from obligation_engine_shared.schemas import ConfirmedItemMessage

pytestmark = pytest.mark.skipif(
    "DB_USER" not in os.environ,
    reason="requires a live Cloud SQL Auth Proxy connection",
)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DB_USER", os.environ["DB_USER"])
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "test-client-secret")
    from committer_svc.main import app

    return TestClient(app)


@pytest.fixture
def test_user_with_google():
    phone = f"+1555{uuid.uuid4().int % 10**7:07d}"
    with get_connection() as conn:
        row = conn.execute(
            "INSERT INTO users (phone_e164, timezone, google_refresh_token_ref) "
            "VALUES (%s, %s, %s) RETURNING id",
            (
                phone,
                "America/Los_Angeles",
                "projects/p/secrets/user-refresh-token-fake/versions/latest",
            ),
        ).fetchone()
        user_id = row[0]
        conn.commit()
    yield user_id
    with get_connection() as conn:
        conn.execute("DELETE FROM users WHERE id = %s", (str(user_id),))
        conn.commit()


@pytest.fixture
def test_item(test_user_with_google):
    with get_connection() as conn:
        row = conn.execute(
            "INSERT INTO items (user_id, raw_channel, ingested_at, state) "
            "VALUES (%s, 'sms', now(), 'CONFIRMED') RETURNING id",
            (str(test_user_with_google),),
        ).fetchone()
        item_id = row[0]
        conn.commit()
    yield item_id, test_user_with_google
    with get_connection() as conn:
        conn.execute("DELETE FROM obligations WHERE item_id = %s", (str(item_id),))
        conn.execute("DELETE FROM latents WHERE item_id = %s", (str(item_id),))
        conn.execute("DELETE FROM items WHERE id = %s", (str(item_id),))
        conn.commit()


def test_confirmed_obligation_full_cycle(client, test_item):
    item_id, user_id = test_item
    confirmed = ConfirmedItemMessage(
        item_id=item_id,
        user_id=user_id,
        type="obligation",
        title="Pay rent",
        summary="Pay rent by Friday.",
        due_at=datetime(2026, 8, 28, 14, 0),
        effort_minutes=15,
        action_type="calendar",
        email_draft=None,
    )
    envelope = {
        "message": {"data": base64.b64encode(confirmed.model_dump_json().encode()).decode()}
    }

    calendar_response = MagicMock()
    calendar_response.json.return_value = {"id": "gcal-event-integration-test"}
    secret_client = MagicMock()
    secret_client.access_secret_version.return_value.payload.data = b"fake-refresh-token"

    with (
        patch("committer_svc.main._secret_client", return_value=secret_client),
        patch("committer_svc.main.AuthorizedSession") as mock_session_cls,
    ):
        mock_session_cls.return_value.post.return_value = calendar_response
        resp = client.post("/pubsub/push", json=envelope)
    assert resp.status_code == 200

    with get_connection() as conn:
        item_row = conn.execute("SELECT state FROM items WHERE id = %s", (str(item_id),)).fetchone()
        obligation_row = conn.execute(
            "SELECT calendar_event_id, action_type FROM obligations WHERE item_id = %s",
            (str(item_id),),
        ).fetchone()
    assert item_row[0] == "COMMITTED"
    assert obligation_row == ("gcal-event-integration-test", "calendar")


def test_confirmed_latent_full_cycle(client, test_item):
    item_id, user_id = test_item
    confirmed = ConfirmedItemMessage(
        item_id=item_id,
        user_id=user_id,
        type="latent",
        title="Learn pottery",
        summary="Someday.",
        due_at=None,
        effort_minutes=120,
        action_type=None,
        email_draft=None,
    )
    envelope = {
        "message": {"data": base64.b64encode(confirmed.model_dump_json().encode()).decode()}
    }

    with patch("committer_svc.main.AuthorizedSession") as mock_session_cls:
        resp = client.post("/pubsub/push", json=envelope)
    assert resp.status_code == 200
    mock_session_cls.assert_not_called()

    with get_connection() as conn:
        item_row = conn.execute("SELECT state FROM items WHERE id = %s", (str(item_id),)).fetchone()
        latent_row = conn.execute(
            "SELECT surface_count, dismissal_count, dormant_until FROM latents WHERE item_id = %s",
            (str(item_id),),
        ).fetchone()
    assert item_row[0] == "COMMITTED"
    assert latent_row == (0, 0, None)
