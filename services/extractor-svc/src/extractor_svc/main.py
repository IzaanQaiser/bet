"""extractor-svc — raw multimodal input to structured JSON (docs/architecture/
agent-contracts.md §2). Step 11 adds real media handling: an image/PDF's
gs:// URI on RawItemMessage gets downloaded and passed to Gemini as an
inline Part alongside (or instead of) the SMS text.

Step 15 (ADR 0008, agent-contracts.md §2.1) adds email-action classification
and drafting to this same call — action_type/email_recipient/email_draft —
rather than a third LLM call site (agent-contracts.md §0's "exactly two call
sites"). An email-intent message with no real address in the text never gets
a guessed recipient: "email_recipient" joins missing_fields instead, same
pattern as an ambiguous due_at.

Phase G step B (agent-contracts.md §2.2) adds a leading is_actionable triage
flag to this same call: pure chat (banter/greeting/reaction/question) gets
is_actionable=False and an in-voice chat_reply, with every extraction field
left null — no fake obligation gets invented out of "hello". resolver-svc
decides what to actually do with that (send chat_reply, mark the item
CHATTED) since this service still has zero Twilio/DB access (ADR 0003).

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
    is_actionable: bool = True
    chat_reply: str | None = None
    type: Literal["obligation", "latent"] | None = None
    title: str | None = None
    summary: str | None = None
    due_at: str | None = None
    effort_minutes: Literal["15", "30", "60", "120", "240"] | None = None
    focus_depth: Literal["shallow", "deep"] | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    missing_fields: list[str] = Field(default_factory=list)
    reasoning: str = ""
    action_type: Literal["calendar", "email"] = "calendar"
    email_recipient: str | None = None
    email_draft: str | None = None


_SYSTEM_PROMPT = """You are the extraction stage of a personal obligation-tracking system. You
are given a message a user sent via SMS — text, and optionally an attached
image or PDF (a screenshot, a scanned letter, a photo of a note). The user
texts like they'd text a friend, not like they're filling out a form.

First decide is_actionable:
- false if the message is pure chat with nothing to remember or schedule —
  a greeting, banter, a reaction, a question about the system itself, "you
  there?", etc. In this case leave type/title/summary/due_at/effort_minutes/
  focus_depth/confidence null and missing_fields empty, and set chat_reply
  to a short, casual, in-voice reply reacting to what they actually said —
  lowercase, terse, a little slang is fine, like a real friend texting back.
  Never invent an obligation or idea out of plain chat.
- true if the message describes a real obligation or idea worth capturing.
  In this case leave chat_reply null and fill every field below normally.

Extract exactly one structured item when is_actionable is true.

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
- action_type is "email" only if the message is unambiguously asking to send
  an email (e.g. "email X about...", "send Sarah an email saying...") AND
  the message itself contains a literal, syntactically valid email address
  for the recipient. Otherwise action_type is "calendar" — this covers
  every non-email obligation, which is most of them.
- If the message is clearly email-intent but no valid address is present
  (e.g. "email Sarah about the delay" — a name, not an address), still set
  action_type to "email", leave email_recipient null, and add
  "email_recipient" to missing_fields. Never guess an address from a name.
- Whenever action_type is "email", classify type as "obligation" even if no
  deadline is present or implied — sending a message is an immediate action
  someone asked for, not a someday idea. If the message implies no deadline
  at all, leave due_at null and do NOT add "due_at" to missing_fields —
  there is nothing to ask about. Only add "due_at" to missing_fields for an
  email action if a date is implied but genuinely ambiguous, same as any
  other obligation.
- When action_type is "email", draft email_draft: a complete, sendable email
  body in the user's own voice, based on what the message says — a greeting,
  the substance, a sign-off. Keep it concise. Never draft a body for
  action_type "calendar".

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
        is_actionable=result.is_actionable,
        chat_reply=result.chat_reply,
        type=result.type,
        title=result.title,
        summary=result.summary,
        due_at=result.due_at,
        effort_minutes=int(result.effort_minutes) if result.effort_minutes else None,
        focus_depth=result.focus_depth,
        confidence=result.confidence,
        missing_fields=result.missing_fields,
        reasoning=result.reasoning,
        action_type=result.action_type,
        email_recipient=result.email_recipient,
        email_draft=result.email_draft,
    )

    try:
        publish("items-extracted", extracted)
    except Exception:
        logger.exception("publish failed item_id=%s", raw.item_id)
        raise HTTPException(status_code=500, detail="publish failed") from None

    logger.info(
        "extracted item_id=%s is_actionable=%s type=%s",
        raw.item_id,
        extracted.is_actionable,
        extracted.type,
    )
    return {"status": "extracted", "item_id": str(raw.item_id)}


@app.get("/health")
async def health():
    return {"status": "ok"}
