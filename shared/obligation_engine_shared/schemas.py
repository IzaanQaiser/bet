"""Pub/Sub message schemas — the literal source of truth for the topic
contracts described in docs/architecture/agent-contracts.md §1. If this file
and that doc ever disagree, this file is buggy, not the doc."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class RawItemMessage(BaseModel):
    """items.raw — published by ingest-svc."""

    item_id: UUID
    user_id: UUID
    media_uri: str | None = None  # GCS URI, null if text-only
    mime_type: str | None = None  # null if text-only
    text: str | None = None  # the SMS body, if any
    received_at: datetime


class ExtractedItemMessage(BaseModel):
    """items.extracted — published by extractor-svc."""

    item_id: UUID
    user_id: UUID
    type: Literal["obligation", "latent"]
    title: str
    summary: str
    due_at: datetime | None
    effort_minutes: Literal[15, 30, 60, 120, 240]
    focus_depth: Literal["shallow", "deep"]
    confidence: float = Field(ge=0.0, le=1.0)
    missing_fields: list[str]
    reasoning: str  # log-only, never shown to the user
    action_type: Literal["calendar", "email"] = "calendar"  # step 15, agent-contracts.md §2.1
    email_recipient: str | None = None  # a real address, never a guessed name
    email_draft: str | None = None  # set only when action_type == "email"


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
    effort_minutes: Literal[15, 30, 60, 120, 240]
    action_type: Literal["calendar", "email"] | None = None
    email_recipient: str | None = None  # step 15 — carries a resolved recipient to committer-svc
    email_draft: str | None = None


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
