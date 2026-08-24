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


def test_email_variant_shows_recipient_and_full_draft():
    """agent-contracts.md §3.3, step 15 — a third variant, not a decoration
    on the obligation card: the draft body itself must be shown, not just
    a title."""
    body = render_confirmation_card(
        "obligation",
        "Reply to Sarah",
        "Confirm the delay.",
        None,  # due_at legitimately absent for an email action (§2.1)
        15,
        "email",
        email_recipient="sarah@example.com",
        email_draft="Hi Sarah,\n\nConfirming the delay.\n\nThanks",
    )
    assert body == (
        "✉️ Email to sarah@example.com:\n\n"
        "Hi Sarah,\n\nConfirming the delay.\n\nThanks\n\n"
        "Reply Y to send, N to cancel, or send a correction."
    )


def test_email_variant_ignores_due_at_and_thread_attach():
    """No date line even when due_at is present (it's context inside the
    draft, not a send time — §2.1); no thread-attach suffix either, since
    an email obligation was never a latent."""
    body = render_confirmation_card(
        "obligation",
        "Reply to Sarah",
        "Confirm the delay.",
        datetime(2026, 9, 4, 14, 0),
        15,
        "email",
        email_recipient="sarah@example.com",
        email_draft="Hi Sarah,\n\nConfirming.\n\nThanks",
        thread_attach_title="Some latent",
    )
    assert "Sep" not in body
    assert "attach" not in body


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
