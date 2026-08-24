"""docs/engineering/test-plan.md step 9 — pure template rendering,
agent-contracts.md §3.3/§3.4. No I/O."""

from datetime import datetime

from resolver_svc.templates import render_cancelled, render_confirmation_card


def test_obligation_variant_includes_date_and_duration():
    body = render_confirmation_card(
        "obligation",
        "Pay rent",
        "Pay rent by Friday.",
        datetime(2026, 9, 4, 14, 0),
        15,
        "calendar",
    )
    assert body == (
        "📅 Pay rent\n"
        "Fri 4 Sep, 2:00 PM · 15 min\n"
        "Reply Y to confirm, N to cancel, or send a correction."
    )


def test_obligation_variant_uses_email_icon_for_email_action():
    body = render_confirmation_card(
        "obligation",
        "Send invoice",
        "Send invoice to client.",
        datetime(2026, 9, 4, 14, 0),
        15,
        "email",
    )
    assert body.startswith("✉️ Send invoice\n")


def test_latent_variant_has_no_date_line():
    body = render_confirmation_card("latent", "Learn pottery", "Someday, no rush.", None, 120, None)
    assert body == (
        "💡 Learn pottery\n"
        "Someday, no rush. · 120 min\n"
        "Reply Y to confirm, N to cancel, or send a correction."
    )
    assert "Fri" not in body and "Sep" not in body  # no date formatting attempted


def test_cancelled_message():
    assert render_cancelled() == "Cancelled."
