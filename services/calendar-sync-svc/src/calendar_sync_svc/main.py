"""calendar-sync-svc — two-way Calendar sync. Real gap, found live and
named in dashboard-svc's own delete_item docstring: deleting or
rescheduling an event *directly on Google Calendar* never told this
system anything — only the other direction (delete here -> real Calendar
delete) worked. Fixed with Google's documented mechanism for exactly
this, push notifications + incremental sync (events.watch +
events.list(syncToken=...)), not polling.

This service only ever reads Calendar and writes Postgres — it never
calls the Calendar write API, so it doesn't touch ADR 0003/0009's
single-writer boundary (committer-svc) at all.

POST /webhook is the one public route (--allow-unauthenticated) in this
project: Google's push notifications hit a plain HTTPS URL with no Cloud
Run IAM token, and that toggle is service-wide, not per-route — making
any other service public to host this route would strip protection from
every other endpoint on it. Verified independently instead: the
X-Goog-Channel-Token header, echoed back by Google on every call, is
checked against a per-channel secret generated at registration time.

POST /sync/run (Cloud Scheduler, every 15 min) is both the channel
renewal job and the fallback poll half of the same precise-push +
infrequent-poll pattern already used for reminders
(/dispatch/reminders/fire + /dispatch/reminders). Also public for the
same service-wide-toggle reason, so it's verified the same way /webhook
is, just against a Google-signed OIDC identity token instead of a
channel secret — application-level verification standing in for the
Cloud Run IAM check that isn't available here, not a workaround.
"""

import json
import logging
import os
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Request
from google.auth.transport.requests import AuthorizedSession
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.cloud import secretmanager, tasks_v2
from google.oauth2 import id_token as google_id_token
from google.oauth2.credentials import Credentials
from google.protobuf import timestamp_pb2
from obligation_engine_shared.db import get_connection

logger = logging.getLogger("calendar_sync_svc")
app = FastAPI()

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"
CALENDAR_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
CALENDAR_WATCH_URL = f"{CALENDAR_EVENTS_URL}/watch"
CALENDAR_STOP_URL = "https://www.googleapis.com/calendar/v3/channels/stop"

TASKS_LOCATION = "us-central1"
TASKS_QUEUE = "reminders"

REMINDER_LEAD = timedelta(minutes=30)  # resolver_svc/main.py's own _REMINDER_LEAD, mirrored
CHANNEL_RENEW_WINDOW = timedelta(hours=48)


class _SyncTokenInvalid(Exception):
    """Google's documented signal (a 410 from events.list) that a stored
    syncToken is no longer valid — the client must discard it and start
    a fresh full sync to re-establish a baseline."""


def _secret_client() -> secretmanager.SecretManagerServiceClient:
    return secretmanager.SecretManagerServiceClient()


def _user_credentials(refresh_token_ref: str) -> Credentials:
    refresh_token = (
        _secret_client().access_secret_version(name=refresh_token_ref).payload.data.decode()
    )
    return Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GOOGLE_OAUTH_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
        scopes=[CALENDAR_SCOPE],
    )


def _verify_scheduler_token(request: Request) -> None:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = auth_header.removeprefix("Bearer ")
    project_id = os.environ["GCP_PROJECT_ID"]
    audience = f"{os.environ['CALENDAR_SYNC_SVC_URL']}/sync/run"
    try:
        claims = google_id_token.verify_oauth2_token(token, GoogleAuthRequest(), audience=audience)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="invalid token") from exc
    expected_email = f"sa-calendar-sync@{project_id}.iam.gserviceaccount.com"
    if claims.get("email") != expected_email or not claims.get("email_verified"):
        raise HTTPException(status_code=401, detail="wrong identity")


