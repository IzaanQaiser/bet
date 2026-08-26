"""Deterministic SMS templates — agent-contracts.md §4.1/§4.2. No LLM
call for any of this; every field is computed, never generated."""

from datetime import date, datetime


def format_due_at(due_at: datetime) -> str:
    """`Ddd D Mon, H:MM AM/PM` — e.g. `Thu 4 Sep, 2:00 PM`. Matches
    agent-contracts.md §3.3's confirmation-card format; §4.1 doesn't
    restate a different one, so this reuses it rather than inventing a
    second date format."""
    return due_at.strftime("%a %-d %b, %-I:%M %p")


def relative_due_description(due_at: date, today: date) -> str:
    days = (due_at - today).days
    if days <= 0:
        return "today"
    if days == 1:
        return "tomorrow"
    return f"in {days} days"


def _format_block_hours(minutes: int) -> str:
    hours, mins = divmod(minutes, 60)
    if mins == 0:
        return f"{hours}h"
    return f"{hours}h {mins}min"


def render_reminder_early(title: str, due_at: datetime, today: date) -> str:
    """Fires at due_at - 30min — the early heads-up. Deliberately no
    effort/duration claim (e.g. "block off ~2h") — user-directed
    simplification: the reminder itself is a flat 30 minutes regardless of
    effort, so a duration-based claim here would no longer even be true."""
    return (
        f"⏰ heads up — {title} is due {relative_due_description(due_at.date(), today)}, "
        f"{format_due_at(due_at)}."
    )


def render_reminder_final(title: str, due_at: datetime, today: date) -> str:
    """Fires at due_at — the reminder at the deadline itself."""
    return (
        f"⏰ last call — {title} is due {relative_due_description(due_at.date(), today)}, "
        f"{format_due_at(due_at)}."
    )


def render_event_reminder_early(title: str, due_at: datetime, today: date) -> str:
    """Fires at due_at - effort — a heads-up before a scheduled event
    (meeting/party/call/appointment) starts. No "block off time"
    framing — that's task language, wrong for something you just attend."""
    return (
        f"⏰ heads up — {title} starts {relative_due_description(due_at.date(), today)}, "
        f"{format_due_at(due_at)}."
    )


def render_event_reminder_start(title: str, due_at: datetime, today: date) -> str:
    """Fires AT due_at — the reminder a task deliberately never gets, and
    a scheduled event deliberately does: it's happening right now."""
    return f"⏰ {title} is starting now — {format_due_at(due_at)}."


def render_fire_suggestion(item_title: str, block_minutes: int) -> str:
    """ADR 0009 — fires the instant a latent's own next_fit_start
    arrives, replacing the old revival_score-picked, one-per-run
    render_suggestion. Deliberately terser — no time-of-day clause, no
    "lightest day" evidence line; the moment itself is the pitch."""
    return (
        f'yo u have {_format_block_hours(block_minutes)} free right now — '
        f'wanna bang out "{item_title}"?\n\nY / N / Later'
    )


def render_deferred(next_fit_start: datetime | None, tz) -> str:
    """N, first dismissal (< 2) — an immediate reschedule, not a cooldown,
    so render_dismissed()'s "for a while" wording would be wrong here;
    that one's kept for the second-dismissal/dormancy path only."""
    if next_fit_start is None:
        return "np, i'll keep an eye out for room."
    return f"np — i'll text you again {next_fit_start.astimezone(tz).strftime('%A')}."


def render_accepted(title: str, due_at: datetime) -> str:
    return f"📅 {title}\n{format_due_at(due_at)} — added to your calendar."


def render_dismissed() -> str:
    """Only reached on the second dismissal now (30d dormancy) — the
    first dismissal gets render_deferred() instead, since it reschedules
    immediately rather than cooling down."""
    return "Got it, I won't suggest that again for a while."


def render_snoozed() -> str:
    return "OK, I'll check back in about a week."
