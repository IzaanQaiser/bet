"""docs/engineering/test-plan.md step 11 — extractor-svc's side of
multimodal ingest. GCS and the ADK Runner are both mocked; verified
against real Gemini separately (a synthetic test image, see
docs/product/status.md's step 11 notes) since mocks can't validate real
multimodal model behavior."""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from obligation_engine_shared.schemas import RawItemMessage


def _mock_final_event(payload: dict):
    event = MagicMock()
    event.is_final_response.return_value = True
    event.content.parts = [MagicMock(text=json.dumps(payload))]
    return event


_PAYLOAD = {
    "type": "obligation",
    "title": "Pay rent",
    "summary": "Pay rent by Friday, $1450.",
    "due_at": None,
    "effort_minutes": "15",
    "focus_depth": "shallow",
    "confidence": 0.95,
    "missing_fields": ["due_at"],
    "reasoning": "Deadline implied but ambiguous.",
}


@pytest.mark.asyncio
async def test_media_downloaded_and_passed_as_inline_part():
    from extractor_svc.main import _extract

    raw = RawItemMessage(
        item_id=uuid4(),
        user_id=uuid4(),
        text="",
        media_uri="gs://obligation-engine-hack-media/some-item.jpg",
        mime_type="image/jpeg",
        received_at=datetime.now(UTC),
    )

    captured_message = {}

    async def fake_run_async(**kwargs):
        captured_message["content"] = kwargs["new_message"]
        yield _mock_final_event(_PAYLOAD)

    mock_bucket = MagicMock()
    mock_bucket.blob.return_value.download_as_bytes.return_value = b"fake-image-bytes"
    mock_storage_client = MagicMock()
    mock_storage_client.bucket.return_value = mock_bucket

    with (
        patch("extractor_svc.main._session_service.create_session", new=AsyncMock()),
        patch("extractor_svc.main.Runner") as mock_runner_cls,
        patch("extractor_svc.main.storage.Client", return_value=mock_storage_client),
    ):
        mock_runner_cls.return_value.run_async = fake_run_async
        result = await _extract(raw)

    mock_storage_client.bucket.assert_called_once_with("obligation-engine-hack-media")
    mock_bucket.blob.assert_called_once_with("some-item.jpg")

    content = captured_message["content"]
    assert len(content.parts) == 2  # text part (empty) + inline media part
    assert result.type == "obligation"


@pytest.mark.asyncio
async def test_text_only_message_sends_single_part_no_gcs_call():
    from extractor_svc.main import _extract

    raw = RawItemMessage(
        item_id=uuid4(), user_id=uuid4(), text="pay rent by friday", received_at=datetime.now(UTC)
    )
    captured_message = {}

    async def fake_run_async(**kwargs):
        captured_message["content"] = kwargs["new_message"]
        yield _mock_final_event(_PAYLOAD)

    with (
        patch("extractor_svc.main._session_service.create_session", new=AsyncMock()),
        patch("extractor_svc.main.Runner") as mock_runner_cls,
        patch("extractor_svc.main.storage.Client") as mock_storage_client,
    ):
        mock_runner_cls.return_value.run_async = fake_run_async
        await _extract(raw)

    mock_storage_client.assert_not_called()
    assert len(captured_message["content"].parts) == 1


def test_download_media_rejects_non_gs_uri():
    from extractor_svc.main import _download_media

    with pytest.raises(ValueError, match="unsupported media_uri scheme"):
        _download_media("https://example.com/not-gcs.jpg")
