"""docs/engineering/test-plan.md step 8 — deterministic template rendering,
agent-contracts.md §4.1/§4.2. No I/O."""

from datetime import date, datetime

from dispatcher_svc.templates import (
    evidence_line,
    relative_due_description,
    render_event_reminder_early,
    render_event_reminder_start,
    render_reminder_early,
    render_reminder_final,
    render_suggestion,
)


def test_relative_due_description_today_tomorrow_and_n_days():
    today = date(2026, 8, 27)
    assert relative_due_description(date(2026, 8, 27), today) == "today"
    assert relative_due_description(date(2026, 8, 26), today) == "today"  # already past
    assert relative_due_description(date(2026, 8, 28), today) == "tomorrow"
    assert relative_due_description(date(2026, 8, 30), today) == "in 3 days"


def test_render_reminder_early_exact_format():
    due = datetime(2026, 9, 4, 14, 0)  # a real Friday
    body = render_reminder_early("Pay rent", due, 120, today=date(2026, 9, 3))
    assert body == (
        "⏰ heads up — Pay rent is due tomorrow, Fri 4 Sep, 2:00 PM.\n"
        "Block off ~2h for it soon."
    )


def test_render_reminder_final_exact_format():
    due = datetime(2026, 9, 4, 14, 0)
    body = render_reminder_final("Pay rent", due, 90, today=date(2026, 9, 4))
    assert body == (
        "⏰ last call — Pay rent is due today, Fri 4 Sep, 2:00 PM.\n"
        "About 1h 30min left if you start now."
    )


def test_render_event_reminder_early_exact_format():
    due = datetime(2026, 8, 25, 20, 39)
    body = render_event_reminder_early("Meeting", due, today=date(2026, 8, 25))
    assert body == "⏰ heads up — Meeting starts today, Tue 25 Aug, 8:39 PM."


def test_render_event_reminder_start_exact_format():
    due = datetime(2026, 8, 25, 20, 39)
    body = render_event_reminder_start("Meeting", due, today=date(2026, 8, 25))
    assert body == "⏰ Meeting is starting now — Tue 25 Aug, 8:39 PM."


def test_suggestion_text_superlative_branch():
    line = evidence_line(
        booked_today=210, trailing_booked_minutes=[300] * 13 + [210], load_delta=-0.30
    )
    assert line == "lightest day you've had in two weeks"


def test_suggestion_text_lighter_than_usual_branch():
    # Not the minimum of the trailing window, but still meaningfully lighter.
    line = evidence_line(
        booked_today=250, trailing_booked_minutes=[300, 300, 200], load_delta=-0.20
    )
    assert line == "lighter than usual"


def test_suggestion_text_omitted_branch():
    line = evidence_line(
        booked_today=290, trailing_booked_minutes=[300, 300, 100], load_delta=-0.03
    )
    assert line is None


def test_render_suggestion_full_worked_example():
    """capacity-engine.md §6's scenario, rendered exactly per
    agent-contracts.md §4.2."""
    body = render_suggestion(
        day_name="Thursday",
        block_start_hour=12,
        block_minutes=180,
        booked_today=210,
        trailing_booked_minutes=[300] * 14,
        load_delta=-0.30,
        item_title="Rewrite the ingest pipeline in Rust",
        days_since_capture=18,
    )
    assert body == (
        "Thursday looks open — 3h clear in the afternoon,\n"
        "lighter than usual.\n\n"
        '💡 "Rewrite the ingest pipeline in Rust"\n'
        "   (you mentioned this 18 days ago)\n\n"
        "Want it on the calendar? Y / N / Later"
    )


def test_render_suggestion_omitted_evidence_has_no_second_clause():
    body = render_suggestion(
        day_name="Monday",
        block_start_hour=9,
        block_minutes=90,
        booked_today=290,
        trailing_booked_minutes=[300, 300, 100],
        load_delta=-0.03,
        item_title="Read the Rust book",
        days_since_capture=10,
    )
    assert body.startswith("Monday looks open — 1h 30min clear in the morning.\n\n")
    assert "lighter" not in body
    assert "lightest" not in body


def test_render_suggestion_time_of_day_phrases():
    morning = render_suggestion("Mon", 9, 60, 100, [200], None, "x", 5)
    afternoon = render_suggestion("Mon", 14, 60, 100, [200], None, "x", 5)
    evening = render_suggestion("Mon", 18, 60, 100, [200], None, "x", 5)
    assert "in the morning" in morning
    assert "in the afternoon" in afternoon
    assert "in the evening" in evening
