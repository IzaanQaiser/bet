"""dispatcher-svc — `POST /dispatch` (Cloud Scheduler + manual trigger,
infrastructure.md §5): computes 7 forward `capacity_snapshots` rows from
real Calendar reads and sends idempotent obligation reminders.

ADR 0009 replaced the old revival_score/REVIVAL_THRESHOLD "at most one
suggestion per run" engine with a per-idea auto-scheduled placeholder
model, user-directed: every committed latent gets a real, tagged
`[idea] {title}` Calendar event written at its own `next_fit_start` (via
committer-svc — dispatcher-svc still never calls the Calendar write API
itself, see committer_client.py), and gets texted at the exact instant
that slot arrives (`POST /latents/{item_id}/fire`, a Cloud Task —
tasks_client.py). `_recompute_and_reschedule` is the one place that ever
writes `next_fit_start`/`placeholder_event_id`; every other latent's own
placeholder is real Calendar busy time to everything else, which is the
entire mechanism behind a declined idea landing after every
already-scheduled one — no explicit cross-item reflow needed
(capacity-engine.md §5).

`POST /reply` — the Y/N/Later response to a fired suggestion
(state-machine.md §2.2/§2.3), routed here by `ingest-svc`. `Y` re-fetches
*real, current* Calendar availability for the placeholder's slot (it can
be stale by the time a reply arrives) and publishes directly to
`items.confirmed`; committer-svc promotes the same placeholder event in
place rather than writing a duplicate. `N` increments `dismissal_count`
and immediately reschedules to the next available slot (>= 2 → 30d
dormancy instead, placeholder cleared). `Later` snoozes 7d, placeholder
cleared, no dismissal penalty. The 24h no-response timeout (§2.2) still
runs at the top of `/dispatch`'s per-user loop.
"""

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from google.auth.transport.requests import AuthorizedSession
from obligation_engine_shared.db import get_connection, log_message
from obligation_engine_shared.pubsub import publish
from obligation_engine_shared.reply_classifier import classify_reply
from obligation_engine_shared.schemas import ConfirmedItemMessage, RoutedReplyMessage
from pydantic import BaseModel
from twilio.rest import Client as TwilioClient

from dispatcher_svc import committer_client, tasks_client
from dispatcher_svc.calendar_client import fetch_events_for_range, user_credentials
from dispatcher_svc.capacity_engine import (
    CapacitySnapshot,
    Interval,
    LatentCandidate,
    block_fit,
    booked_minutes,
    fragmentation_index,
    free_intervals,
    free_minutes,
    largest_contiguous_block,
    load_delta,
)
from dispatcher_svc.templates import (
    render_accepted,
    render_deferred,
    render_dismissed,
    render_event_reminder_early,
    render_event_reminder_start,
    render_fire_suggestion,
    render_reminder_early,
    render_reminder_final,
    render_snoozed,
)

logger = logging.getLogger("dispatcher_svc")
app = FastAPI()

EFFORT_BUCKETS = (15, 30, 60, 120, 240)  # ConfirmedItemMessage's Literal, schemas.py

# Not secrets by Twilio's own credential model — a SID can't authenticate
# anything without its paired secret (infrastructure.md §4.1, §4's "not a
# secret" notes). Read from env anyway, not hardcoded: keeps them out of
# source scans entirely rather than relying on a reviewer knowing that
# distinction.
TWILIO_FROM_NUMBER = "+14152365420"

TRAILING_DAYS = 14
FORWARD_DAYS = 7

# User-directed (v1): never suggest starting an idea in the past or with
# less than 30 minutes' notice — a free block found by the algorithm has
# to leave the user time to actually see the text and get to it. Applied
# by clipping today's effective working-hours start up to (now + this),
# which naturally reduces to a no-op for every day after today (already
# entirely in the future, so the clip can never bind).
SUGGESTION_LEAD = timedelta(minutes=30)


def _buffered_wh_start(wh_start: time, wh_end: time, now_local: datetime) -> time:
    """Today's real remaining working-hours start, not the nominal one —
    see SUGGESTION_LEAD above. Clamped to wh_end, not left to exceed it:
    a buffer that pushes past the end of the working day correctly
    collapses free_intervals() to zero free minutes for today (verified
    in capacity_engine.py's own _complement — an empty window when
    start >= end), rather than passing an invalid start > end pair in."""
    buffered = (now_local + SUGGESTION_LEAD).time()
    return min(max(wh_start, buffered), wh_end)


def _twilio_client() -> TwilioClient:
    return TwilioClient(
        os.environ["TWILIO_API_KEY_SID"],
        os.environ["TWILIO_API_KEY_SECRET"],
        os.environ["TWILIO_ACCOUNT_SID"],
    )


def _send_sms(user_id, to: str, body: str) -> None:
    """Sends, then logs to the messages table (migrations/0007) in its own
    short transaction — same pattern as resolver-svc's _send_sms, this
    project's one implementation not being duplicated with drift."""
    _twilio_client().messages.create(to=to, from_=TWILIO_FROM_NUMBER, body=body)
    with get_connection() as log_conn:
        log_message(log_conn, user_id, "out", body)
        log_conn.commit()


@dataclass
class DayComputation:
    booked: int
    snapshot: CapacitySnapshot
    largest_interval: Interval | None
    snapshot_id: str | None = None