def _enqueue_reminder_task(item_id, slot: int, fire_at: datetime) -> None:
    """Same shape as committer_svc's own _enqueue_reminder_task, plus
    scheduled_for — the staleness-protection fix (dispatcher_svc/main.py's
    ReminderFirePayload) needs the target instant in the task body so a
    superseded task can recognize itself and no-op."""
    try:
        project_id = os.environ["GCP_PROJECT_ID"]
        dispatcher_url = os.environ["DISPATCHER_SVC_URL"]
        url = f"{dispatcher_url}/dispatch/reminders/fire"
        dispatcher_sa = f"sa-dispatcher@{project_id}.iam.gserviceaccount.com"
        client = tasks_v2.CloudTasksClient()
        parent = client.queue_path(project_id, TASKS_LOCATION, TASKS_QUEUE)
        schedule_time = timestamp_pb2.Timestamp()
        schedule_time.FromDatetime(fire_at.astimezone(UTC))
        client.create_task(
            parent=parent,
            task={
                "http_request": {
                    "http_method": tasks_v2.HttpMethod.POST,
                    "url": url,
                    "headers": {"Content-Type": "application/json"},
                    "body": json.dumps(
                        {
                            "item_id": str(item_id),
                            "slot": slot,
                            "scheduled_for": fire_at.isoformat(),
                        }
                    ).encode(),
                    "oidc_token": {"service_account_email": dispatcher_sa, "audience": url},
                },
                "schedule_time": schedule_time,
            },
        )
    except Exception:
        logger.exception("failed to enqueue reminder task item_id=%s slot=%s", item_id, slot)


def _enqueue_fire_task(item_id, fire_at: datetime) -> None:
    """Same shape as dispatcher_svc's own tasks_client.enqueue_fire_task —
    duplicated rather than imported since it's a different service/
    deployable, same pattern as every other Cloud Tasks enqueuer in this
    project (committer-svc, dashboard-svc each have their own copy)."""
    try:
        project_id = os.environ["GCP_PROJECT_ID"]
        dispatcher_url = os.environ["DISPATCHER_SVC_URL"]
        url = f"{dispatcher_url}/latents/{item_id}/fire"
        dispatcher_sa = f"sa-dispatcher@{project_id}.iam.gserviceaccount.com"
        client = tasks_v2.CloudTasksClient()
        parent = client.queue_path(project_id, TASKS_LOCATION, TASKS_QUEUE)
        schedule_time = timestamp_pb2.Timestamp()
        schedule_time.FromDatetime(fire_at.astimezone(UTC))
        client.create_task(
            parent=parent,
            task={
                "http_request": {
                    "http_method": tasks_v2.HttpMethod.POST,
                    "url": url,
                    "headers": {"Content-Type": "application/json"},
                    "body": json.dumps({"scheduled_for": fire_at.isoformat()}).encode(),
                    "oidc_token": {"service_account_email": dispatcher_sa, "audience": url},
                },
                "schedule_time": schedule_time,
            },
        )
    except Exception:
        logger.exception("failed to enqueue fire task item_id=%s", item_id)


def _ensure_watch(conn, user_id: UUID, refresh_ref: str) -> None:
    """Registers (or renews, within CHANNEL_RENEW_WINDOW of expiry) this
    user's push-notification channel. Calendar API channels aren't
    extendable in place — renewing always means a fresh watch call with a
    new channel id; the old channel is best-effort stopped afterward so
    it doesn't linger."""
    existing = conn.execute(
        "SELECT channel_id, resource_id, channel_expiration FROM calendar_sync_channels "
        "WHERE user_id = %s",
        (str(user_id),),
    ).fetchone()
    if existing is not None and existing[2] > datetime.now(UTC) + CHANNEL_RENEW_WINDOW:
        return

    session = AuthorizedSession(_user_credentials(refresh_ref))
    channel_id = str(uuid4())
    channel_token = secrets.token_urlsafe(32)
    webhook_url = f"{os.environ['CALENDAR_SYNC_SVC_URL']}/webhook"
    resp = session.post(
        CALENDAR_WATCH_URL,
        json={"id": channel_id, "type": "web_hook", "address": webhook_url, "token": channel_token},
    )
    resp.raise_for_status()
    body = resp.json()
    resource_id = body["resourceId"]
    expiration = datetime.fromtimestamp(int(body["expiration"]) / 1000, tz=UTC)

    if existing is not None:
        try:
            session.post(CALENDAR_STOP_URL, json={"id": existing[0], "resourceId": existing[1]})
        except Exception:
            logger.exception("failed to stop old channel user_id=%s", user_id)

    conn.execute(
        """
        INSERT INTO calendar_sync_channels
            (user_id, channel_id, resource_id, channel_token, channel_expiration)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET
            channel_id = EXCLUDED.channel_id,
            resource_id = EXCLUDED.resource_id,
            channel_token = EXCLUDED.channel_token,
            channel_expiration = EXCLUDED.channel_expiration,
            updated_at = now()
        """,
        (str(user_id), channel_id, resource_id, channel_token, expiration),
    )
    conn.commit()
    logger.info("watch registered/renewed user_id=%s expiration=%s", user_id, expiration)


