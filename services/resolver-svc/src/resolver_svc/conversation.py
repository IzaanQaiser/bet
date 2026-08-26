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

Conversation-continuity note (Phase G follow-up, same session as step D):
ingest-svc routes any inbound SMS to whichever item this user has open
(DUPLICATE_SUSPECTED/CLARIFYING/AWAITING_CONFIRMATION) purely by state —
it has no way to know whether the message's *content* actually has
anything to do with that item. Before this, every reply while an item was
open got force-fed to converse() as if it must be about that item, which
is wrong whenever it isn't (a stuck test item absorbing an unrelated
follow-up was a real instance of this, found during step D's own live
testing). Deliberately not fixed with a timeout — SMS threads are
persistent on the user's screen, so a reply an hour or a day later is
still often genuinely about the same item; only the reply's own content
can tell related from unrelated. `relates_to_item` is that escape hatch:
when the model says a reply doesn't relate, main.py leaves the open item
completely untouched (no timeout-driven state change, nothing lost — it's
still there waiting) and spins up a brand new item through the same path
a first-contact message takes, via the new `create_raw_item` shared
helper + `items-raw` publish. Verified empirically before wiring into
main.py, per this project's pattern of not trusting a prompt-only change
to a shared schema without a real Vertex AI check first.

Dedupe-question note (same follow-up work, prompted directly by a user
report against the live deployed demo): §3.1's dedupe question was
deliberately left as a fixed "Reply Y to merge, N if it's different"
template through step D and the continuity fix above — not what either
was about. That rigid Y/N script is exactly what the user hit and
objected to. `dedupe_candidate_title`/`awaiting_dedupe_reply` fold the
dedupe question into this same call instead: the initial question and
both its merge/different acknowledgments are now `reply_text`, in voice,
same as every other outbound message in this flow — no fixed template
left anywhere in the confirm-a-duplicate path. `templates.py`'s
`render_dedupe_question`/`render_merged` are now dead and removed;
`render_needs_review` stays — a deliberate exhaustion terminal message,
untouched by any of this.
"""

import json
import logging
import os
import time
from datetime import datetime
from typing import Literal

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import BaseModel

logger = logging.getLogger("resolver_svc.conversation")

_SYSTEM_PROMPT = """You are the conversational stage of a personal obligation-tracking system,
texting a user back like a real friend would — casual, terse, lowercase is
fine. Match their own texting style from the recent message history given
below if there is any; otherwise use a generic casual voice (things like
"bet", "no worries" are fine defaults, but don't force slang that clashes
with what the user's own history actually sounds like).

You're given: the item (title/type/summary/effort/known fields), which
fields (if any) are still missing, whether a confirmation message has
ALREADY been sent for this item (awaiting_confirmation), a pending
thread-attach candidate title if any, a possible-duplicate candidate title
if any (dedupe_candidate_title) and whether a reply to THAT question is
being interpreted right now (awaiting_dedupe_reply), recent message
history, and the user's latest reply (absent on the very first turn for a
new item).

Do, in order:

0. Relatedness (only meaningful when a latest reply is given — on the very
   first turn for a new item there is no reply yet, so this is trivially
   true): decide whether the latest reply actually relates to THIS item at
   all, or reads like a completely separate new thought/request/topic the
   user is bringing up instead of responding to this one. Text threads are
   persistent — a reply given a long time after the last message can still
   obviously relate to it, so elapsed time is never a reason by itself to
   call something unrelated. A reply that doesn't fully resolve what's
   missing, or is vague/hesitant/off-hand, is still about the item — don't
   call it unrelated just because it doesn't answer the question. Only set
   relates_to_item false when the CONTENT itself reads as a genuinely
   different topic: a new obligation, a new unconnected chat message, etc.
   When false, leave every other field at its default (empty still_missing,
   null intent, due_at_filled false, empty reply_text) — this item is left
   completely alone and the reply is handled as its own separate thing
   elsewhere; do not reference this item in reply_text.

