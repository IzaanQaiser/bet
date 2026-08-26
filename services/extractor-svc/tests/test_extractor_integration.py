"""Integration test against the real Pub/Sub emulator — docs/engineering/
test-plan.md step 4. Only Gemini (_extract) is mocked; the publish() call
goes through the real emulator client, verified by pulling the message back
on a throwaway subscription.

Requires PUBSUB_EMULATOR_HOST and GCP_PROJECT_ID set (scripts/setup-emulator.sh
must have created the topics already). Skipped automatically otherwise.
"""

import base64
import json
import os
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(
    "PUBSUB_EMULATOR_HOST" not in os.environ or "GCP_PROJECT_ID" not in os.environ,
    reason="requires a live Pub/Sub emulator — see module docstring",
)


@pytest.fixture
def extracted_subscription():
    from google.cloud import pubsub_v1

    subscriber = pubsub_v1.SubscriberClient()
    project_id = os.environ["GCP_PROJECT_ID"]
    topic_path = subscriber.topic_path(project_id, "items-extracted")
    sub_name = f"test-extractor-integration-{uuid.uuid4().hex[:8]}"
    sub_path = subscriber.subscription_path(project_id, sub_name)
    subscriber.create_subscription(name=sub_path, topic=topic_path)
    yield subscriber, sub_path
    subscriber.delete_subscription(subscription=sub_path)


def test_raw_to_extracted_end_to_end(extracted_subscription):
    from extractor_svc.main import _ExtractionResult, app
    from obligation_engine_shared.schemas import RawItemMessage

    subscriber, sub_path = extracted_subscription
    client = TestClient(app)

    raw = RawItemMessage(
        item_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        text="pay rent by friday",
        received_at=datetime.now(UTC),
    )
    envelope = {"message": {"data": base64.b64encode(raw.model_dump_json().encode()).decode()}}

    result = _ExtractionResult(
        type="obligation",
        title="Pay rent",
        summary="Pay rent by Friday.",
        due_at=None,
        effort_minutes=15,
        confidence=0.95,
        missing_fields=["due_at"],
        reasoning="Deadline implied but ambiguous.",
    )

    with patch("extractor_svc.main._extract", new=AsyncMock(return_value=result)):
        resp = client.post("/pubsub/push", json=envelope)
    assert resp.status_code == 200

    pulled = subscriber.pull(subscription=sub_path, max_messages=1, timeout=10)
    assert len(pulled.received_messages) == 1
    published = json.loads(pulled.received_messages[0].message.data)
    assert published["item_id"] == str(raw.item_id)
    assert published["effort_minutes"] == 15
    assert published["due_at"] is None
    subscriber.acknowledge(
        subscription=sub_path, ack_ids=[m.ack_id for m in pulled.received_messages]
    )