def _fetch_delta(session: AuthorizedSession, sync_token: str | None) -> tuple[list[dict], str]:
    events: list[dict] = []
    page_token = None
    while True:
        params: dict = {"singleEvents": "true"}
        if sync_token:
            params["syncToken"] = sync_token
        if page_token:
            params["pageToken"] = page_token
        resp = session.get(CALENDAR_EVENTS_URL, params=params)
        if resp.status_code == 410:
            raise _SyncTokenInvalid
        resp.raise_for_status()
        payload = resp.json()
        events.extend(payload.get("items", []))
        page_token = payload.get("nextPageToken")
        if not page_token:
            return events, payload["nextSyncToken"]


def _parse_event_start(event: dict) -> datetime | None:
    start = event.get("start", {})
    if "dateTime" not in start:
        return None  # all-day event, or a cancelled event with no start left to read
    return datetime.fromisoformat(start["dateTime"])


def _reconcile_obligation(conn, item_id, stored_due_at, event: dict, cancelled: bool) -> None:
    if cancelled:
        conn.execute("UPDATE items SET state = 'CANCELLED' WHERE id = %s", (str(item_id),))
        conn.commit()
        logger.info("sync: obligation cancelled on Calendar item_id=%s", item_id)
        return

    new_start = _parse_event_start(event)
    if new_start is None or (stored_due_at is not None and new_start == stored_due_at):
        return

    reminder_1_at = new_start - REMINDER_LEAD
    reminder_2_at = new_start
    now = datetime.now(UTC)
    # A slot whose new target has already passed is marked sent, same as
    # committer-svc's own commit-time logic — suppresses a late/incorrect
    # send rather than firing a "heads up" after the fact.
    r1_sent = now if reminder_1_at <= now else None
    r2_sent = now if reminder_2_at <= now else None

    conn.execute(
        """
        UPDATE obligations
        SET due_at = %s, reminder_1_at = %s, reminder_2_at = %s,
            reminder_1_sent_at = %s, reminder_2_sent_at = %s
        WHERE item_id = %s
        """,
        (new_start, reminder_1_at, reminder_2_at, r1_sent, r2_sent, str(item_id)),
    )
    conn.commit()
    logger.info(
        "sync: obligation due_at changed on Calendar item_id=%s new_due_at=%s", item_id, new_start
    )

    if r1_sent is None:
        _enqueue_reminder_task(item_id, 1, reminder_1_at)
    if r2_sent is None:
        _enqueue_reminder_task(item_id, 2, reminder_2_at)


def _reconcile_latent(conn, item_id, stored_next_fit, event: dict, cancelled: bool) -> None:
    if cancelled:
        # Symmetric with dashboard-svc's own forward-delete semantics
        # (delete idea -> cancel item + drop the placeholder).
        conn.execute("UPDATE items SET state = 'CANCELLED' WHERE id = %s", (str(item_id),))
        conn.execute(
            "UPDATE latents SET next_fit_start = NULL, placeholder_event_id = NULL "
            "WHERE item_id = %s",
            (str(item_id),),
        )
        conn.commit()
        logger.info("sync: idea cancelled on Calendar item_id=%s", item_id)
        return

    new_start = _parse_event_start(event)
    if new_start is None or (stored_next_fit is not None and new_start == stored_next_fit):
        return

    conn.execute(
        "UPDATE latents SET next_fit_start = %s WHERE item_id = %s",
        (new_start, str(item_id)),
    )
    conn.commit()
    logger.info(
        "sync: idea slot moved on Calendar item_id=%s new_next_fit_start=%s", item_id, new_start
    )
    _enqueue_fire_task(item_id, new_start)


