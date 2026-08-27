"""docs/engineering/test-plan.md step 8 — deterministic template rendering,
agent-contracts.md §4.1/§4.2. No I/O."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from dispatcher_svc.templates import (
    relative_due_description,
    render_accepted,
    render_deferred,
    render_dismissed,
    render_event_reminder,
    render_reminder,
    render_snoozed,
)


def test_relative_due_description_today_tomorrow_and_n_days():
    today = date(2026, 8, 27)
    assert relative_due_description(date(2026, 8, 27), today) == "today"
    assert relative_due_description(date(2026, 8, 26), today) == "today"  # already past
    assert relative_due_description(date(2026, 8, 28), today) == "tomorrow"
    assert relative_due_description(date(2026, 8, 30), today) == "in 3 days"


def test_render_reminder_exact_format():
    """v1 simplification: one reminder now, at the time-of."""
    due = datetime(2026, 9, 4, 14, 0)
    body = render_reminder("Pay rent", due, today=date(2026, 9, 4))
    assert body == "⏰ last call, Pay rent is due today, Fri 4 Sep, 2:00 PM."


def test_render_event_reminder_exact_format():
    due = datetime(2026, 8, 25, 20, 39)
    body = render_event_reminder("Meeting", due, today=date(2026, 8, 25))
    assert body == "⏰ Meeting is starting now, Tue 25 Aug, 8:39 PM."


def test_render_deferred_with_a_new_slot():
    tz = ZoneInfo("America/Vancouver")
    next_fit = datetime(2026, 8, 28, 9, 0, tzinfo=tz)
    body = render_deferred(next_fit, tz)
    assert body == "np, i'll text you again Friday."


def test_render_deferred_with_no_slot_found():
    body = render_deferred(None, ZoneInfo("UTC"))
    assert body == "np, i'll keep an eye out for room."


def test_no_template_body_contains_an_em_dash():
    """No em dash, ever, in anything the bot says (user-directed)."""
    due = datetime(2026, 9, 4, 14, 0)
    today = date(2026, 9, 4)
    bodies = [
        render_reminder("Pay rent", due, today),
        render_event_reminder("Meeting", due, today),
        render_deferred(datetime(2026, 8, 28, 9, 0, tzinfo=ZoneInfo("UTC")), ZoneInfo("UTC")),
        render_deferred(None, ZoneInfo("UTC")),
        render_accepted("Pay rent", due),
        render_dismissed(),
        render_snoozed(),
    ]
    assert all("—" not in body for body in bodies)
