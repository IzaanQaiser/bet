from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from obligation_engine_shared.schemas import (
    ConfirmedItemMessage,
    ExtractedItemMessage,
    RawItemMessage,
)
from pydantic import ValidationError


def test_raw_item_message_valid():
    RawItemMessage(
        item_id=uuid4(),
        user_id=uuid4(),
        media_uri=None,
        mime_type=None,
        text="pay rent by friday",
        received_at=datetime.now(UTC),
    )


def test_extracted_item_message_valid():
    ExtractedItemMessage(
        item_id=uuid4(),
        user_id=uuid4(),
        type="obligation",
        title="Pay rent",
        summary="Rent due Friday",
        due_at=datetime.now(UTC),
        effort_minutes=15,
        focus_depth="shallow",
        confidence=0.95,
        missing_fields=[],
        reasoning="Clear deadline stated.",
    )


def test_confirmed_item_message_valid():
    ConfirmedItemMessage(
        item_id=uuid4(),
        user_id=uuid4(),
        type="obligation",
        title="Pay rent",
        summary="Rent due Friday",
        due_at=datetime.now(UTC),
        effort_minutes=15,
        action_type="calendar",
        email_draft=None,
    )


def test_extracted_item_message_rejects_invalid_effort_minutes():
    with pytest.raises(ValidationError):
        ExtractedItemMessage(
            item_id=uuid4(),
            user_id=uuid4(),
            type="obligation",
            title="x",
            summary="x",
            due_at=None,
            effort_minutes=45,  # not one of the 5 allowed buckets
            focus_depth="shallow",
            confidence=0.5,
            missing_fields=[],
            reasoning="x",
        )


def test_confirmed_item_message_due_at_optional_for_latent():
    # Regression guard for the bug fixed in the cohesiveness pass
    # (docs/architecture/data-model.md §2.4) — a latent legitimately has no
    # due_at and flows through this same message shape.
    msg = ConfirmedItemMessage(
        item_id=uuid4(),
        user_id=uuid4(),
        type="latent",
        title="Rewrite the ingest pipeline in Rust",
        summary="An idea for later",
        due_at=None,
        effort_minutes=120,
        action_type=None,
        email_draft=None,
    )
    assert msg.due_at is None
    assert msg.action_type is None


def test_extracted_item_message_strips_due_at_tzinfo():
    # Regression guard for a real finding: Gemini is explicitly instructed
    # to emit due_at as a naive local wall-clock string, but a live
    # conversation still got one back with a fabricated -07:00 offset,
    # which committer-svc's "naive means already-local" check then
    # silently trusted, corrupting the real Calendar event it wrote.
    aware = datetime(2026, 8, 26, 15, 0, 0, tzinfo=UTC)
    msg = ExtractedItemMessage(
        item_id=uuid4(),
        user_id=uuid4(),
        type="obligation",
        title="x",
        summary="x",
        due_at=aware,
        effort_minutes=15,
        focus_depth="shallow",
        confidence=0.9,
        missing_fields=[],
        reasoning="x",
    )
    assert msg.due_at is not None
    assert msg.due_at.tzinfo is None
    # the wall-clock numbers are preserved as given, not converted
    assert msg.due_at == datetime(2026, 8, 26, 15, 0, 0)


def test_confirmed_item_message_strips_due_at_tzinfo():
    # -07:00, matching the real fabricated offset this regression is named after.
    fabricated_offset = datetime(2026, 8, 26, 15, 0, 0, tzinfo=timezone(timedelta(hours=-7)))
    msg = ConfirmedItemMessage(
        item_id=uuid4(),
        user_id=uuid4(),
        type="obligation",
        title="x",
        summary="x",
        due_at=fabricated_offset,
        effort_minutes=15,
        action_type="calendar",
        email_draft=None,
    )
    assert msg.due_at is not None
    assert msg.due_at.tzinfo is None
    assert msg.due_at == datetime(2026, 8, 26, 15, 0, 0)


def test_due_at_none_stays_none():
    msg = ConfirmedItemMessage(
        item_id=uuid4(),
        user_id=uuid4(),
        type="latent",
        title="x",
        summary="x",
        due_at=None,
        effort_minutes=15,
        action_type=None,
        email_draft=None,
    )
    assert msg.due_at is None


def test_confirmed_item_message_reminder_times_default_none():
    msg = ConfirmedItemMessage(
        item_id=uuid4(),
        user_id=uuid4(),
        type="obligation",
        title="x",
        summary="x",
        due_at=datetime(2026, 8, 26, 18, 0, 0),
        effort_minutes=60,
        action_type="calendar",
        email_draft=None,
    )
    assert msg.reminder_1_at is None
    assert msg.reminder_2_at is None


def test_confirmed_item_message_reminder_times_roundtrip():
    r1 = datetime(2026, 8, 26, 16, 0, 0)
    r2 = datetime(2026, 8, 26, 17, 0, 0)
    msg = ConfirmedItemMessage(
        item_id=uuid4(),
        user_id=uuid4(),
        type="obligation",
        title="x",
        summary="x",
        due_at=datetime(2026, 8, 26, 18, 0, 0),
        effort_minutes=60,
        action_type="calendar",
        email_draft=None,
        reminder_1_at=r1,
        reminder_2_at=r2,
    )
    roundtripped = ConfirmedItemMessage.model_validate_json(msg.model_dump_json())
    assert roundtripped.reminder_1_at == r1
    assert roundtripped.reminder_2_at == r2


@pytest.mark.parametrize(
    "model_cls,kwargs",
    [
        (
            RawItemMessage,
            dict(
                item_id=uuid4(),
                user_id=uuid4(),
                media_uri=None,
                mime_type=None,
                text="hello",
                received_at=datetime.now(UTC),
            ),
        ),
        (
            ExtractedItemMessage,
            dict(
                item_id=uuid4(),
                user_id=uuid4(),
                type="latent",
                title="x",
                summary="x",
                due_at=None,
                effort_minutes=30,
                focus_depth="deep",
                confidence=0.7,
                missing_fields=["due_at"],
                reasoning="x",
            ),
        ),
        (
            ConfirmedItemMessage,
            dict(
                item_id=uuid4(),
                user_id=uuid4(),
                type="obligation",
                title="x",
                summary="x",
                due_at=datetime.now(UTC),
                effort_minutes=60,
                action_type="email",
                email_draft="Dear team, ...",
            ),
        ),
    ],
)
def test_message_roundtrip(model_cls, kwargs):
    instance = model_cls(**kwargs)
    roundtripped = model_cls.model_validate_json(instance.model_dump_json())
    assert roundtripped == instance
