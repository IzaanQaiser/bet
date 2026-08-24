"""Unit test for _extract's parsing of the ADK Runner's event stream. The
Runner/LlmAgent themselves are mocked — verified against the real Gemini
API separately (docs/architecture/agent-contracts.md §2's "Resolved gap"
notes); this only checks that _extract correctly locates the structured
JSON in event.content.parts[-1].text (event.output is None even with
output_schema set, per that same finding) and validates it."""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest


def _mock_final_event(payload: dict):
    event = MagicMock()
    event.is_final_response.return_value = True
    event.content.parts = [MagicMock(text=json.dumps(payload))]
    return event


@pytest.mark.asyncio
async def test_extract_parses_final_event_text():
    from extractor_svc.main import _extract
    from obligation_engine_shared.schemas import RawItemMessage

    raw = RawItemMessage(
        item_id=uuid4(),
        user_id=uuid4(),
        text="pay rent by friday",
        received_at=datetime.now(UTC),
    )
    payload = {
        "type": "obligation",
        "title": "Pay rent",
        "summary": "Pay rent by Friday.",
        "due_at": None,
        "effort_minutes": "15",
        "focus_depth": "shallow",
        "confidence": 0.95,
        "missing_fields": ["due_at"],
        "reasoning": "Deadline implied but ambiguous.",
    }

    async def fake_run_async(**kwargs):
        yield _mock_final_event(payload)

    with (
        patch("extractor_svc.main._session_service.create_session", new=AsyncMock()),
        patch("extractor_svc.main.Runner") as mock_runner_cls,
    ):
        mock_runner_cls.return_value.run_async = fake_run_async
        result = await _extract(raw)

    assert result.type == "obligation"
    assert result.due_at is None  # ambiguous date never guessed
    assert result.missing_fields == ["due_at"]
    assert result.effort_minutes == "15"  # still a string at this layer


@pytest.mark.asyncio
async def test_extract_raises_if_no_final_response():
    from extractor_svc.main import _extract
    from obligation_engine_shared.schemas import RawItemMessage

    raw = RawItemMessage(item_id=uuid4(), user_id=uuid4(), text="hi", received_at=datetime.now(UTC))

    async def fake_run_async(**kwargs):
        return
        yield  # pragma: no cover - makes this an async generator

    with (
        patch("extractor_svc.main._session_service.create_session", new=AsyncMock()),
        patch("extractor_svc.main.Runner") as mock_runner_cls,
    ):
        mock_runner_cls.return_value.run_async = fake_run_async
        with pytest.raises(RuntimeError):
            await _extract(raw)
