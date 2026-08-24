"""Unit tests — the Gemini call (_extract) and Pub/Sub publish are mocked
out, per docs/engineering/test-plan.md step 4. Envelope decoding, error
handling, and the string-to-int effort_minutes cast only."""

import base64
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient


def _push_envelope(raw_json: bytes) -> dict:
    return {"message": {"data": base64.b64encode(raw_json).decode()}}


@pytest.fixture
def client():
    from extractor_svc.main import app

    return TestClient(app)


def _raw_message_json(item_id, user_id) -> bytes:
    from obligation_engine_shared.schemas import RawItemMessage

    return (
        RawItemMessage(
            item_id=item_id,
            user_id=user_id,
            text="pay rent by friday",
            received_at=datetime.now(UTC),
        )
        .model_dump_json()
        .encode()
    )


def _extraction_result(**overrides):
    from extractor_svc.main import _ExtractionResult

    defaults = dict(
        type="obligation",
        title="Pay rent",
        summary="Pay rent by Friday.",
        due_at=None,
        effort_minutes="15",
        focus_depth="shallow",
        confidence=0.95,
        missing_fields=["due_at"],
        reasoning="Has an implied but ambiguous deadline.",
    )
    defaults.update(overrides)
    return _ExtractionResult(**defaults)


def test_valid_envelope_extracts_and_publishes(client):
    item_id, user_id = uuid4(), uuid4()
    body = _push_envelope(_raw_message_json(item_id, user_id))
    with (
        patch("extractor_svc.main._extract", new=AsyncMock(return_value=_extraction_result())),
        patch("extractor_svc.main.publish") as mock_publish,
    ):
        resp = client.post("/pubsub/push", json=body)
    assert resp.status_code == 200
    assert resp.json() == {"status": "extracted", "item_id": str(item_id)}
    topic, published = mock_publish.call_args[0]
    assert topic == "items-extracted"
    assert published.item_id == item_id
    assert published.effort_minutes == 15  # cast from wire string "15"


def test_malformed_envelope_returns_500_for_retry(client):
    with patch("extractor_svc.main.publish") as mock_publish:
        resp = client.post("/pubsub/push", json={"message": {"data": "not-valid-base64json"}})
    assert resp.status_code == 500
    mock_publish.assert_not_called()


def test_extraction_failure_returns_500_for_retry(client):
    item_id, user_id = uuid4(), uuid4()
    body = _push_envelope(_raw_message_json(item_id, user_id))
    with (
        patch("extractor_svc.main._extract", new=AsyncMock(side_effect=RuntimeError("boom"))),
        patch("extractor_svc.main.publish") as mock_publish,
    ):
        resp = client.post("/pubsub/push", json=body)
    assert resp.status_code == 500
    mock_publish.assert_not_called()


def test_publish_failure_returns_500_for_retry(client):
    item_id, user_id = uuid4(), uuid4()
    body = _push_envelope(_raw_message_json(item_id, user_id))
    with (
        patch("extractor_svc.main._extract", new=AsyncMock(return_value=_extraction_result())),
        patch("extractor_svc.main.publish", side_effect=RuntimeError("pubsub down")),
    ):
        resp = client.post("/pubsub/push", json=body)
    assert resp.status_code == 500


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
