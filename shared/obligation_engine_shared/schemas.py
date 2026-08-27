"""Pub/Sub message schemas — the literal source of truth for the topic
contracts described in docs/architecture/agent-contracts.md §1. If this file
and that doc ever disagree, this file is buggy, not the doc."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


def _strip_due_at_tzinfo(v: datetime | None) -> datetime | None:
    """Gemini is explicitly instructed (agent-contracts.md's due_at rule,
    conversation.py's clarification prompt) to emit due_at as a naive
    local wall-clock string — no UTC offset, ever — since committer-svc
    treats a naive due_at as already being in the user's own timezone
    and attaches that zone itself (agent-contracts.md §1's "Resolved
    gap" note). Real-world finding, not theoretical: a live conversation
    still got back a due_at with a fabricated "-07:00" offset despite
    the explicit instruction, which committer-svc's `if due_at.tzinfo is
    None` check then silently skipped — the offset rode straight into
    the real Calendar event's dateTime, contradicting the correct
    America/Toronto timeZone label sent alongside it in the same call.
    Stripping any tzinfo here keeps the wall-clock numbers exactly as
    given and enforces "always local, always naive" by construction,
    rather than trusting every model response to honor the prompt.
    """
    if v is not None and v.tzinfo is not None:
        return v.replace(tzinfo=None)
    return v


class RawItemMessage(BaseModel):
    """items.raw — published by ingest-svc."""

    item_id: UUID
    user_id: UUID
    media_uri: str | None = None  # GCS URI, null if text-only
    mime_type: str | None = None  # null if text-only
    text: str | None = None  # the SMS body, if any
    received_at: datetime


class ExtractedItemMessage(BaseModel):
    """items.extracted — published by extractor-svc.

    Phase G step B (agent-contracts.md §2.2): is_actionable is the leading
    triage field. When false, the message is pure chat (banter/greeting/
    reaction/question) with nothing to capture — type/title/summary/due_at/
    effort_minutes/confidence are all null, missing_fields is empty. When
    true, every other field is filled exactly as before this step.

    raw_text is a plain passthrough of RawItemMessage.text, not a model
    output — carried through so resolver-svc's chat handler has the
    original message to react to with real conversation history/context
    (its own converse() call, is_chat mode), replacing an earlier
    chat_reply field that extractor-svc generated with zero history
    available to it (a real bug: a casual "betski" sent right after a
    task auto-committed got a context-blind "hey! what's up?" back)."""

    item_id: UUID
    user_id: UUID
    is_actionable: bool = True
    raw_text: str | None = None
    type: Literal["obligation", "latent"] | None = None
    title: str | None = None
    summary: str | None = None
    due_at: datetime | None = None
    # Bucketed (15/30/60/120/240) for a task, per extractor-svc's own
    # prompt convention — never asked about, always silently guessed. Not
    # bucketed at the schema level anymore: a scheduled event's duration
    # is asked for directly and must carry the user's exact stated number
    # (migrations/0016 — a real, visible bug found live: "1.5 hours"
    # rounding to a 2-hour Calendar event). Sanity-bounded, not enum-bound.
    effort_minutes: int | None = Field(default=None, gt=0, le=1440)
    # A task with a completion deadline ("assignment due at 6pm" — remind
    # strictly before it, never at it) vs. a scheduled event you attend at
    # a specific time ("meeting at 8:39pm" — the reminder that matters
    # most is the one AT that time). Defaults false: every ambiguous case
    # keeps the deadline-style behavior, only a real "attend this" signal
    # opts into event-style reminders.
    is_scheduled_event: bool = False
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    missing_fields: list[str] = Field(default_factory=list)
    reasoning: str = ""  # log-only, never shown to the user
    action_type: Literal["calendar", "email"] = "calendar"  # step 15, agent-contracts.md §2.1
    email_recipient: str | None = None  # a real address, never a guessed name
    email_draft: str | None = None  # set only when action_type == "email"

    _strip_due_at_tz = field_validator("due_at")(_strip_due_at_tzinfo)


class ConfirmedItemMessage(BaseModel):
    """items.confirmed — published by resolver-svc (normal path) or
    dispatcher-svc (accepted-suggestion path, state-machine.md §2.3).

    due_at/action_type/email_draft are Optional, not required: a latent
    flows through this same message shape and legitimately has none of
    them. See agent-contracts.md §1's "Resolved bug" note. due_at may
    also be null for an email-type obligation (agent-contracts.md §2.1 —
    it's context inside the draft, not a send time).
    """

    item_id: UUID
    user_id: UUID
    type: Literal["obligation", "latent"]
    title: str
    summary: str
    due_at: datetime | None = None
    effort_minutes: int = Field(gt=0, le=1440)  # exact minutes, see ExtractedItemMessage's note
    action_type: Literal["calendar", "email"] | None = None
    email_recipient: str | None = None  # step 15 — carries a resolved recipient to committer-svc
    email_draft: str | None = None
    # Single time-of reminder — resolver-svc sets this to due_at itself
    # once known (v1 simplification: was a two-stage due_at-30min/due_at
    # pair, collapsed to just due_at; the 30-minute lead now lives only in
    # the Calendar event's own native popup reminder, committer-svc's
    # _write_calendar_event/_create_placeholder_event). Null whenever
    # due_at isn't applicable (a latent, an email action with no due
    # date). committer-svc persists it as-is; dispatcher-svc fires it.
    reminder_at: datetime | None = None

    _strip_due_at_tz = field_validator("due_at")(_strip_due_at_tzinfo)


class RoutedReplyMessage(BaseModel):
    """Internal ingest-svc -> resolver-svc call, state-machine.md §4 — a
    synchronous service-to-service HTTP call, not a Pub/Sub topic, but
    still a message crossing a service boundary, so it gets the same
    shared-schema treatment as the three above. ingest-svc has already
    done the "is there an open conversation for this user" lookup by the
    time this is sent; item_id is that lookup's result, not re-derived by
    resolver-svc."""

    user_id: UUID
    item_id: UUID
    text: str


class PlaceholderUpsertRequest(BaseModel):
    """Internal dispatcher-svc -> committer-svc call (ADR 0009) — the
    second synchronous service-to-service asymmetry in this codebase,
    same treatment as RoutedReplyMessage above. dispatcher-svc computes
    where a latent's [idea]-tagged placeholder belongs; committer-svc,
    the only service with Calendar write credentials, is the one that
    actually calls the API. existing_event_id=None means create; a real
    id means try to move that event in place, falling back to create if
    it 404s (the user deleted it by hand)."""

    user_id: UUID
    title: str
    start: datetime  # naive local wall-clock, same convention as ConfirmedItemMessage.due_at
    effort_minutes: int = Field(gt=0, le=1440)
    existing_event_id: str | None = None

    _strip_start_tz = field_validator("start")(_strip_due_at_tzinfo)
