"""Deterministic SMS templates — agent-contracts.md §3.3/§3.4. No LLM
call for any of this; every field is computed, never generated.

Thread-attach suffix (§3.3, offered when a 0.82-0.92 similarity latent
match exists) is deliberately not implemented here — that needs the
embedding search dedupe isn't built until step 12."""

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
) -> str:
    if item_type == "obligation":
        icon = "✉️" if action_type == "email" else "📅"
        return (
            f"{icon} {title}\n"
            f"{format_due_at(due_at)} · {effort_minutes} min\n"
            f"Reply Y to confirm, N to cancel, or send a correction."
        )
    return (
        f"💡 {title}\n"
        f"{summary} · {effort_minutes} min\n"
        f"Reply Y to confirm, N to cancel, or send a correction."
    )


def render_cancelled() -> str:
    return "Cancelled."