def _compute_day(events, day, wh_start, wh_end, rolling_mean_input) -> DayComputation:
    intervals = free_intervals(day, events, wh_start, wh_end)
    fm = free_minutes(intervals)
    lcb = largest_contiguous_block(intervals)
    fi = fragmentation_index(intervals)
    booked = booked_minutes(wh_start, wh_end, fm)
    delta = load_delta(booked, rolling_mean_input) if rolling_mean_input is not None else None
    largest = max(intervals, key=lambda i: i.duration_minutes, default=None)
    snapshot = CapacitySnapshot(
        date=day,
        free_minutes=fm,
        largest_contiguous_block=lcb,
        fragmentation_index=fi,
        load_delta=delta,
    )
    return DayComputation(booked=booked, snapshot=snapshot, largest_interval=largest)


def _persist_snapshot(conn, user_id, computation: DayComputation) -> str:
    snap = computation.snapshot
    row = conn.execute(
        """
        INSERT INTO capacity_snapshots
            (user_id, date, free_minutes, largest_contiguous_block, fragmentation_index, load_delta)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (user_id, date) DO UPDATE SET
            free_minutes = EXCLUDED.free_minutes,
            largest_contiguous_block = EXCLUDED.largest_contiguous_block,
            fragmentation_index = EXCLUDED.fragmentation_index,
            load_delta = EXCLUDED.load_delta,
            computed_at = now()
        RETURNING id
        """,
        (
            str(user_id),
            snap.date,
            snap.free_minutes,
            snap.largest_contiguous_block,
            snap.fragmentation_index,
            snap.load_delta,
        ),
    ).fetchone()
    return row[0]


def _send_reminders(conn, user_id, phone, now_utc, tz) -> int:
    """Two independent fire conditions, not one (state-machine.md §4.1's
    old single reminder_window_hours/reminder_sent_at replaced by
    per-obligation reminder_1_at/reminder_2_at — resolver-svc computes both
    from due_at at confirm time, a flat 30-minute-before/at-due rule for
    everything, no effort involved — user-directed simplification). Each
    obligation can fire both in the same /dispatch run if both thresholds
    already passed — same forgiving "better late than never" semantics
    the old single reminder always had."""
    today_local = now_utc.astimezone(tz).date()
    sent = 0

    early_rows = conn.execute(
        """
        SELECT o.item_id, i.title, o.due_at, i.is_scheduled_event
        FROM obligations o JOIN items i ON i.id = o.item_id
        WHERE i.user_id = %s AND i.state = 'COMMITTED' AND o.reminder_1_sent_at IS NULL
          AND o.reminder_1_at IS NOT NULL AND o.reminder_1_at <= %s
        """,
        (str(user_id), now_utc),
    ).fetchall()
    for item_id, title, due_at, is_scheduled_event in early_rows:
        local_due = due_at.astimezone(tz)
        body = (
            render_event_reminder_early(title, local_due, today_local)
            if is_scheduled_event
            else render_reminder_early(title, local_due, today_local)
        )
        _send_sms(user_id, to=phone, body=body)
        conn.execute(
            "UPDATE obligations SET reminder_1_sent_at = now() WHERE item_id = %s",
            (str(item_id),),
        )
        sent += 1

    final_rows = conn.execute(
        """
        SELECT o.item_id, i.title, o.due_at, i.is_scheduled_event
        FROM obligations o JOIN items i ON i.id = o.item_id
        WHERE i.user_id = %s AND i.state = 'COMMITTED' AND o.reminder_2_sent_at IS NULL
          AND o.reminder_2_at IS NOT NULL AND o.reminder_2_at <= %s
        """,
        (str(user_id), now_utc),
    ).fetchall()
    for item_id, title, due_at, is_scheduled_event in final_rows:
        local_due = due_at.astimezone(tz)
        body = (
            render_event_reminder_start(title, local_due, today_local)
            if is_scheduled_event
            else render_reminder_final(title, local_due, today_local)
        )
        _send_sms(user_id, to=phone, body=body)
        conn.execute(
            "UPDATE obligations SET reminder_2_sent_at = now() WHERE item_id = %s",
            (str(item_id),),
        )
        sent += 1

    if sent:
        conn.commit()
    return sent


def _resolve_stale_suggestions(conn, user_id) -> int:
    """SURFACED -> ELIGIBLE via outcome='no_response', no dismissal
    penalty, after 24h of silence (state-machine.md §2.2). Must run
    before this same run's _eligible_latents/_send_suggestion — a stale
    open suggestion's has_open_suggestion=True would otherwise keep
    excluding its latent from ever being reconsidered."""
    rows = conn.execute(
        "UPDATE suggestions SET outcome = 'no_response', responded_at = now() "
        "WHERE user_id = %s AND outcome IS NULL AND sent_at <= now() - interval '24 hours' "
        "RETURNING id",
        (str(user_id),),
    ).fetchall()
    if rows:
        conn.commit()
    return len(rows)


