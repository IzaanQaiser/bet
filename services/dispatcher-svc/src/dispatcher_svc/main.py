"""dispatcher-svc — step 8. `POST /dispatch` (Cloud Scheduler + manual
trigger, infrastructure.md §5): computes 7 forward `capacity_snapshots`
rows from real Calendar reads, sends idempotent obligation reminders, and
sends at most one latent-revival suggestion per run.

Deliberately NOT built here: parsing a Y/N/Later reply to a sent
suggestion, or moving a latent out of the SURFACED phase — those are the
latent-lifecycle transitions (state-machine.md §2.2), which is a
different concern from *sending* the suggestion and belongs with the
feedback-loop step. A sent suggestion just sits as a `suggestions` row
with `outcome IS NULL` until that step exists.
"""

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from google.auth.transport.requests import AuthorizedSession
from obligation_engine_shared.db import get_connection
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
from dispatcher_svc.templates import render_reminder, render_suggestion

logger = logging.getLogger("dispatcher_svc")
app = FastAPI()

# Plain config, not secrets — same treatment as every other Twilio/OAuth
# identifier in this project (infrastructure.md §4.1, §4's "not a secret"
# notes). Only TWILIO_API_KEY_SECRET is an actual secret, via env.
TWILIO_ACCOUNT_SID = "AC3292d4a7944b87b2fe3db562856e32bd"
TWILIO_API_KEY_SID = "SK7a7912d15fea946956ab8bbae8214bce"
TWILIO_FROM_NUMBER = "+14152365420"

TRAILING_DAYS = 14
FORWARD_DAYS = 7


def _twilio_client() -> TwilioClient:
    return TwilioClient(TWILIO_API_KEY_SID, os.environ["TWILIO_API_KEY_SECRET"], TWILIO_ACCOUNT_SID)


def _send_sms(to: str, body: str) -> None:
    _twilio_client().messages.create(to=to, from_=TWILIO_FROM_NUMBER, body=body)


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
        _send_sms(to=phone, body=render_reminder(title, local_due, today_local))
        conn.execute(
            "UPDATE obligations SET reminder_sent_at = now() WHERE item_id = %s", (str(item_id),)
        )
        sent += 1
    if sent:
        conn.commit()
    return sent


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
    _send_sms(to=phone, body=body)

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
            latents = _eligible_latents(conn, user_id, tz)
            suggestion_sent = _send_suggestion(
                conn, user_id, phone, tz, day_context, today, latents
            )

        results.append(
            {
                "user_id": str(user_id),
                "snapshots_persisted": len(day_context),
                "reminders_sent": reminders_sent,
                "suggestion_sent": suggestion_sent,
            }
        )

    logger.info("dispatch complete: %s", results)
    return {"status": "ok", "results": results}


@app.get("/health")
async def health():
    return {"status": "ok"}
