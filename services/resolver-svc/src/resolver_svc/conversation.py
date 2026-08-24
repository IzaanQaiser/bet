"""Phase G step D — the unified conversational turn (agent-contracts.md
§3.2/§3.3, replacing clarification.py + the fixed confirmation-card/Y-N-A
handling for the main obligation confirm flow). One Gemini call per turn,
replacing three previously-separate mechanisms: clarify()'s field-merge
question loop, templates.py's render_confirmation_card, and
reply_classifier.py's strict Y/N/ATTACH keyword matching — all for this one
flow. Still exactly two LLM call sites in the whole system (agent-contracts.md
§0): extraction, and this.

Verified empirically against real Vertex AI before writing this module
(this project's established pattern) — mixing a concrete field-merge
output, a Literal intent classification, and a free-text reply_text field
in one schema validated correctly on 7/7 real scenarios (field merge alone,
AFFIRM/DENY/CORRECTION/ATTACH, and voice-mirroring from history).

ADR 0001/0003 note: this call NEVER decides whether a write happens — it
only fills a classified `intent` field. Pipeline code in main.py does a
plain `if intent == "AFFIRM":` before ever publishing to items.confirmed,
same mechanism as the old `if classification == "Y":`. The LlmAgent below
has no `tools=[...]` — it cannot call anything, only return structured
text. CORRECTION never publishes on its own, no matter how complete the
merged fields look — only a subsequent, separate AFFIRM turn does.
"""

import json
import os
from datetime import datetime
from typing import Literal

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import BaseModel

_SYSTEM_PROMPT = """You are the conversational stage of a personal obligation-tracking system,
texting a user back like a real friend would — casual, terse, lowercase is
fine. Match their own texting style from the recent message history given
below if there is any; otherwise use a generic casual voice (things like
"bet", "no worries" are fine defaults, but don't force slang that clashes
with what the user's own history actually sounds like).

You're given: the item (title/type/summary/effort/known fields), which
fields (if any) are still missing, whether a confirmation message has
ALREADY been sent for this item (awaiting_confirmation), a pending
thread-attach candidate title if any, recent message history, and the
user's latest reply (absent on the very first turn for a new item).

Do, in order:

1. Field merging: if the latest reply resolves due_at (a date/time given or
   clearly implied) AND due_at is listed as missing (or this is a
   correction, see step 2), resolve it to a full ISO 8601 datetime with NO
   UTC offset (a naive local datetime string, e.g. "2026-08-28T14:00:00" —
   never append a timezone suffix) using the given current date/timezone
   only as reference for resolving relative dates, set due_at_filled true,
   put the value in due_at. Never invent a date the reply didn't provide or
   imply — if ambiguous, leave due_at_filled false and due_at null. Same
   rule for email_recipient (a literal address only, never guessed from a
   name). Only touch fields actually listed as missing, or explicitly
   updated during a correction. still_missing MUST be a subset of the given
   missing fields — the only two field names that can ever appear are
   "due_at" and "email_recipient". Never add any other field name (e.g.
   "title") to still_missing, even if the title/summary itself still seems
   vague — the title is fixed by an earlier stage and isn't something this
   turn resolves; if it genuinely reads as unclear, just work with it as
   given rather than asking about it.

2. Intent (ONLY set this if awaiting_confirmation is true — a confirmation
   message was already sent and this reply is responding to it; otherwise
   leave intent null):
   - AFFIRM: a clear yes/confirmation ("yes", "yeah", "bet", "sounds good",
     "do it", etc.)
   - DENY: a clear no/cancel ("no", "nah", "don't", "cancel", etc.)
   - CORRECTION: the reply changes a detail (a different time, a different
     recipient, etc.) rather than simply confirming or denying — resolve
     the corrected field(s) per step 1 above even though they weren't in
     the original missing list.
   - ATTACH: the reply accepts the offered thread-attach ("attach it",
     "yeah attach", "a").
   - OTHER: genuinely unclear, off-topic, or a question.

3. reply_text: the actual next SMS to send. Rules:
   - If still_missing is non-empty (fields remain missing, not yet at
     confirmation): a short casual question asking for what's still
     missing, in one natural sentence, never a list.
   - If still_missing just became empty (nothing missing, first time this
     item reaches a confirmation): a short casual message stating what
     will happen (the task, the date/time if any) and asking them to
     confirm — this doubles as the confirmation prompt itself, so it must
     make clear a yes/no is expected. Mention the thread-attach candidate
     naturally if one is given.
   - If awaiting_confirmation and intent is AFFIRM: a short casual
     acknowledgment that it's done/scheduled.
   - If DENY: a short casual "no worries, scrapped it" style line.
   - If CORRECTION: a short casual line restating the updated detail and
     asking to confirm again — never assume yes just because a correction
     was given.
   - If ATTACH: a short casual line confirming the attach.
   - If OTHER: a short casual line asking them to clarify (yes/no/what to
     change).
   Keep it SMS-length, under 160 characters where possible.

Output must conform exactly to the provided schema. No text outside it.
"""