def _eligible_latents(conn, user_id, tz) -> list[LatentCandidate]:
    """Every committed latent that isn't currently dormant — dormancy
    (7d snooze / 30d second-dismissal) short-circuits a sweep recompute
    entirely rather than needing floor-clipping in the slot search
    itself; the item just reappears here, and gets a real recompute,
    once dormant_until passes on its own (a plain timestamp comparison,
    no job). has_open_suggestion is still returned (not filtered here)
    so callers can skip recomputing/re-texting something already
    mid-conversation without a second query."""
    rows = conn.execute(
        """
        SELECT i.id, i.title, i.effort_minutes,
               l.dismissal_count, l.dormant_until, l.last_surfaced_at,
               EXISTS (
                   SELECT 1 FROM suggestions s WHERE s.item_id = i.id AND s.outcome IS NULL
               ) AS has_open_suggestion,
               l.next_fit_start, l.placeholder_event_id
        FROM latents l JOIN items i ON i.id = l.item_id
        WHERE i.user_id = %s AND i.type = 'latent' AND i.state = 'COMMITTED'
          AND (l.dormant_until IS NULL OR l.dormant_until <= now())
        """,
        (str(user_id),),
    ).fetchall()
    return [
        LatentCandidate(
            item_id=str(r[0]),
            title=r[1],
            effort_minutes=r[2],
            dismissal_count=r[3],
            dormant_until=r[4].astimezone(tz).date() if r[4] else None,
            last_surfaced_at=r[5].astimezone(tz).date() if r[5] else None,
            has_open_suggestion=r[6],
            next_fit_start=r[7],
            placeholder_event_id=r[8],
        )
        for r in rows
    ]


def _next_fitting_slot(
    forward_events: dict, tz, wh_start, wh_end, now_local, today, effort_minutes: int,
    exclude_event_id: str | None,
) -> datetime | None:
    """The earliest day in the given window, and the earliest free
    interval *within* that day, whose duration physically fits this item
    — not the day's largest free interval, which can start much later
    than an earlier, smaller-but-still-sufficient one (real bug, found
    live: a 2h idea got scheduled into a 3h gap at 8pm instead of the
    2h36m gap at 5pm that already comfortably fit it, because the old
    version picked whichever interval was biggest, not whichever was
    soonest). `free_intervals` already returns intervals in chronological
    order (built by walking busy time forward), so within a day this is
    just "first one that fits," no extra sort needed. Excludes this same
    item's own current placeholder from what counts as busy
    (capacity-engine.md §5's self-exclusion note — every *other* item's
    placeholder is left in, which is what makes a declined idea naturally
    land after every already-scheduled one). None when nothing in the
    window fits."""
    for d in sorted(forward_events):
        day_wh_start = _buffered_wh_start(wh_start, wh_end, now_local) if d == today else wh_start
        intervals = free_intervals(
            d, forward_events[d], day_wh_start, wh_end, exclude_event_id=exclude_event_id
        )
        for interval in intervals:
            if block_fit(interval.duration_minutes, effort_minutes):
                return datetime.combine(d, interval.start, tzinfo=tz)
    return None


def _clear_placeholder(conn, user_id, item: LatentCandidate) -> None:
    """No fitting slot found, or the item just went dormant — remove any
    existing real placeholder and null both columns. Best-effort on the
    Calendar delete: if it fails, the DB write still proceeds (a leftover
    tagged placeholder on the calendar is a much smaller problem than a
    latent stuck pointing at a since-deleted event id)."""
    if item.placeholder_event_id is not None:
        try:
            committer_client.delete_placeholder(
                UUID(item.item_id), user_id, item.placeholder_event_id
            )
        except Exception:
            logger.exception("failed to delete placeholder item_id=%s", item.item_id)
    conn.execute(
        "UPDATE latents SET next_fit_start = NULL, placeholder_event_id = NULL WHERE item_id = %s",
        (item.item_id,),
    )


def _recompute_and_reschedule(
    conn, user_id, tz, wh_start, wh_end, now_local, today, forward_events, item: LatentCandidate
) -> None:
    """The one place that ever writes latents.next_fit_start/
    placeholder_event_id — every writer (initial commit, working-hours
    change, the sweep, a post-decline reschedule) goes through this.
    Diffs against the value already stored and only touches the
    Calendar/Cloud Tasks when it actually changed, which bounds Cloud
    Tasks volume to "once per real slot change" and guarantees at most
    one live fire-task per latent (an old one that fires anyway is a
    guaranteed no-op — see /latents/{item_id}/fire's staleness check).
    Returns the resulting next_fit_start (None if cleared/never found) so
    callers (e.g. /reply's N-path, which texts back the new day) don't
    need a second query to learn what this just wrote."""
    new_next_fit = _next_fitting_slot(
        forward_events, tz, wh_start, wh_end, now_local, today, item.effort_minutes,
        exclude_event_id=item.placeholder_event_id,
    )

    if new_next_fit == item.next_fit_start:
        return new_next_fit

    if new_next_fit is None:
        _clear_placeholder(conn, user_id, item)
        return None

    try:
        new_event_id = committer_client.upsert_placeholder(
            UUID(item.item_id), user_id, item.title, new_next_fit, item.effort_minutes,
            item.placeholder_event_id,
        )
    except Exception:
        logger.exception("failed to upsert placeholder item_id=%s", item.item_id)
        return item.next_fit_start

    conn.execute(
        "UPDATE latents SET next_fit_start = %s, placeholder_event_id = %s WHERE item_id = %s",
        (new_next_fit, new_event_id, item.item_id),
    )

    try:
        tasks_client.enqueue_fire_task(UUID(item.item_id), new_next_fit)
    except Exception:
        logger.exception("failed to enqueue fire task item_id=%s", item.item_id)

    return new_next_fit


