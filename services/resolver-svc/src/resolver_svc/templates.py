"""Deterministic SMS templates — agent-contracts.md §3.1/§3.3/§3.4. No LLM
call for any of this; every field is computed, never generated."""

from datetime import datetime


def format_due_at(due_at: datetime) -> str:
    """`Ddd D Mon, H:MM AM/PM` — e.g. `Thu 4 Sep, 2:00 PM`, agent-contracts.md §3.3."""
    return due_at.strftime("%a %-d %b, %-I:%M %p")


def render_confirmation_card(
    item_type: str,
    title: str,
    summary: str,
    due_at: datetime | None,
    effort_minutes: int,
    action_type: str | None,
    email_recipient: str | None = None,
    email_draft: str | None = None,
    thread_attach_title: str | None = None,
) -> str:
    # Email is a third variant, not a decoration on the obligation one
    # (agent-contracts.md §3.3, step 15) — checked first since due_at can
    # legitimately be None here (§2.1), which the obligation branch below
    # can't handle (format_due_at requires a real datetime). Never gets
    # the thread-attach suffix: an email obligation was never a latent,
    # so it never has a thread-attach candidate to offer.
    if action_type == "email":
        return (
            f"✉️ Email to {email_recipient}:\n\n"
            f"{email_draft}\n\n"
            f"Reply Y to send, N to cancel, or send a correction."
        )
    if item_type == "obligation":
        body = (
            f"📅 {title}\n"
            f"{format_due_at(due_at)} · {effort_minutes} min\n"
            f"Reply Y to confirm, N to cancel, or send a correction."
        )
    else:
        body = (
            f"💡 {title}\n"
            f"{summary} · {effort_minutes} min\n"
            f"Reply Y to confirm, N to cancel, or send a correction."
        )
    if thread_attach_title:
        body += f'\n\nAlso similar to "{thread_attach_title}" — reply A to attach as a follow-up.'
    return body


def render_dedupe_question(existing_title: str) -> str:
    return f'Is this the same as "{existing_title}"?\nReply Y to merge, N if it\'s different.'


def render_merged(existing_title: str) -> str:
    return f'Got it — that\'s the same as "{existing_title}". Nothing new added.'


def render_attached(thread_title: str) -> str:
    return f'Attached to "{thread_title}".'


def render_cancelled() -> str:
    return "Cancelled."


def render_needs_review(title: str) -> str:
    return (
        f"I couldn't get all the details for \"{title}\" — I've saved what I have. "
        f"Send it again with more detail if you'd like me to try again."
    )
