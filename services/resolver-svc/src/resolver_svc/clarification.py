"""Clarification LLM call — agent-contracts.md §3.2, the second (and
last) Gemini call in this system (§0).

Resolved gap, found in step 10's real testing: the doc's
`ClarificationResponse.filled_fields: dict[str, Any]` doesn't work on
Vertex AI's structured output — verified empirically in a scratch test
before writing this module. An open-key/`Any`-typed dict output field
makes the model emit a huge run of whitespace padding inside an
otherwise-empty object instead of real key-value content, reproducibly,
regardless of `dict[str, Any]` vs `dict[str, str]`. Narrowed to a
concrete `due_at`-only schema instead — `due_at` was the only field the
extractor's own contract ever added to `missing_fields`, until step 15.

Step 15 adds the second concrete field this module's own docstring
anticipated: `email_recipient` (agent-contracts.md §2.1/§3.2), extended
the same way `due_at` was rather than reintroducing the rejected generic
dict shape. `missing_fields` is now passed in explicitly (previously
implicit — the system prompt just always meant `due_at`, since nothing
else was ever missing) so the model knows which of the two fields is
actually being asked about this turn.
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
one or more fields missing — which ones (due_at and/or email_recipient)
are listed in the message below, along with the item's title and the
user's latest reply (absent on the first turn). Do:

1. If the reply resolves due_at (a date/time is provided or clearly
   implied) AND due_at is currently missing, resolve it to a full ISO
   8601 datetime using the provided current date/timezone as reference,
   set due_at_filled to true, and put the resolved value in due_at.
   Never invent a date the reply didn't provide or imply — if ambiguous,
   leave due_at_filled false and due_at null.
2. If the reply resolves email_recipient (a literal email address is
   provided) AND email_recipient is currently missing, set
   email_recipient_filled to true and put the address in email_recipient.
   Never guess an address from a name — if the reply gives a name but not
   an address, leave email_recipient_filled false.
3. Only resolve fields that are actually listed as missing below — never
   touch a field that isn't currently missing.
4. If any of the listed fields remain missing after that, write ONE short
   question — SMS length, under 160 characters where possible — that asks
   for all of them together in one natural sentence. Never itemize as a
   list.

Output must conform exactly to the provided schema. No text outside it.
"""


class ClarificationResult(BaseModel):
    due_at_filled: bool = False
    due_at: str | None = None
    email_recipient_filled: bool = False
    email_recipient: str | None = None
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
    session_id: str,
    now_local: datetime,
    tz_name: str,
    title: str,
    missing_fields: list[str],
    latest_reply: str | None,
) -> ClarificationResult:
    await _session_service.create_session(
        app_name="clarifier", user_id="clarifier", session_id=session_id
    )
    runner = Runner(app_name="clarifier", agent=_agent, session_service=_session_service)
    reply_text = f"'{latest_reply}'" if latest_reply else "(none, first turn)"
    message_text = (
        f"Current date/time: {now_local.isoformat()}, timezone: {tz_name}\n"
        f"Item: {title}\n"
        f"Missing fields: {', '.join(missing_fields)}\n"
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