@app.post("/dispatch")
async def dispatch():
    with get_connection() as conn:
        users = conn.execute(
            "SELECT id, timezone, working_hours_start, working_hours_end, phone_e164, "
            "google_refresh_token_ref FROM users"
        ).fetchall()

    results = []
    for user_id, tz_name, wh_start, wh_end, phone, refresh_ref in users:
        if refresh_ref is None:
            logger.warning("user_id=%s has no linked Google account, skipping dispatch", user_id)
            continue

        tz = ZoneInfo(tz_name)
        session = AuthorizedSession(user_credentials(refresh_ref))
        now_utc = datetime.now(UTC)
        today = now_utc.astimezone(tz).date()

        # Two Calendar reads total per user per run, not one per day —
        # infrastructure.md §4's quota assumption.
        trailing_start = today - timedelta(days=TRAILING_DAYS - 1)
        trailing_events = fetch_events_for_range(session, trailing_start, today, tz_name)
        trailing_booked = [
            _compute_day(trailing_events[d], d, wh_start, wh_end, None).booked
            for d in sorted(trailing_events)
        ]

        forward_end = today + timedelta(days=FORWARD_DAYS - 1)
        forward_events = fetch_events_for_range(session, today, forward_end, tz_name)
        now_local = now_utc.astimezone(tz)
        day_context = {}
        with get_connection() as conn:
            for d in sorted(forward_events):
                # Only today's start ever moves — every later day is
                # already entirely in the future, so the buffer can never
                # bind for it (SUGGESTION_LEAD's own note).
                day_wh_start = (
                    _buffered_wh_start(wh_start, wh_end, now_local) if d == today else wh_start
                )
                computation = _compute_day(
                    forward_events[d], d, day_wh_start, wh_end, trailing_booked
                )
                computation.snapshot_id = _persist_snapshot(conn, user_id, computation)
                day_context[d] = computation
            conn.commit()

            reminders_sent = _send_reminders(conn, user_id, phone, now_utc, tz)
            stale_resolved = _resolve_stale_suggestions(conn, user_id)
            latents = _eligible_latents(conn, user_id, tz)
            recomputed = 0
            for item in latents:
                if item.has_open_suggestion:
                    continue
                _recompute_and_reschedule(
                    conn, user_id, tz, wh_start, wh_end, now_local, today, forward_events, item
                )
                recomputed += 1
            conn.commit()

        results.append(
            {
                "user_id": str(user_id),
                "snapshots_persisted": len(day_context),
                "reminders_sent": reminders_sent,
                "stale_suggestions_resolved": stale_resolved,
                "latents_recomputed": recomputed,
            }
        )

    logger.info("dispatch complete: %s", results)
    return {"status": "ok", "results": results}


def _fetch_forward_events(refresh_ref, tz_name, today) -> dict:
    session = AuthorizedSession(user_credentials(refresh_ref))
    forward_end = today + timedelta(days=FORWARD_DAYS - 1)
    return fetch_events_for_range(session, today, forward_end, tz_name)


@app.post("/latents/{item_id}/next-fit")
async def compute_next_fit(item_id: UUID):
    """Fired by committer-svc's _commit_latent, immediately on commit —
    real bug fix, user-directed: without this, a freshly-committed idea
    showed "someday" until the next twice-daily sweep, up to ~6 hours of
    staleness. Recomputes and (via _recompute_and_reschedule) writes the
    real [idea]-tagged placeholder for this one item."""
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT i.title, i.effort_minutes, i.user_id, u.timezone, u.working_hours_start,
                   u.working_hours_end, u.google_refresh_token_ref,
                   l.dismissal_count, l.dormant_until, l.last_surfaced_at,
                   l.next_fit_start, l.placeholder_event_id
            FROM items i
            JOIN latents l ON l.item_id = i.id
            JOIN users u ON u.id = i.user_id
            WHERE i.id = %s
            """,
            (str(item_id),),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="unknown latent item_id")
    (title, effort_minutes, user_id, tz_name, wh_start, wh_end, refresh_ref, dismissal_count,
     dormant_until, last_surfaced_at, next_fit_start, placeholder_event_id) = row

    if refresh_ref is None:
        logger.info(
            "item_id=%s user has no linked Google account, skipping next-fit compute", item_id
        )
        return {"status": "skipped_no_google_account", "item_id": str(item_id)}

    tz = ZoneInfo(tz_name)
    now_utc = datetime.now(UTC)
    today = now_utc.astimezone(tz).date()
    now_local = now_utc.astimezone(tz)
    forward_events = _fetch_forward_events(refresh_ref, tz_name, today)

    item = LatentCandidate(
        item_id=str(item_id),
        title=title,
        effort_minutes=effort_minutes,
        dismissal_count=dismissal_count,
        dormant_until=dormant_until.astimezone(tz).date() if dormant_until else None,
        last_surfaced_at=last_surfaced_at.astimezone(tz).date() if last_surfaced_at else None,
        has_open_suggestion=False,
        next_fit_start=next_fit_start,
        placeholder_event_id=placeholder_event_id,
    )
    with get_connection() as conn:
        _recompute_and_reschedule(
            conn, user_id, tz, wh_start, wh_end, now_local, today, forward_events, item
        )
        conn.commit()

    logger.info("recomputed next-fit item_id=%s", item_id)
    return {"status": "ok", "item_id": str(item_id)}


@app.post("/users/{user_id}/next-fit")
async def compute_next_fit_for_user(user_id: UUID):
    """Fired by dashboard-svc's PATCH /me/profile whenever working hours
    actually change — real bug fix, user-directed follow-up: without
    this, an existing idea's placeholder stayed at its old, now-stale
    slot even after the working hours that determined it changed.
    Recomputes every non-dormant committed latent for this one user off
    a single Calendar read, each with its own self-exclusion."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT timezone, working_hours_start, working_hours_end, google_refresh_token_ref "
            "FROM users WHERE id = %s",
            (str(user_id),),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="unknown user_id")
    tz_name, wh_start, wh_end, refresh_ref = row

    if refresh_ref is None:
        logger.info(
            "user_id=%s has no linked Google account, skipping next-fit recompute", user_id
        )
        return {"status": "skipped_no_google_account", "user_id": str(user_id)}

    tz = ZoneInfo(tz_name)
    now_utc = datetime.now(UTC)
    today = now_utc.astimezone(tz).date()
    now_local = now_utc.astimezone(tz)
    forward_events = _fetch_forward_events(refresh_ref, tz_name, today)

    with get_connection() as conn:
        latents = _eligible_latents(conn, user_id, tz)
        for item in latents:
            if item.has_open_suggestion:
                continue
            _recompute_and_reschedule(
                conn, user_id, tz, wh_start, wh_end, now_local, today, forward_events, item
            )
        conn.commit()

    logger.info("recomputed next-fit for %d latent(s) user_id=%s", len(latents), user_id)
    return {"status": "ok", "user_id": str(user_id), "latents_updated": len(latents)}


