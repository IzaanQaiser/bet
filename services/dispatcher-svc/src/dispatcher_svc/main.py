"""dispatcher-svc — step 8. `POST /dispatch` (Cloud Scheduler + manual
trigger, infrastructure.md §5): computes 7 forward `capacity_snapshots`
rows from real Calendar reads, sends idempotent obligation reminders, and
sends at most one latent-revival suggestion per run.

Step 14 adds `POST /reply` — the Y/N/Later response to a sent
suggestion (state-machine.md §2.2/§2.3), routed here by `ingest-svc`
once a suggestion is actually reachable to reply to. `N` increments
`dismissal_count` (>= 2 → 30d dormancy via `dormant_until`, reused from
snoozing per §2.2's decision — one column, two callers); `Later` snoozes
7d with no penalty; `Y` re-fetches *real, current* Calendar availability
for the suggested day (the original snapshot can be stale by the time a
reply arrives — hours or a day later) and publishes directly to
`items.confirmed`, bypassing resolver-svc entirely (§2.3 — no Gemini
call, every field is already known). Also adds the 24h no-response
timeout (§2.2) at the top of `/dispatch`'s per-user loop, resolved
before that same run scores any new suggestion — otherwise a stale open
suggestion would wrongly keep excluding its own latent from
reconsideration.
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
    select_suggestion,
)
from dispatcher_svc.templates import (
    render_accepted,
    render_dismissed,
    render_event_reminder_early,
    render_event_reminder_start,
    render_reminder_early,
    render_reminder_final,
    render_snoozed,
    render_suggestion,
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
    """Named for its original one caller (select_suggestion, which does
    its own is_eligible() filtering internally) — this actually returns
    every committed latent, unfiltered. _update_next_fit_slots below
    relies on that: a dashboard preview should compute a fit for every
    idea, not just ones the proactive-suggestion gates would currently
    allow texting about."""
    rows = conn.execute(
        """
        SELECT i.id, i.created_at, i.effort_minutes, i.focus_depth,
               l.dismissal_count, l.dormant_until, l.last_surfaced_at,
               EXISTS (
                   SELECT 1 FROM suggestions s WHERE s.item_id = i.id AND s.outcome IS NULL
               ) AS has_open_suggestion
        FROM latents l JOIN items i ON i.id = l.item_id
        WHERE i.user_id = %s AND i.type = 'latent' AND i.state = 'COMMITTED'
        """,
        (str(user_id),),
    ).fetchall()
    return [
        LatentCandidate(
            item_id=str(r[0]),
            created_at=r[1].astimezone(tz).date(),
            effort_minutes=r[2],
            focus_depth=r[3],
            dismissal_count=r[4],
            dormant_until=r[5].astimezone(tz).date() if r[5] else None,
            last_surfaced_at=r[6].astimezone(tz).date() if r[6] else None,
            has_open_suggestion=r[7],
        )
        for r in rows
    ]


def _next_fitting_slot(
    day_context: dict, tz, effort_minutes: int, focus_depth: str
) -> datetime | None:
    """User-directed: the dashboard shows, per idea, when it could
    actually happen — not the revival_score-weighted "best" day
    select_suggestion picks (which can prefer a later but better-scored
    day over an earlier but choppier one), just the earliest day whose
    largest free block physically fits this item. Eligibility gates
    (age/dormancy/cooldown/REVIVAL_THRESHOLD) deliberately don't apply
    here — those exist to avoid annoying, unsolicited texts, not to hide
    computable information from a page the user is voluntarily looking
    at. None when nothing in the 7-day forward window fits."""
    for d in sorted(day_context):
        ctx = day_context[d]
        if ctx.largest_interval is None:
            continue
        if block_fit(ctx.snapshot.largest_contiguous_block, effort_minutes, focus_depth):
            return datetime.combine(d, ctx.largest_interval.start, tzinfo=tz)
    return None


def _update_next_fit_slots(conn, latents: list[LatentCandidate], day_context: dict, tz) -> None:
    for item in latents:
        next_fit = _next_fitting_slot(day_context, tz, item.effort_minutes, item.focus_depth)
        conn.execute(
            "UPDATE latents SET next_fit_start = %s WHERE item_id = %s",
            (next_fit, item.item_id),
        )


def _send_suggestion(conn, user_id, phone, tz, day_context: dict, today, latents) -> bool:
    snapshots = [ctx.snapshot for ctx in day_context.values()]
    best = select_suggestion(latents, snapshots, today)
    if best is None:
        return False

    ctx = day_context[best.snapshot.date]
    if ctx.largest_interval is None:
        logger.error(
            "selected suggestion has no free interval, skipping item_id=%s", best.item.item_id
        )
        return False

    trailing_booked = [c.booked for c in day_context.values()]
    title_row = conn.execute(
        "SELECT title FROM items WHERE id = %s", (best.item.item_id,)
    ).fetchone()
    days_since_capture = (today - best.item.created_at).days

    body = render_suggestion(
        day_name=best.snapshot.date.strftime("%A"),
        block_start_hour=ctx.largest_interval.start.hour,
        block_minutes=ctx.largest_interval.duration_minutes,
        booked_today=ctx.booked,
        trailing_booked_minutes=trailing_booked,
        load_delta=best.snapshot.load_delta,
        item_title=title_row[0],
        days_since_capture=days_since_capture,
    )
    _send_sms(user_id, to=phone, body=body)

    conn.execute(
        "INSERT INTO suggestions (item_id, user_id, snapshot_id) VALUES (%s, %s, %s)",
        (best.item.item_id, str(user_id), ctx.snapshot_id),
    )
    conn.execute(
        """
        UPDATE latents
        SET last_surfaced_at = now(), surface_count = surface_count + 1
        WHERE item_id = %s
        """,
        (best.item.item_id,),
    )
    conn.commit()
    return True


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
            _update_next_fit_slots(conn, latents, day_context, tz)
            conn.commit()
            suggestion_sent = _send_suggestion(
                conn, user_id, phone, tz, day_context, today, latents
            )

        results.append(
            {
                "user_id": str(user_id),
                "snapshots_persisted": len(day_context),
                "reminders_sent": reminders_sent,
                "stale_suggestions_resolved": stale_resolved,
                "suggestion_sent": suggestion_sent,
            }
        )

    logger.info("dispatch complete: %s", results)
    return {"status": "ok", "results": results}


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
    return conn.execute(
        """
        SELECT s.id, s.snapshot_id, i.title, i.summary, i.effort_minutes,
               l.dismissal_count, u.timezone, u.working_hours_start, u.working_hours_end,
               u.google_refresh_token_ref, u.phone_e164, cs.date
        FROM suggestions s
        JOIN items i ON i.id = s.item_id
        JOIN latents l ON l.item_id = s.item_id
        JOIN users u ON u.id = s.user_id
        JOIN capacity_snapshots cs ON cs.id = s.snapshot_id
        WHERE s.item_id = %s AND s.outcome IS NULL
        ORDER BY s.sent_at DESC LIMIT 1
        """,
        (str(item_id),),
    ).fetchone()


def _accept_suggestion(payload: RoutedReplyMessage, suggestion_id, ctx) -> dict:
    (_id, _snapshot_id, title, summary, effort_minutes, _dismissal_count, tz_name, wh_start,
     wh_end, refresh_ref, phone, snapshot_date) = ctx

    if refresh_ref is None:
        logger.error("user_id=%s has no linked Google account, cannot accept", payload.user_id)
        raise HTTPException(status_code=500, detail="no linked Google account")

    # Real current availability, not the (possibly stale) snapshot from
    # when the suggestion was sent — a reply can arrive hours or a day
    # later, and the day's Calendar state can have changed since. Same
    # SUGGESTION_LEAD buffer as the original scoring pass, for the same
    # reason: if the suggested day is today and the reply itself arrives
    # late (the originally-suggested slot already started), don't let
    # due_at land in the past or with no real notice — re-derive today's
    # effective start from right now, not from whatever it was when this
    # suggestion first went out.
    tz = ZoneInfo(tz_name)
    now_local = datetime.now(UTC).astimezone(tz)
    is_today = snapshot_date == now_local.date()
    effective_wh_start = (
        _buffered_wh_start(wh_start, wh_end, now_local) if is_today else wh_start
    )
    session = AuthorizedSession(user_credentials(refresh_ref))
    events_by_day = fetch_events_for_range(session, snapshot_date, snapshot_date, tz_name)
    intervals = free_intervals(
        snapshot_date, events_by_day[snapshot_date], effective_wh_start, wh_end
    )
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
        suggestion_id, _snapshot_id, _title, _summary, _effort_minutes, dismissal_count,
        _tz_name, _wh_start, _wh_end, _refresh_ref, phone, _snapshot_date,
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
                conn.execute(
                    "UPDATE latents SET dismissal_count = %s, "
                    "dormant_until = now() + interval '30 days' WHERE item_id = %s",
                    (new_count, str(payload.item_id)),
                )
            else:
                conn.execute(
                    "UPDATE latents SET dismissal_count = %s WHERE item_id = %s",
                    (new_count, str(payload.item_id)),
                )
            conn.commit()
        _send_sms(payload.user_id, phone, render_dismissed())
        logger.info("DISMISSED item_id=%s dismissal_count=%d", payload.item_id, new_count)
        return {"status": "dismissed", "item_id": str(payload.item_id)}

    if classification == "LATER":
        with get_connection() as conn:
            conn.execute(
                "UPDATE suggestions SET outcome = 'snoozed', responded_at = now() WHERE id = %s",
                (suggestion_id,),
            )
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
