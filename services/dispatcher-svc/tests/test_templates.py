"""docs/engineering/test-plan.md step 8 — deterministic template rendering,
agent-contracts.md §4.1/§4.2. No I/O."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from dispatcher_svc.templates import (
    relative_due_description,
    render_deferred,
    render_event_reminder_early,
    render_event_reminder_start,
    render_fire_suggestion,
    render_reminder_early,
    render_reminder_final,
)


def test_relative_due_description_today_tomorrow_and_n_days():
    today = date(2026, 8, 27)
    assert relative_due_description(date(2026, 8, 27), today) == "today"
    assert relative_due_description(date(2026, 8, 26), today) == "today"  # already past
    assert relative_due_description(date(2026, 8, 28), today) == "tomorrow"
    assert relative_due_description(date(2026, 8, 30), today) == "in 3 days"


def test_render_reminder_early_exact_format():
    due = datetime(2026, 9, 4, 14, 0)  # a real Friday
    body = render_reminder_early("Pay rent", due, today=date(2026, 9, 3))
    assert body == "⏰ heads up — Pay rent is due tomorrow, Fri 4 Sep, 2:00 PM."


def test_render_reminder_final_exact_format():
    due = datetime(2026, 9, 4, 14, 0)
    body = render_reminder_final("Pay rent", due, today=date(2026, 9, 4))
    assert body == "⏰ last call — Pay rent is due today, Fri 4 Sep, 2:00 PM."


def test_render_event_reminder_early_exact_format():
    due = datetime(2026, 8, 25, 20, 39)
    body = render_event_reminder_early("Meeting", due, today=date(2026, 8, 25))
    assert body == "⏰ heads up — Meeting starts today, Tue 25 Aug, 8:39 PM."


def test_render_event_reminder_start_exact_format():
    due = datetime(2026, 8, 25, 20, 39)
    body = render_event_reminder_start("Meeting", due, today=date(2026, 8, 25))
    assert body == "⏰ Meeting is starting now — Tue 25 Aug, 8:39 PM."


def test_render_fire_suggestion_exact_format():
    """ADR 0009 — fires at the exact instant a latent's next_fit_start
    arrives, replacing the old revival_score-picked render_suggestion."""
    body = render_fire_suggestion("Nerf gun turret", 240)
    assert body == (
        'yo u have 4h free right now — wanna bang out "Nerf gun turret"?\n\nY / N / Later'
    )


def test_render_fire_suggestion_formats_partial_hours():
    body = render_fire_suggestion("x", 90)
    assert "1h 30min free" in body


def test_render_deferred_with_a_new_slot():
    tz = ZoneInfo("America/Vancouver")
    next_fit = datetime(2026, 8, 28, 9, 0, tzinfo=tz)
    body = render_deferred(next_fit, tz)
    assert body == "np — i'll text you again Friday."


def test_render_deferred_with_no_slot_found():
    body = render_deferred(None, ZoneInfo("UTC"))
    assert body == "np, i'll keep an eye out for room."