@app.post("/dispatch/reminders")
async def dispatch_reminders():
    """SAFETY-NET FALLBACK, not the primary reminder mechanism — see
    /dispatch/reminders/fire below for that. Real gap, found live:
    /dispatch only runs twice a day (7am/1pm America/Toronto,
    scripts/deploy.sh) because each run does 2 real Calendar API reads per
    user (infrastructure.md §4's quota assumption) for the capacity-
    snapshot/suggestion pipeline — appropriate cadence for that,
    completely wrong for reminder delivery. A first fix here was to poll
    this endpoint every few minutes — then correctly pushed back on
    (polling that often is wasteful, and still imprecise) in favor of
    committer-svc scheduling a real Cloud Task for the exact reminder
    instant. This endpoint stays wired up on a much cheaper, infrequent
    cadence (scripts/deploy.sh) purely as a fallback for whatever the
    precise path might miss (a Cloud Tasks outage, a lost task) — no
    Calendar reads, pure Postgres + Twilio either way. Calling
    _send_reminders from both this and /dispatch/reminders/fire is safe,
    not a double-send risk: it's already idempotent
    (reminder_N_sent_at IS NULL)."""
    with get_connection() as conn:
        users = conn.execute("SELECT id, timezone, phone_e164 FROM users").fetchall()

    now_utc = datetime.now(UTC)
    results = []
    for user_id, tz_name, phone in users:
        tz = ZoneInfo(tz_name)
        with get_connection() as conn:
            reminders_sent = _send_reminders(conn, user_id, phone, now_utc, tz)
        if reminders_sent:
            results.append({"user_id": str(user_id), "reminders_sent": reminders_sent})

    logger.info("dispatch_reminders complete: %s", results)
    return {"status": "ok", "results": results}


class ReminderFirePayload(BaseModel):
    item_id: UUID
    slot: int


