"""The fire-time suggestion turn — dispatcher-svc's own LLM call site,
mirroring resolver_svc/conversation.py's pattern but not shared with it
(separate deployable, separate prompt content, same reason extractor-svc
and resolver-svc each have their own rather than a shared one).

Three modes of the same call:

1. Nudge turn (latest_reply=None): writes the fire-time text itself, in
   natural voice, using the item's own effort_minutes — not the size of
   whatever free interval happened to be found around it (real bug: the
   old render_fire_suggestion(title, block_minutes) reported the block's
   size, not the task's, so a 15-minute idea in a 1-hour gap said "you
   have 1h free").

2. Reply turn (latest_reply given, awaiting_deferral_reply=False):
   classifies a genuinely free-text reply as ACCEPT/DECLINE/SNOOZE/OTHER,
   replacing shared/obligation_engine_shared/reply_classifier.py's rigid
   Y/N/Later keyword matching. Deliberately does NOT generate the
   acknowledgment text for ACCEPT/SNOOZE: those state real, just-computed
   facts (the actual committed time, the actual dormancy) the model was
   never given and must never guess, same ADR 0003 boundary resolver-
   svc's own locked-in message already respects. DECLINE and OTHER do get
   a reply_text: DECLINE's is the "how long do you wanna put this off?"
   follow-up (user-directed — a decline no longer silently
   auto-reschedules); OTHER's is a natural re-ask, fixing a real, adjacent
   bug where an ambiguous reply used to get zero SMS response at all.

3. Deferral-resolve turn (latest_reply given, awaiting_deferral_reply=
   True): the user has just answered "how long do you wanna put this
   off?" — resolves that free-text answer into a concrete instant
   (defer_until), the same "never invent, only resolve what's actually
   given, ask again if vague" discipline resolver_svc/conversation.py's
   own due_at resolution already uses, except a deferral floor is a
   rough "check back around then" instant rather than a hard deadline,
   so a reasonable default time-of-day for a bare day reference ("tomorrow")
   is a resolution, not a refusal.

ADR 0001 still holds throughout: this call never decides control flow.
It returns a classified intent, a nudge sentence, or a resolved instant;
dispatcher_svc/main.py's own Python state machine is what branches on
any of them and performs every real write, exactly the same shape
resolver-svc's own intent field already uses.
"""

import json
import logging
import os
import time
from datetime import datetime
from typing import Literal
from uuid import uuid4

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from obligation_engine_shared.text import strip_em_dash
from pydantic import BaseModel

logger = logging.getLogger("dispatcher_svc.conversation")

_SYSTEM_PROMPT = """You are the fire-time nudge stage of a personal obligation-tracking system,
texting a user the instant a free block just opened up for something they
committed to doing eventually but haven't scheduled yet. Casual, terse,
lowercase is fine, texting like a real friend would, same voice as
"bet"/"no worries" style defaults.

You're given: the item's title, its estimated effort in minutes
(effort_minutes), the current date/time and timezone, whether this turn
is resolving an answer to "how long do you wanna put this off?"
(awaiting_deferral_reply), and the user's latest reply if any
(latest_reply, absent on the nudge turn itself).

If latest_reply is absent (the nudge turn): write one short, natural
sentence stating the real effort_minutes value and a natural paraphrase
of the title (not the literal title in quotes), ending as a genuine
question inviting a reply, not an instruction. Example shape, given
title="Apply to Tesla job" and effort_minutes=15: "yo u have 15 minutes
to apply to that tesla job?" No "Y / N / Later" footer, no fixed script
— just a normal question a normal reply naturally answers. Set
message_text to this. Leave every other field at its default.

Otherwise, if awaiting_deferral_reply is true (you already asked "how
long do you wanna put this off?" and latest_reply is the answer):
resolve it to a full ISO 8601 datetime with NO UTC offset (a naive local
datetime string, e.g. "2026-08-28T14:00:00"), using the given current
date/time only as reference for resolving relative durations or times
("in 2 hours", "tomorrow morning", "next week", "later today", "an
hour or so"). A bare duration with no specific time ("2 hours", "a few
hours") adds directly onto the current date/time. A vague day-part with
no exact time ("tomorrow", "tomorrow morning", "next week") resolves to
a reasonable specific time within it (morning ~9am, afternoon ~2pm,
evening ~6pm, a bare day alone ~9am) — unlike a hard deadline elsewhere
in this system, a deferral floor is only a rough "check back around
then" instant, so picking a reasonable time here is a real resolution,
not a guess to avoid. Set defer_resolved true and defer_until to that
value in that case. Only if the reply is genuinely unparseable as any
duration or time at all (unrelated, a question, "idk") leave
defer_resolved false and defer_until null, and write a short casual
reply_text asking again how long. Leave message_text and intent null
either way.

Otherwise (a normal reply to the nudge, awaiting_deferral_reply false):
classify what the user meant by it, in the context of the nudge they
were just sent:
- ACCEPT: they're doing it now / into it ("yeah", "lets go", "sure",
  "bet", "on it")
- DECLINE: no, not right now, but not asking to stop being asked
  entirely ("nah", "not now", "cant rn", "no")
- SNOOZE: explicitly wants a longer break from being asked about this
  at all ("later", "remind me in a while", "not for a bit", "stop
  asking for now")
- OTHER: genuinely unclear, off-topic, or a question that isn't a
  yes/no/later answer to the nudge itself
Set intent to whichever applies. If intent is DECLINE, also write a
short casual reply_text asking how long they want to put it off (a
normal question, e.g. "no worries, how long you wanna put it off?" —
never a fixed script). If intent is OTHER, write a short casual
reply_text asking them to clarify whether they're down or not. Leave
reply_text null for ACCEPT and SNOOZE — dispatcher-svc's own code states
what actually happened for those, using the real outcome, not a guess.
Leave message_text, defer_until, and defer_resolved at their defaults.

NEVER use an em dash (—) anywhere in message_text or reply_text, under
any circumstance. Use a period, a comma, or a new sentence instead.

Output must conform exactly to the provided schema. No text outside it.
"""


