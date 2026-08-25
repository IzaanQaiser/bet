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
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from google.auth.transport.requests import AuthorizedSession
from obligation_engine_shared.db import get_connection, log_message
from obligation_engine_shared.pubsub import publish
from obligation_engine_shared.reply_classifier import classify_reply
from obligation_engine_shared.schemas import ConfirmedItemMessage, RoutedReplyMessage
from twilio.rest import Client as TwilioClient

from dispatcher_svc.calendar_client import fetch_events_for_range, user_credentials
from dispatcher_svc.capacity_engine import (
    CapacitySnapshot,
    Interval,
    LatentCandidate,
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
    render_reminder,
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
    rows = conn.execute(
        """
        SELECT o.item_id, i.title, o.due_at
        FROM obligations o JOIN items i ON i.id = o.item_id
        WHERE i.user_id = %s AND i.state = 'COMMITTED' AND o.reminder_sent_at IS NULL
          AND o.due_at <= %s + make_interval(hours => o.reminder_window_hours)
        """,
        (str(user_id), now_utc),
    ).fetchall()
    sent = 0
    today_local = now_utc.astimezone(tz).date()
    for item_id, title, due_at in rows:
        local_due = due_at.astimezone(tz)
        _send_sms(user_id, to=phone, body=render_reminder(title, local_due, today_local))
        conn.execute(
            "UPDATE obligations SET reminder_sent_at = now() WHERE item_id = %s", (str(item_id),)
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
        day_context = {}
        with get_connection() as conn:
            for d in sorted(forward_events):
                computation = _compute_day(forward_events[d], d, wh_start, wh_end, trailing_booked)
                computation.snapshot_id = _persist_snapshot(conn, user_id, computation)
                day_context[d] = computation
            conn.commit()

            reminders_sent = _send_reminders(conn, user_id, phone, now_utc, tz)
            stale_resolved = _resolve_stale_suggestions(conn, user_id)
            latents = _eligible_latents(conn, user_id, tz)
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
    # later, and the day's Calendar state can have changed since.
    session = AuthorizedSession(user_credentials(refresh_ref))
    events_by_day = fetch_events_for_range(session, snapshot_date, snapshot_date, tz_name)
    intervals = free_intervals(snapshot_date, events_by_day[snapshot_date], wh_start, wh_end)
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