def _reconcile_event(conn, event: dict) -> None:
    """Looks up whether this Calendar event id is one we actually track
    (an obligation's real event, or a latent's [idea] placeholder) —
    anything else is skipped entirely, never touched. Both lookups are
    scoped to state='COMMITTED' so an already-cancelled item can't be
    "re-cancelled" or have its due_at churned by a leftover event."""
    event_id = event["id"]
    cancelled = event.get("status") == "cancelled"

    ob_row = conn.execute(
        "SELECT o.item_id, o.due_at FROM obligations o JOIN items i ON i.id = o.item_id "
        "WHERE o.calendar_event_id = %s AND i.state = 'COMMITTED'",
        (event_id,),
    ).fetchone()
    if ob_row is not None:
        _reconcile_obligation(conn, ob_row[0], ob_row[1], event, cancelled)
        return

    lat_row = conn.execute(
        "SELECT l.item_id, l.next_fit_start FROM latents l JOIN items i ON i.id = l.item_id "
        "WHERE l.placeholder_event_id = %s AND i.state = 'COMMITTED'",
        (event_id,),
    ).fetchone()
    if lat_row is not None:
        _reconcile_latent(conn, lat_row[0], lat_row[1], event, cancelled)


def _sync_user(conn, user_id: UUID, refresh_ref: str) -> None:
    row = conn.execute(
        "SELECT sync_token FROM calendar_sync_channels WHERE user_id = %s",
        (str(user_id),),
    ).fetchone()
    sync_token = row[0] if row else None

    session = AuthorizedSession(_user_credentials(refresh_ref))

    try:
        events, new_sync_token = _fetch_delta(session, sync_token)
    except _SyncTokenInvalid:
        # No reliable delta once the token's invalid — reseed the
        # baseline without reconciling (§ "_SyncTokenInvalid" note above:
        # reconciling here could redo already-applied changes or miss
        # others). A brand-new user (sync_token was already None) takes
        # this same code path's *other* branch instead — that one's own
        # full-sync result IS a legitimate baseline to reconcile against,
        # since there's no prior known-good state to preserve.
        logger.warning("sync token invalid/expired, reseeding baseline user_id=%s", user_id)
        _events, new_sync_token = _fetch_delta(session, None)
        events = []

    for event in events:
        _reconcile_event(conn, event)

    conn.execute(
        "UPDATE calendar_sync_channels SET sync_token = %s, updated_at = now() WHERE user_id = %s",
        (new_sync_token, str(user_id)),
    )
    conn.commit()


@app.post("/sync/run")
async def sync_run(request: Request):
    _verify_scheduler_token(request)

    with get_connection() as conn:
        users = conn.execute(
            "SELECT id FROM users WHERE google_refresh_token_ref IS NOT NULL"
        ).fetchall()

    results = []
    for (user_id,) in users:
        with get_connection() as conn:
            refresh_row = conn.execute(
                "SELECT google_refresh_token_ref FROM users WHERE id = %s", (str(user_id),)
            ).fetchone()
        refresh_ref = refresh_row[0]
        try:
            with get_connection() as conn:
                _ensure_watch(conn, user_id, refresh_ref)
                _sync_user(conn, user_id, refresh_ref)
            results.append({"user_id": str(user_id), "status": "ok"})
        except Exception:
            logger.exception("sync/run failed user_id=%s", user_id)
            results.append({"user_id": str(user_id), "status": "error"})

    logger.info("sync/run complete: %s", results)
    return {"status": "ok", "results": results}


@app.post("/webhook")
async def webhook(request: Request):
    channel_id = request.headers.get("X-Goog-Channel-ID")
    channel_token = request.headers.get("X-Goog-Channel-Token")
    resource_state = request.headers.get("X-Goog-Resource-State")

    if not channel_id or not channel_token:
        raise HTTPException(status_code=400, detail="missing channel headers")

    with get_connection() as conn:
        row = conn.execute(
            "SELECT user_id, channel_token FROM calendar_sync_channels WHERE channel_id = %s",
            (channel_id,),
        ).fetchone()
    if row is None or row[1] != channel_token:
        logger.warning("webhook: unknown or mismatched channel_id=%s", channel_id)
        raise HTTPException(status_code=403, detail="invalid channel")
    user_id = row[0]

    if resource_state == "sync":
        # The initial confirmation ping sent the moment a channel is
        # registered — no actual Calendar change to react to yet.
        return {"status": "ack"}

    with get_connection() as conn:
        refresh_row = conn.execute(
            "SELECT google_refresh_token_ref FROM users WHERE id = %s", (str(user_id),)
        ).fetchone()
    if refresh_row is None or refresh_row[0] is None:
        return {"status": "skipped"}

    with get_connection() as conn:
        _sync_user(conn, user_id, refresh_row[0])

    return {"status": "ok"}


@app.get("/health")
async def health():
    return {"status": "ok"}