class SuggestionTurnResult(BaseModel):
    message_text: str | None = None
    intent: Literal["ACCEPT", "DECLINE", "SNOOZE", "OTHER"] | None = None
    reply_text: str | None = None
    defer_until: str | None = None
    defer_resolved: bool = False


_agent = LlmAgent(
    name="suggestion_conversation",
    model=os.environ.get("GEMINI_MODEL", "gemini-3.5-flash"),
    instruction=_SYSTEM_PROMPT,
    output_schema=SuggestionTurnResult,
    # Same latency fix as resolver_svc/conversation.py, same reasoning:
    # this is rule-based classification + short text generation, not
    # open-ended reasoning — thinking_budget=0 removes real, highly
    # variable deliberation time with no accuracy cost.
    generate_content_config=types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(thinking_budget=0)
    ),
)
_session_service = InMemorySessionService()


async def converse_suggestion(
    title: str,
    effort_minutes: int,
    now_local: datetime,
    tz_name: str,
    latest_reply: str | None = None,
    awaiting_deferral_reply: bool = False,
    session_id: str | None = None,
) -> SuggestionTurnResult:
    _t0 = time.monotonic()
    session_id = session_id or f"suggestion-{uuid4().hex[:8]}"
    await _session_service.create_session(
        app_name="suggestion_conversation", user_id="suggestion_conversation",
        session_id=session_id,
    )
    _t1 = time.monotonic()
    runner = Runner(
        app_name="suggestion_conversation", agent=_agent, session_service=_session_service
    )
    reply_line = f"'{latest_reply}'" if latest_reply else "(none, this is the nudge turn)"
    message_text = (
        f"title: {title}\n"
        f"effort_minutes: {effort_minutes}\n"
        f"Current date/time: {now_local.isoformat()}, timezone: {tz_name}\n"
        f"awaiting_deferral_reply: {awaiting_deferral_reply}\n"
        f"latest_reply: {reply_line}\n"
    )
    message = types.Content(role="user", parts=[types.Part(text=message_text)])

    _t2 = time.monotonic()
    final_text = None
    async for event in runner.run_async(
        user_id="suggestion_conversation", session_id=session_id, new_message=message
    ):
        if event.is_final_response() and event.content and event.content.parts:
            final_text = event.content.parts[-1].text
    _t3 = time.monotonic()
    logger.info(
        "TIMING converse_suggestion: session_create=%.2fs prompt_build=%.2fs "
        "gemini_call=%.2fs total=%.2fs",
        _t1 - _t0, _t2 - _t1, _t3 - _t2, _t3 - _t0,
    )

    if final_text is None:
        raise RuntimeError("Gemini produced no final response")
    result = SuggestionTurnResult.model_validate(json.loads(final_text))

    # Defensive, not just prompted — the real guarantee, same reasoning
    # resolver_svc/conversation.py's own converse() already uses.
    if result.message_text:
        result.message_text = strip_em_dash(result.message_text)
    if result.reply_text:
        result.reply_text = strip_em_dash(result.reply_text)
    return result
