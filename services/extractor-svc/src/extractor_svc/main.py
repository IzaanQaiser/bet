"""extractor-svc — raw multimodal input to structured JSON (docs/architecture/
agent-contracts.md §2). Step 11 adds real media handling: an image/PDF's
gs:// URI on RawItemMessage gets downloaded and passed to Gemini as an
inline Part alongside (or instead of) the SMS text.

Zero DB access, zero Calendar/Gmail scope (ADR 0003) — this is the one service
in the whole system that ever touches untrusted, unconfirmed user input, and
it can write to nothing but the items.extracted topic.
"""

import json
import logging
import os
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.cloud import storage
from google.genai import types
from obligation_engine_shared.pubsub import decode_push_envelope, publish
from obligation_engine_shared.schemas import ExtractedItemMessage, RawItemMessage
from pydantic import BaseModel, Field

logger = logging.getLogger("extractor_svc")
app = FastAPI()


# Gemini's structured-output schema only supports string enum values, not
# integer ones (Literal[15, ...] fails Vertex AI schema validation outright —
# found empirically, see agent-contracts.md §2's "Resolved gap"). This is the
# wire schema the model actually fills in; effort_minutes is cast to int
# below when building the real ExtractedItemMessage.
class _ExtractionResult(BaseModel):
    type: Literal["obligation", "latent"]
    title: str
    summary: str
    due_at: str | None = None
    effort_minutes: Literal["15", "30", "60", "120", "240"]
    focus_depth: Literal["shallow", "deep"]
    confidence: float = Field(ge=0.0, le=1.0)
    missing_fields: list[str]
    reasoning: str


_SYSTEM_PROMPT = """You are the extraction stage of a personal obligation-tracking system. You
are given a message a user sent via SMS — text, and optionally an attached
image or PDF (a screenshot, a scanned letter, a photo of a note). Extract
exactly one structured item from it.

Rules:
- Classify as "obligation" if it has or implies a deadline; otherwise
  "latent" (an idea, a project, an intention with no deadline).
- Never invent a due_at. If a date is implied but ambiguous ("next week",
  "soon"), leave due_at null and add "due_at" to missing_fields — do not
  guess a specific date.
- effort_minutes must be exactly one of "15", "30", "60", "120", "240"
  (as a string) — pick the closest realistic bucket. Never output any other
  value.
- focus_depth is "deep" if the task needs one uninterrupted stretch of
  concentration (writing, coding, focused analysis); "shallow" if it can be
  done in short pieces or is administrative/low-cognitive-load (a phone
  call, filling a form, paying a bill).
- confidence reflects your overall certainty about the classification and
  fields, not any single field in isolation.
- reasoning is one sentence explaining the classification, for logs only —
  it is never shown to the user.

Output must conform exactly to the provided schema. No text outside it.
"""

_agent = LlmAgent(
    name="extractor",
    model=os.environ.get("GEMINI_MODEL", "gemini-3.5-flash"),
    instruction=_SYSTEM_PROMPT,
    output_schema=_ExtractionResult,
)
_session_service = InMemorySessionService()


def _download_media(media_uri: str) -> bytes:
    if not media_uri.startswith("gs://"):
        raise ValueError(f"unsupported media_uri scheme: {media_uri!r}")
    bucket_name, _, blob_name = media_uri.removeprefix("gs://").partition("/")
    return storage.Client().bucket(bucket_name).blob(blob_name).download_as_bytes()


async def _extract(raw: RawItemMessage) -> _ExtractionResult:
    session_id = str(raw.item_id)
    await _session_service.create_session(
        app_name="extractor", user_id=str(raw.user_id), session_id=session_id
    )
    runner = Runner(app_name="extractor", agent=_agent, session_service=_session_service)

    parts = [types.Part(text=raw.text or "")]
    if raw.media_uri:
        media_bytes = _download_media(raw.media_uri)
        parts.append(types.Part.from_bytes(data=media_bytes, mime_type=raw.mime_type))
    message = types.Content(role="user", parts=parts)

    final_text = None
    async for event in runner.run_async(
        user_id=str(raw.user_id), session_id=session_id, new_message=message
    ):
        if event.is_final_response() and event.content and event.content.parts:
            final_text = event.content.parts[-1].text

    if final_text is None:
        raise RuntimeError("Gemini produced no final response")
    return _ExtractionResult.model_validate(json.loads(final_text))


@app.post("/pubsub/push")
async def pubsub_push(request: Request):
    envelope = await request.json()
    try:
        raw = decode_push_envelope(envelope, RawItemMessage)
    except Exception:
        logger.exception("malformed push envelope, could not decode RawItemMessage")
        # Internal corruption (we control the publisher), not untrusted
        # external input — let Pub/Sub retry rather than silently drop it.
        raise HTTPException(status_code=500, detail="malformed envelope") from None

    try:
        result = await _extract(raw)
    except Exception:
        logger.exception("extraction failed item_id=%s", raw.item_id)
        raise HTTPException(status_code=500, detail="extraction failed") from None

    extracted = ExtractedItemMessage(
        item_id=raw.item_id,
        user_id=raw.user_id,
        type=result.type,
        title=result.title,
        summary=result.summary,
        due_at=result.due_at,
        effort_minutes=int(result.effort_minutes),
        focus_depth=result.focus_depth,
        confidence=result.confidence,
        missing_fields=result.missing_fields,
        reasoning=result.reasoning,
    )

    try:
        publish("items-extracted", extracted)
    except Exception:
        logger.exception("publish failed item_id=%s", raw.item_id)
        raise HTTPException(status_code=500, detail="publish failed") from None

    logger.info("extracted item_id=%s type=%s", raw.item_id, extracted.type)
    return {"status": "extracted", "item_id": str(raw.item_id)}


@app.get("/health")
async def health():
    return {"status": "ok"}
