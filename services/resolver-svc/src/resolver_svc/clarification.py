"""Clarification LLM call — agent-contracts.md §3.2, the second (and
last) Gemini call in this system (§0).

Resolved gap, found in step 10's real testing: the doc's
`ClarificationResponse.filled_fields: dict[str, Any]` doesn't work on
Vertex AI's structured output — verified empirically in a scratch test
before writing this module. An open-key/`Any`-typed dict output field
makes the model emit a huge run of whitespace padding inside an
otherwise-empty object instead of real key-value content, reproducibly,
regardless of `dict[str, Any]` vs `dict[str, str]`. Narrowed to a
concrete `due_at`-only schema instead — `due_at` is the only field the
extractor's own contract (agent-contracts.md §2) ever adds to
`missing_fields` in the first place, so the generic multi-field
mechanism was speculative generality for a case that doesn't occur
today, on top of being a shape Vertex can't reliably fill anyway.
"""

import json
import os
from datetime import datetime

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import BaseModel

_SYSTEM_PROMPT = """You are the clarification stage. You have a partially-structured item with
a due date missing. Given the user's latest reply (absent on the first
turn), do two things:

1. If the reply provides or clearly implies a due date, resolve it to a
   full ISO 8601 datetime using the provided current date/timezone as
   reference, set due_at_filled to true, and put the resolved value in
   due_at. Never invent a date the reply didn't provide or imply — if
   ambiguous, leave due_at_filled false and due_at null.
2. If due_at is still unresolved after that, write ONE short question —
   SMS length, under 160 characters where possible — asking for it.

Output must conform exactly to the provided schema. No text outside it.
"""


class ClarificationResult(BaseModel):
    due_at_filled: bool
    due_at: str | None = None
    still_missing: list[str]
    question: str | None = None


_agent = LlmAgent(
    name="clarifier",
    model=os.environ.get("GEMINI_MODEL", "gemini-3.5-flash"),
    instruction=_SYSTEM_PROMPT,
    output_schema=ClarificationResult,
)
_session_service = InMemorySessionService()


async def clarify(
    session_id: str, now_local: datetime, tz_name: str, title: str, latest_reply: str | None
) -> ClarificationResult:
    await _session_service.create_session(
        app_name="clarifier", user_id="clarifier", session_id=session_id
    )
    runner = Runner(app_name="clarifier", agent=_agent, session_service=_session_service)
    reply_text = f"'{latest_reply}'" if latest_reply else "(none, first turn)"
    message_text = (
        f"Current date/time: {now_local.isoformat()}, timezone: {tz_name}\n"
        f"Item: {title}\n"
        f"User's latest reply: {reply_text}\n"
    )
    message = types.Content(role="user", parts=[types.Part(text=message_text)])

    final_text = None
    async for event in runner.run_async(
        user_id="clarifier", session_id=session_id, new_message=message
    ):
        if event.is_final_response() and event.content and event.content.parts:
            final_text = event.content.parts[-1].text

    if final_text is None:
        raise RuntimeError("Gemini produced no final response")
    return ClarificationResult.model_validate(json.loads(final_text))