class ConversationTurnResult(BaseModel):
    due_at_filled: bool = False
    due_at: str | None = None
    email_recipient_filled: bool = False
    email_recipient: str | None = None
    still_missing: list[str] = []
    intent: Literal["AFFIRM", "DENY", "CORRECTION", "ATTACH", "OTHER"] | None = None
    reply_text: str


_agent = LlmAgent(
    name="conversation",
    model=os.environ.get("GEMINI_MODEL", "gemini-3.5-flash"),
    instruction=_SYSTEM_PROMPT,
    output_schema=ConversationTurnResult,
)
_session_service = InMemorySessionService()


async def converse(
    session_id: str,
    now_local: datetime,
    tz_name: str,
    title: str,
    item_type: str,
    summary: str,
    effort_minutes: int,
    known_fields: dict,
    missing_fields: list[str],
    awaiting_confirmation: bool,
    thread_attach_title: str | None,
    history: list[str],
    latest_reply: str | None,
) -> ConversationTurnResult:
    await _session_service.create_session(
        app_name="conversation", user_id="conversation", session_id=session_id
    )
    runner = Runner(app_name="conversation", agent=_agent, session_service=_session_service)
    reply_text = f"'{latest_reply}'" if latest_reply else "(none, first turn)"
    hist_block = "\n".join(history) if history else "(none yet)"
    message_text = (
        f"Current date/time: {now_local.isoformat()}, timezone: {tz_name}\n"
        f"Item: {title} (type={item_type})\n"
        f"Summary: {summary}\n"
        f"Effort: {effort_minutes} min\n"
        f"Known fields: {known_fields}\n"
        f"Missing fields: {missing_fields}\n"
        f"awaiting_confirmation: {awaiting_confirmation}\n"
        f"Thread-attach candidate: {thread_attach_title}\n"
        f"Recent message history (oldest first):\n{hist_block}\n"
        f"User's latest reply: {reply_text}\n"
    )
    message = types.Content(role="user", parts=[types.Part(text=message_text)])

    final_text = None
    async for event in runner.run_async(
        user_id="conversation", session_id=session_id, new_message=message
    ):
        if event.is_final_response() and event.content and event.content.parts:
            final_text = event.content.parts[-1].text

    if final_text is None:
        raise RuntimeError("Gemini produced no final response")
    result = ConversationTurnResult.model_validate(json.loads(final_text))

    # Defensive, not just prompted: still_missing is an unconstrained
    # list[str] at the schema level (Vertex AI structured output doesn't
    # support a Literal-list here — untested combination, not worth risking
    # given the two field names are already known statically). A real run
    # once returned "title" alongside "due_at" despite the prompt's
    # instruction — the pipeline has no merge logic for anything but
    # due_at/email_recipient, so an unrecognized name would silently strand
    # the item asking about a field it can never resolve. Filtered here,
    # the one choke point every caller goes through, rather than trusting
    # the prompt alone.
    result.still_missing = [f for f in result.still_missing if f in ("due_at", "email_recipient")]
    return result
