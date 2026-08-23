from datetime import UTC, datetime
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
