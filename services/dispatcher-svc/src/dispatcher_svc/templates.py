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


def render_reminder_early(title: str, due_at: datetime, effort_minutes: int, today: date) -> str:
    """Fires at due_at - 2*effort — the early heads-up, not yet urgent."""
    return (
        f"⏰ heads up — {title} is due {relative_due_description(due_at.date(), today)}, "
        f"{format_due_at(due_at)}.\n"
        f"Block off ~{_format_block_hours(effort_minutes)} for it soon."
    )


def render_reminder_final(title: str, due_at: datetime, effort_minutes: int, today: date) -> str:
    """Fires at due_at - effort — the start-by, last-call reminder: about
    exactly enough time left to do the work, no more."""
    return (
        f"⏰ last call — {title} is due {relative_due_description(due_at.date(), today)}, "
        f"{format_due_at(due_at)}.\n"
        f"About {_format_block_hours(effort_minutes)} left if you start now."
    )


def _time_of_day_phrase(block_start_hour: int) -> str:
    if block_start_hour < 12:
        return "in the morning"
    if block_start_hour < 17:
        return "in the afternoon"
    return "in the evening"


def evidence_line(
    booked_today: int, trailing_booked_minutes: list[int], load_delta: float | None
) -> str | None:
    """capacity-engine.md §6's two-tier rule, superlative before generic,
    omitted rather than a weak claim if neither is true."""
    if trailing_booked_minutes and booked_today == min(trailing_booked_minutes):
        return "lightest day you've had in two weeks"
    if load_delta is not None and load_delta < -0.15:
        return "lighter than usual"
    return None


def render_suggestion(
    day_name: str,
    block_start_hour: int,
    block_minutes: int,
    booked_today: int,
    trailing_booked_minutes: list[int],
    load_delta: float | None,
    item_title: str,
    days_since_capture: int,
) -> str:
    evidence = evidence_line(booked_today, trailing_booked_minutes, load_delta)
    opening = (
        f"{day_name} looks open — {_format_block_hours(block_minutes)} clear "
        f"{_time_of_day_phrase(block_start_hour)}"
    )
    opening += f",\n{evidence}." if evidence else "."
    return (
        f"{opening}\n\n"
        f'💡 "{item_title}"\n'
        f"   (you mentioned this {days_since_capture} days ago)\n\n"
        f"Want it on the calendar? Y / N / Later"
    )


def render_accepted(title: str, due_at: datetime) -> str:
    return f"📅 {title}\n{format_due_at(due_at)} — added to your calendar."


def render_dismissed() -> str:
    return "Got it, I won't suggest that again for a while."


def render_snoozed() -> str:
    return "OK, I'll check back in about a week."
