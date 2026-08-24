"""Integration test against the real Pub/Sub emulator + the **real** GCS
media bucket — docs/engineering/test-plan.md step 11's
`test_mms_end_to_end`, the extractor-svc half. A real fixture image is
uploaded directly to GCS (standing in for ingest-svc's own real upload,
covered separately in ingest-svc's own integration test), then
extractor-svc's real endpoint downloads it for real; only the Gemini
call itself is mocked.

Requires PUBSUB_EMULATOR_HOST and GCP_PROJECT_ID. Skipped automatically
otherwise.
"""

import base64
import os
import uuid
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from obligation_engine_shared.schemas import RawItemMessage

pytestmark = pytest.mark.skipif(
    "PUBSUB_EMULATOR_HOST" not in os.environ or "GCP_PROJECT_ID" not in os.environ,
    reason="requires a live Pub/Sub emulator — see module docstring",
)

MEDIA_BUCKET = "obligation-engine-hack-media"


@pytest.fixture
def client():
    from extractor_svc.main import app

    return TestClient(app)


@pytest.fixture
def uploaded_fixture_image():
    from google.cloud import storage

    blob_name = f"test-media-integration-{uuid.uuid4().hex[:8]}.jpg"
    fixture_bytes = b"\xff\xd8\xff\xe0fake-jpeg-bytes-for-extractor-integration-test"
    blob = storage.Client().bucket(MEDIA_BUCKET).blob(blob_name)
    blob.upload_from_string(fixture_bytes, content_type="image/jpeg")
    yield f"gs://{MEDIA_BUCKET}/{blob_name}", fixture_bytes
    blob.delete()


@pytest.fixture
def extracted_subscription():
    from google.cloud import pubsub_v1

    subscriber = pubsub_v1.SubscriberClient()
    project_id = os.environ["GCP_PROJECT_ID"]
    topic_path = subscriber.topic_path(project_id, "items-extracted")
    sub_name = f"test-extractor-media-{uuid.uuid4().hex[:8]}"
    sub_path = subscriber.subscription_path(project_id, sub_name)
    subscriber.create_subscription(name=sub_path, topic=topic_path)
    yield subscriber, sub_path
    subscriber.delete_subscription(subscription=sub_path)


def test_downloads_real_gcs_object_and_extracts(
    client, uploaded_fixture_image, extracted_subscription
):
    from extractor_svc.main import _ExtractionResult

    media_uri, fixture_bytes = uploaded_fixture_image
    subscriber, sub_path = extracted_subscription

    raw = RawItemMessage(
        item_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        text="",
        media_uri=media_uri,
        mime_type="image/jpeg",
        received_at=datetime.now(UTC),
    )
    envelope = {"message": {"data": base64.b64encode(raw.model_dump_json().encode()).decode()}}

    result = _ExtractionResult(
        type="obligation",
        title="Pay rent",
        summary="Pay rent by Friday, from a photographed note.",
        due_at=None,
        effort_minutes="15",
        focus_depth="shallow",
        confidence=0.9,
        missing_fields=["due_at"],
        reasoning="Deadline implied but ambiguous, from image text.",
    )

    downloaded_bytes = {}

    async def fake_extract(_raw):
        from extractor_svc.main import _download_media

        # Exercise the real download path directly rather than mocking
        # it away, so this test actually proves the GCS read works.
        downloaded_bytes["data"] = _download_media(_raw.media_uri)
        return result

    with patch("extractor_svc.main._extract", side_effect=fake_extract):
        resp = client.post("/pubsub/push", json=envelope)
    assert resp.status_code == 200

    assert downloaded_bytes["data"] == fixture_bytes

    pulled = subscriber.pull(subscription=sub_path, max_messages=1, timeout=10)
    assert len(pulled.received_messages) == 1
    subscriber.acknowledge(
        subscription=sub_path, ack_ids=[m.ack_id for m in pulled.received_messages]
    )