@app.post("/dispatch/reminders/fire")
async def dispatch_reminders_fire(payload: ReminderFirePayload):
    """The actual, precise reminder mechanism: committer-svc enqueues one
    Cloud Task per reminder slot at the exact reminder instant
    (committer_svc/main.py's _enqueue_reminder_task), and Cloud Tasks
    invokes this directly at that time — no polling, no 5-40 minute
    slop, the reminder fires when it's actually supposed to.

    Still checked against real DB state, not blindly fired: a task
    enqueued hours or days ago could find the item since deleted or
    cancelled, or already sent by the /dispatch/reminders fallback in the
    meantime (Cloud Tasks delivers at-least-once) — same
    reminder_N_sent_at IS NULL idempotency guard every other reminder
    path already relies on."""
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT i.title, o.due_at, i.is_scheduled_event, i.user_id,
                   o.reminder_1_sent_at, o.reminder_2_sent_at, u.timezone, u.phone_e164
            FROM obligations o
            JOIN items i ON i.id = o.item_id
            JOIN users u ON u.id = i.user_id
            WHERE o.item_id = %s AND i.state = 'COMMITTED'
            """,
            (str(payload.item_id),),
        ).fetchone()
        if row is None:
            logger.info(
                "reminder fire skipped item_id=%s slot=%s (no longer committed)",
                payload.item_id,
                payload.slot,
            )
            return {"status": "skipped", "item_id": str(payload.item_id)}

        (
            title,
            due_at,
            is_scheduled_event,
            user_id,
            reminder_1_sent_at,
            reminder_2_sent_at,
            tz_name,
            phone,
        ) = row
        already_sent = reminder_1_sent_at if payload.slot == 1 else reminder_2_sent_at
        if already_sent is not None:
            return {"status": "already_sent", "item_id": str(payload.item_id)}

        tz = ZoneInfo(tz_name)
        local_due = due_at.astimezone(tz)
        today_local = datetime.now(UTC).astimezone(tz).date()
        if payload.slot == 1:
            body = (
                render_event_reminder_early(title, local_due, today_local)
                if is_scheduled_event
                else render_reminder_early(title, local_due, today_local)
            )
            _send_sms(user_id, to=phone, body=body)
            conn.execute(
                "UPDATE obligations SET reminder_1_sent_at = now() WHERE item_id = %s",
                (str(payload.item_id),),
            )
        else:
            body = (
                render_event_reminder_start(title, local_due, today_local)
                if is_scheduled_event
                else render_reminder_final(title, local_due, today_local)
            )
            _send_sms(user_id, to=phone, body=body)
            conn.execute(
                "UPDATE obligations SET reminder_2_sent_at = now() WHERE item_id = %s",
                (str(payload.item_id),),
            )
        conn.commit()

    logger.info("reminder fired item_id=%s slot=%s", payload.item_id, payload.slot)
    return {"status": "sent", "item_id": str(payload.item_id)}


class FirePayload(BaseModel):
    scheduled_for: datetime


@app.post("/latents/{item_id}/fire")
async def fire(item_id: UUID, payload: FirePayload):
    """The Cloud Task tasks_client.enqueue_fire_task schedules for exactly
    a latent's next_fit_start. Re-verifies the block is still actually
    free (a stored next_fit_start can be hours or days old) before
    texting — if it's gone, silently reschedules instead, no SMS, since
    nothing's been asked of the user yet."""
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT i.title, i.effort_minutes, i.user_id, u.timezone, u.working_hours_start,
                   u.working_hours_end, u.google_refresh_token_ref, u.phone_e164,
                   l.dismissal_count, l.dormant_until, l.last_surfaced_at,
                   l.next_fit_start, l.placeholder_event_id,
                   EXISTS (
                       SELECT 1 FROM suggestions s WHERE s.item_id = i.id AND s.outcome IS NULL
                   ) AS has_open_suggestion
            FROM items i
            JOIN latents l ON l.item_id = i.id
            JOIN users u ON u.id = i.user_id
            WHERE i.id = %s AND i.type = 'latent' AND i.state = 'COMMITTED'
            """,
            (str(item_id),),
        ).fetchone()
    if row is None:
        logger.info("fire skipped item_id=%s (no longer a committed latent)", item_id)
        return {"status": "skipped", "item_id": str(item_id)}
    (title, effort_minutes, user_id, tz_name, wh_start, wh_end, refresh_ref, phone,
     dismissal_count, dormant_until, last_surfaced_at, next_fit_start, placeholder_event_id,
     has_open_suggestion) = row

    if next_fit_start != payload.scheduled_for:
        logger.info(
            "fire skipped item_id=%s (stale task, current next_fit_start=%s != %s)",
            item_id, next_fit_start, payload.scheduled_for,
        )
        return {"status": "stale_task_skipped", "item_id": str(item_id)}

    if has_open_suggestion:
        logger.info("fire skipped item_id=%s (already has an open suggestion)", item_id)
        return {"status": "already_open", "item_id": str(item_id)}

    tz = ZoneInfo(tz_name)
    now_utc = datetime.now(UTC)
    today = now_utc.astimezone(tz).date()
    now_local = now_utc.astimezone(tz)

    item = LatentCandidate(
        item_id=str(item_id),
        title=title,
        effort_minutes=effort_minutes,
        dismissal_count=dismissal_count,
        dormant_until=dormant_until.astimezone(tz).date() if dormant_until else None,
        last_surfaced_at=last_surfaced_at.astimezone(tz).date() if last_surfaced_at else None,
        has_open_suggestion=False,
        next_fit_start=next_fit_start,
        placeholder_event_id=placeholder_event_id,
    )

    # Single-day re-verify, same "don't trust a possibly stale value"
    # pattern _accept_suggestion already uses for the Y path.
    forward_events = _fetch_forward_events(refresh_ref, tz_name, today)
    slot_day = next_fit_start.astimezone(tz).date()
    day_wh_start = (
        _buffered_wh_start(wh_start, wh_end, now_local) if slot_day == today else wh_start
    )
    intervals = free_intervals(
        slot_day, forward_events.get(slot_day, []), day_wh_start, wh_end,
        exclude_event_id=placeholder_event_id,
    )
    largest = max(intervals, key=lambda i: i.duration_minutes, default=None)
    still_fits = (
        largest is not None and block_fit(largest_contiguous_block(intervals), effort_minutes)
    )

    if not still_fits:
        logger.info("fire item_id=%s slot no longer fits, rescheduling silently", item_id)
        with get_connection() as conn:
            _recompute_and_reschedule(
                conn, user_id, tz, wh_start, wh_end, now_local, today, forward_events, item
            )
            conn.commit()
        return {"status": "rescheduled_silently", "item_id": str(item_id)}

    with get_connection() as conn:
        _send_sms(user_id, phone, render_fire_suggestion(title, largest.duration_minutes))
        conn.execute(
            "INSERT INTO suggestions (item_id, user_id, snapshot_id, scheduled_for) "
            "VALUES (%s, %s, NULL, %s)",
            (str(item_id), str(user_id), next_fit_start),
        )
        conn.execute(
            "UPDATE latents SET last_surfaced_at = now(), surface_count = surface_count + 1 "
            "WHERE item_id = %s",
            (str(item_id),),
        )
        conn.commit()

    logger.info("FIRED item_id=%s", item_id)
    return {"status": "fired", "item_id": str(item_id)}


def _capped_effort_minutes(original: int, block_minutes: int) -> int:
    """state-machine.md §2.3: "duration = effort_minutes, capped at the
    block length." ConfirmedItemMessage.effort_minutes is a strict
    Literal[15, 30, 60, 120, 240] (schemas.py), so "capped" is
    reinterpreted here as the largest bucket that still fits the block —
    falling back to the smallest bucket if even that overruns, rather
    than refusing to schedule an explicit user Y over a small overrun."""
    fitting = [b for b in EFFORT_BUCKETS if b <= original and b <= block_minutes]
    return max(fitting) if fitting else EFFORT_BUCKETS[0]


def _open_suggestion_context(conn, item_id):
    """scheduled_for (the exact instant /latents/{item_id}/fire actually
    texted about) replaces the old snapshot-derived date — a fire-time
    suggestion has no capacity_snapshots row to join to at all (ADR 0009,
    migrations/0020)."""
    return conn.execute(
        """
        SELECT s.id, i.title, i.summary, i.effort_minutes,
               l.dismissal_count, l.placeholder_event_id,
               u.timezone, u.working_hours_start, u.working_hours_end,
               u.google_refresh_token_ref, u.phone_e164, s.scheduled_for
        FROM suggestions s
        JOIN items i ON i.id = s.item_id
        JOIN latents l ON l.item_id = s.item_id
        JOIN users u ON u.id = s.user_id
        WHERE s.item_id = %s AND s.outcome IS NULL
        ORDER BY s.sent_at DESC LIMIT 1
        """,
        (str(item_id),),
    ).fetchone()


def _accept_suggestion(payload: RoutedReplyMessage, suggestion_id, ctx) -> dict:
    (_id, title, summary, effort_minutes, _dismissal_count, placeholder_event_id, tz_name,
     wh_start, wh_end, refresh_ref, phone, scheduled_for) = ctx

    if refresh_ref is None:
        logger.error("user_id=%s has no linked Google account, cannot accept", payload.user_id)
        raise HTTPException(status_code=500, detail="no linked Google account")

    # Real current availability, not the (possibly stale) suggestion from
    # when it was sent — a reply can arrive hours or a day later, and the
    # day's Calendar state can have changed since. Same SUGGESTION_LEAD
    # buffer as the original scoring pass, for the same reason: if the
    # suggested day is today and the reply itself arrives late (the
    # originally-suggested slot already started), don't let due_at land
    # in the past or with no real notice — re-derive today's effective
    # start from right now, not from whatever it was when this suggestion
    # first went out. The item's own real placeholder is on the calendar
    # too by now — exclude it, or it would incorrectly read as busy.
    tz = ZoneInfo(tz_name)
    now_local = datetime.now(UTC).astimezone(tz)
    snapshot_date = scheduled_for.astimezone(tz).date()
    is_today = snapshot_date == now_local.date()
    effective_wh_start = (
        _buffered_wh_start(wh_start, wh_end, now_local) if is_today else wh_start
    )
    session = AuthorizedSession(user_credentials(refresh_ref))
    events_by_day = fetch_events_for_range(session, snapshot_date, snapshot_date, tz_name)
    intervals = free_intervals(
        snapshot_date, events_by_day[snapshot_date], effective_wh_start, wh_end,
        exclude_event_id=placeholder_event_id,
    )
    # Earliest interval that fully fits the original effort_minutes first
    # — real bug, found live: picking the day's *largest* interval here
    # could silently commit the obligation to a completely different time
    # than what was actually texted (scheduled_for), if some other part
    # of the day happened to have a bigger gap. Only fall back to
    # "whatever's biggest, capped down" (the pre-existing behavior) when
    # nothing fits the full request — same "never refuse an explicit Y"
    # principle _capped_effort_minutes already documents, just no longer
    # the first choice.
    largest = next((i for i in intervals if i.duration_minutes >= effort_minutes), None)
    if largest is None:
        largest = max(intervals, key=lambda i: i.duration_minutes, default=None)

    if largest is None:
        # The day genuinely filled up between send and reply — a real
        # edge case, not hypothetical. Tell the user rather than
        # silently failing or scheduling into a conflict.
        with get_connection() as conn:
            conn.execute(
                "UPDATE suggestions SET outcome = 'dismissed', responded_at = now() WHERE id = %s",
                (suggestion_id,),
            )
            conn.commit()
        _send_sms(
            payload.user_id,
            phone,
            f'Sorry, that day filled up — I couldn\'t find room for "{title}" anymore.',
        )
        logger.info("accept failed, no free block left item_id=%s", payload.item_id)
        return {"status": "no_capacity", "item_id": str(payload.item_id)}

    capped_effort = _capped_effort_minutes(effort_minutes, largest.duration_minutes)
    due_at_local = datetime.combine(snapshot_date, largest.start, tzinfo=ZoneInfo(tz_name))

    confirmed = ConfirmedItemMessage(
        item_id=payload.item_id,
        user_id=payload.user_id,
        type="obligation",
        title=title,
        summary=summary,
        due_at=due_at_local,
        effort_minutes=capped_effort,
        action_type="calendar",
        email_draft=None,
    )
    publish("items-confirmed", confirmed)

    with get_connection() as conn:
        conn.execute(
            "UPDATE suggestions SET outcome = 'accepted', responded_at = now() WHERE id = %s",
            (suggestion_id,),
        )
        conn.commit()
    _send_sms(payload.user_id, phone, render_accepted(title, due_at_local))
    logger.info("ACCEPTED item_id=%s due_at=%s", payload.item_id, due_at_local)
    return {"status": "accepted", "item_id": str(payload.item_id)}


@app.post("/reply")
async def reply(payload: RoutedReplyMessage):
    with get_connection() as conn:
        ctx = _open_suggestion_context(conn, payload.item_id)
    if ctx is None:
        logger.warning("reply routed for item_id=%s with no open suggestion", payload.item_id)
        return {"status": "unexpected_state", "item_id": str(payload.item_id)}
    (
        suggestion_id, title, _summary, effort_minutes, dismissal_count, placeholder_event_id,
        tz_name, wh_start, wh_end, refresh_ref, phone, _scheduled_for,
    ) = ctx

    classification = classify_reply(payload.text)

    if classification == "Y":
        return _accept_suggestion(payload, suggestion_id, ctx)

    if classification == "N":
        new_count = dismissal_count + 1
        with get_connection() as conn:
            conn.execute(
                "UPDATE suggestions SET outcome = 'dismissed', responded_at = now() WHERE id = %s",
                (suggestion_id,),
            )
            if new_count >= 2:
                # Second dismissal: 30d dormancy, same as before — but now
                # also clear the real placeholder rather than leaving a
                # stale [idea] event sitting on the calendar for a month.
                item = LatentCandidate(
                    item_id=str(payload.item_id), title=title, effort_minutes=effort_minutes,
                    dismissal_count=new_count, dormant_until=None, last_surfaced_at=None,
                    has_open_suggestion=False, next_fit_start=None,
                    placeholder_event_id=placeholder_event_id,
                )
                _clear_placeholder(conn, payload.user_id, item)
                conn.execute(
                    "UPDATE latents SET dismissal_count = %s, "
                    "dormant_until = now() + interval '30 days' WHERE item_id = %s",
                    (new_count, str(payload.item_id)),
                )
                conn.commit()
                _send_sms(payload.user_id, phone, render_dismissed())
                logger.info(
                    "DISMISSED (dormant) item_id=%s dismissal_count=%d",
                    payload.item_id, new_count,
                )
                return {"status": "dismissed", "item_id": str(payload.item_id)}

            conn.execute(
                "UPDATE latents SET dismissal_count = %s WHERE item_id = %s",
                (new_count, str(payload.item_id)),
            )
            conn.commit()

        # First dismissal: reschedule to the next available slot right
        # away, user-directed — a decline isn't a "don't ask again for a
        # while" signal on its own, only a second one is.
        new_next_fit = None
        if refresh_ref is not None:
            tz = ZoneInfo(tz_name)
            now_utc = datetime.now(UTC)
            today = now_utc.astimezone(tz).date()
            now_local = now_utc.astimezone(tz)
            forward_events = _fetch_forward_events(refresh_ref, tz_name, today)
            item = LatentCandidate(
                item_id=str(payload.item_id), title=title, effort_minutes=effort_minutes,
                dismissal_count=new_count, dormant_until=None, last_surfaced_at=None,
                has_open_suggestion=False, next_fit_start=None,
                placeholder_event_id=placeholder_event_id,
            )
            with get_connection() as conn:
                new_next_fit = _recompute_and_reschedule(
                    conn, payload.user_id, tz, wh_start, wh_end, now_local, today,
                    forward_events, item,
                )
                conn.commit()
            tz_for_render = tz
        else:
            tz_for_render = UTC

        _send_sms(payload.user_id, phone, render_deferred(new_next_fit, tz_for_render))
        logger.info("DEFERRED item_id=%s next_fit_start=%s", payload.item_id, new_next_fit)
        return {"status": "dismissed", "item_id": str(payload.item_id)}

    if classification == "LATER":
        item = LatentCandidate(
            item_id=str(payload.item_id), title=title, effort_minutes=effort_minutes,
            dismissal_count=dismissal_count, dormant_until=None, last_surfaced_at=None,
            has_open_suggestion=False, next_fit_start=None,
            placeholder_event_id=placeholder_event_id,
        )
        with get_connection() as conn:
            conn.execute(
                "UPDATE suggestions SET outcome = 'snoozed', responded_at = now() WHERE id = %s",
                (suggestion_id,),
            )
            _clear_placeholder(conn, payload.user_id, item)
            conn.execute(
                "UPDATE latents SET dormant_until = now() + interval '7 days' WHERE item_id = %s",
                (str(payload.item_id),),
            )
            conn.commit()
        _send_sms(payload.user_id, phone, render_snoozed())
        logger.info("SNOOZED item_id=%s", payload.item_id)
        return {"status": "snoozed", "item_id": str(payload.item_id)}

    logger.info(
        "suggestion reply outside Y/N/Later, not handled item_id=%s text=%r",
        payload.item_id,
        payload.text,
    )
    return {"status": "unhandled_reply", "item_id": str(payload.item_id)}


@app.get("/health")
async def health():
    return {"status": "ok"}