1. Field merging: if the latest reply resolves due_at (a date/time given or
   clearly implied) AND due_at is listed as missing (or this is a
   correction, see step 2), resolve it to a full ISO 8601 datetime with NO
   UTC offset (a naive local datetime string, e.g. "2026-08-28T14:00:00" —
   never append a timezone suffix) using the given current date/timezone
   only as reference for resolving relative dates, set due_at_filled true,
   put the value in due_at. Never invent a date the reply didn't provide or
   imply — if ambiguous, leave due_at_filled false and due_at null. Same
   rule for email_recipient (a literal address only, never guessed from a
   name). If effort_minutes is listed as missing and the latest reply gives
   any duration/scope signal ("3 hours probably", "maybe an hour", "quick
   one"), resolve it to the nearest of 15/30/60/120/240 minutes — round up
   on a tie (underestimating available work time is the worse failure
   mode) — set effort_minutes_filled true, put the value in effort_minutes.
   If the reply is still vague with no usable signal, leave
   effort_minutes_filled false. Only touch fields actually listed as
   missing, or explicitly updated during a correction. still_missing MUST
   be a subset of the given missing fields — the only three field names
   that can ever appear are "due_at", "email_recipient", and
   "effort_minutes". Never add any other field name (e.g. "title") to
   still_missing, even if the title/summary itself still seems vague — the
   title is fixed by an earlier stage and isn't something this turn
   resolves; if it genuinely reads as unclear, just work with it as given
   rather than asking about it.

2. Intent (ONLY set this if awaiting_confirmation OR awaiting_dedupe_reply
   is true — a question was already sent and this reply is responding to
   it; otherwise leave intent null):

   If awaiting_dedupe_reply is true, this reply is answering a DIFFERENT
   question than usual — "is this the same as an existing item" — not
   confirming a new one. Here AFFIRM/DENY mean:
   - AFFIRM: yes, it's the same thing (will be merged into the existing
     item — "yeah", "same one", "that's it", etc.)
   - DENY: no, it's different / a separate new thing ("no", "nah, different
     thing", "not the same", etc.)
   CORRECTION and ATTACH don't apply to a dedupe reply — use OTHER for
   anything that isn't a clear yes/no on "is this the same item".

   Otherwise (awaiting_confirmation true, the normal case):
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
   - If dedupe_candidate_title is given and this is the very first turn
     (no latest_reply, awaiting_dedupe_reply false): a short casual
     question asking whether this is the same thing as the existing item
     (name it naturally) — phrase it like you'd actually ask a friend,
     e.g. "isn't this the same as X you already had on there?" — NEVER a
     rigid "Reply Y to merge, N if it's different" style instruction, no
     fixed format, just a normal question a yes/no answer naturally fits.
   - If awaiting_dedupe_reply and intent is AFFIRM: a short casual line
     acknowledging you're treating it as the same thing / merging it —
     mention the existing item naturally.
   - If awaiting_dedupe_reply and intent is DENY: a short casual line
     acknowledging it's separate/different — just an acknowledgment, do
     NOT ask a yes/no confirm question here, the next turn handles
     whatever's still needed for the new item.
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
   - If DENY (awaiting_confirmation, not a dedupe reply): a short casual
     "no worries, scrapped it" style line.
   - If CORRECTION: a short casual line restating the updated detail and
     asking to confirm again — never assume yes just because a correction
     was given.
   - If ATTACH: a short casual line confirming the attach.
   - If OTHER: a short casual line asking them to clarify (yes/no/what to
     change, or — for a dedupe reply — whether it's the same thing or not).
   Whenever a due date/time is mentioned anywhere in reply_text, word it as
   when the task is DUE, never as when a reminder will arrive — the actual
   reminder goes out in advance of the deadline, never at the deadline
   itself, so "I'll set a reminder for 6pm" or "it's locked in for 6" reads
   as the reminder arriving at the deadline, which isn't true and isn't
   what happens. Say "it's due at 6pm" (or similar) instead — never phrase
   the due time as if it's the reminder's own delivery time. Real finding,
   not theoretical: a live conversation phrased a 6pm-due assignment
   exactly this wrong way on both the confirmation prompt and the AFFIRM
   acknowledgment.
   If reminder_1_at and reminder_2_at are both given (non-null) on this
   turn's confirmation message or AFFIRM acknowledgment, state both given
   times directly in the sentence — e.g. if given as 12:00 PM and 3:00 PM,
   say something like "I'll remind you at 12pm and 3pm" using those exact
   given values, not placeholders — so the user knows exactly when they'll
   hear from the system again, not just that a reminder exists. Don't
   mention them on any other kind of turn (a still-missing question, DENY,
   CORRECTION, etc.) — only when stating what will happen has already
   earned its place in the message per the rules above.
   Keep it SMS-length, under 160 characters where possible.

Output must conform exactly to the provided schema. No text outside it.
"""


class ConversationTurnResult(BaseModel):
    relates_to_item: bool = True
    due_at_filled: bool = False
    due_at: str | None = None
    email_recipient_filled: bool = False
    email_recipient: str | None = None
    effort_minutes_filled: bool = False
    # Plain int, not a Literal enum: Vertex AI structured output rejects an
    # integer Literal outright (extractor-svc's _ExtractionResult hit this
    # exact gap first, agent-contracts.md §2) but an unconstrained numeric
    # field works fine (confidence: float already proves this). The prompt
    # instructs the model to pick a bucket value directly; _round_to_bucket
    # below re-buckets defensively regardless, rather than trusting the
    # model's raw number to already be exactly one of the five.
    effort_minutes: int | None = None
    still_missing: list[str] = []
    intent: Literal["AFFIRM", "DENY", "CORRECTION", "ATTACH", "OTHER"] | None = None
    reply_text: str


_agent = LlmAgent(
    name="conversation",
    model=os.environ.get("GEMINI_MODEL", "gemini-3.5-flash"),
    instruction=_SYSTEM_PROMPT,
    output_schema=ConversationTurnResult,
    # Latency fix: left unset, Gemini 3.5 Flash's default "AUTOMATIC" thinking
    # budget spent real, highly variable time deliberating before producing
    # structured output for what's fundamentally rule-based classification +
    # short text generation — no multi-step reasoning needed given how
    # explicit the prompt's own instructions already are. Measured against
    # real Vertex AI before shipping (not assumed): default averaged 6.3s
    # per call (up to 9.7s); thinking_budget=0 averaged 2.2s, consistently,
    # with zero classification regressions across 9 re-run scenarios
    # (AFFIRM/DENY/CORRECTION/relatedness/dedupe, both plain and natural
    # phrasing).
    generate_content_config=types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(thinking_budget=0)
    ),
)
_session_service = InMemorySessionService()

_EFFORT_BUCKETS = (15, 30, 60, 120, 240)


def _round_to_bucket(minutes: int) -> int:
    """Nearest of _EFFORT_BUCKETS, rounding up on an exact tie —
    underestimating available work time is the worse failure mode (same
    reasoning extractor-svc's own bucket-guessing already uses)."""
    return min(_EFFORT_BUCKETS, key=lambda b: (abs(b - minutes), -b))


async def converse(
    session_id: str,
    now_local: datetime,
    tz_name: str,
    title: str,
    item_type: str,
    summary: str,
    effort_minutes: int | None,
    known_fields: dict,
    missing_fields: list[str],
    awaiting_confirmation: bool,
    thread_attach_title: str | None,
    history: list[str],
    latest_reply: str | None,
    dedupe_candidate_title: str | None = None,
    awaiting_dedupe_reply: bool = False,
    reminder_1_at: str | None = None,
    reminder_2_at: str | None = None,
) -> ConversationTurnResult:
    _t0 = time.monotonic()
    await _session_service.create_session(
        app_name="conversation", user_id="conversation", session_id=session_id
    )
    _t1 = time.monotonic()
    runner = Runner(app_name="conversation", agent=_agent, session_service=_session_service)
    reply_text = f"'{latest_reply}'" if latest_reply else "(none, first turn)"
    hist_block = "\n".join(history) if history else "(none yet)"
    effort_line = f"{effort_minutes} min" if effort_minutes is not None else "unknown"
    message_text = (
        f"Current date/time: {now_local.isoformat()}, timezone: {tz_name}\n"
        f"Item: {title} (type={item_type})\n"
        f"Summary: {summary}\n"
        f"Effort: {effort_line}\n"
        f"Known fields: {known_fields}\n"
        f"Missing fields: {missing_fields}\n"
        f"awaiting_confirmation: {awaiting_confirmation}\n"
        f"Thread-attach candidate: {thread_attach_title}\n"
        f"dedupe_candidate_title: {dedupe_candidate_title}\n"
        f"awaiting_dedupe_reply: {awaiting_dedupe_reply}\n"
        f"reminder_1_at: {reminder_1_at}\n"
        f"reminder_2_at: {reminder_2_at}\n"
        f"Recent message history (oldest first):\n{hist_block}\n"
        f"User's latest reply: {reply_text}\n"
    )
    message = types.Content(role="user", parts=[types.Part(text=message_text)])

    _t2 = time.monotonic()
    final_text = None
    async for event in runner.run_async(
        user_id="conversation", session_id=session_id, new_message=message
    ):
        if event.is_final_response() and event.content and event.content.parts:
            final_text = event.content.parts[-1].text
    _t3 = time.monotonic()
    logger.info(
        "TIMING converse: session_create=%.2fs prompt_build=%.2fs gemini_call=%.2fs total=%.2fs",
        _t1 - _t0, _t2 - _t1, _t3 - _t2, _t3 - _t0,
    )

    if final_text is None:
        raise RuntimeError("Gemini produced no final response")
    result = ConversationTurnResult.model_validate(json.loads(final_text))

    # Defensive, not just prompted: still_missing is an unconstrained
    # list[str] at the schema level (Vertex AI structured output doesn't
    # support a Literal-list here — untested combination, not worth risking
    # given the three field names are already known statically). A real run
    # once returned "title" alongside "due_at" despite the prompt's
    # instruction — the pipeline has no merge logic for anything but
    # due_at/email_recipient/effort_minutes, so an unrecognized name would
    # silently strand the item asking about a field it can never resolve.
    # Filtered here, the one choke point every caller goes through, rather
    # than trusting the prompt alone.
    result.still_missing = [
        f for f in result.still_missing if f in ("due_at", "email_recipient", "effort_minutes")
    ]

    # Defensive re-bucketing, same reasoning as still_missing above: don't
    # trust the model's raw number to already be exactly one of the five
    # canonical buckets, even though the prompt asks for that directly.
    if result.effort_minutes_filled and result.effort_minutes is not None:
        result.effort_minutes = _round_to_bucket(result.effort_minutes)

    # Defensive, same reasoning as above: relates_to_item is only ever
    # meaningful when there's an actual reply to judge. Forcing it true on
    # the first turn (no latest_reply) means a caller never has to special-
    # case that path — it can check the field unconditionally.
    if latest_reply is None:
        result.relates_to_item = True
    return result
