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
only fills a classified `intent` field. The LlmAgent below has no
`tools=[...]` — it cannot call anything, only return structured text.
For the dedupe merge/different question, `intent == "AFFIRM"` is still
main.py's plain gate before writing `parent_item_id`, same mechanism as
the old `if classification == "Y":`.

**V1 polish, superseding the paragraph above for the main (non-dedupe)
flow — user-directed, main.py's own module docstring has the full note:**
there is no more separate confirmation step, no more `awaiting_confirmation`,
no more AFFIRM/DENY/CORRECTION/ATTACH classification for a fresh item.
`still_missing` emptying out is now itself what triggers the commit —
main.py calls `_confirm_and_publish` the instant that happens, no
affirmative reply required. `intent` is only ever meaningfully set now for
`awaiting_dedupe_reply` (AFFIRM/DENY/OTHER); CORRECTION and ATTACH are
gone from the schema entirely, since both only ever existed for the
removed confirmation step.

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

You're given: the item (title/type/summary/effort/known fields), whether
it's a scheduled event you attend at a specific time rather than a task
with a completion deadline (is_scheduled_event), which fields (if any)
are still missing, a pending thread-attach candidate title if any, a
possible-duplicate candidate title if any (dedupe_candidate_title) and
whether a reply to THAT question is being interpreted right now
(awaiting_dedupe_reply), recent message history, the user's other real
committed obligations if any (other_items — what's already on their plate,
separate from this item), and the user's latest reply (absent on the very
first turn for a new item). There is no separate confirmation step for a
fresh item — the moment nothing is missing, it's locked in immediately, no
yes/no required.

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
   imply — if ambiguous, leave due_at_filled false and due_at null. This
   applies just as much when only the TIME is missing, not just the date:
   "tonight", "tomorrow", "today" etc. pin a calendar day but say nothing
   about what time — never fill in a plausible-sounding default (e.g.
   treating "tonight" as 11:59pm, "tomorrow" as 6pm) to complete the
   datetime. A day with no time is exactly as unresolved as no day at
   all: leave due_at_filled false and due_at null, so still_missing keeps
   "due_at" and the next reply_text asks specifically for the time. Real
   finding, not theoretical: a live conversation silently assumed times
   like this on two separate real obligations, never stated to the user
   until they asked or pushed back on it. Same rule for email_recipient
   (a literal address only, never guessed from a name). If "title" is
   listed as missing (only ever happens for a scheduled event with no
   identifying detail yet) and the latest reply gives one — a name,
   subject, location, or purpose — set title_filled true, put a short
   specific label in title. If it's still vague, leave title_filled false.
   If "effort_minutes" is listed as missing (only ever happens for a
   scheduled event with no stated duration yet) and the latest reply gives
   a real duration ("an hour" -> 60, "1.5 hours" -> 90, "quick 20 min
   call" -> 20), resolve it to the EXACT number of minutes stated or
   clearly implied — never round to a "nice" number, never bucket it.
   This becomes a real Calendar event's exact end time, so precision
   matters here in a way it doesn't for a task's rough work-time guess
   (real bug, found live: "1.5 hours" got silently rounded to a 2-hour
   Calendar event). Set effort_minutes_filled true, put the exact value
   in effort_minutes. If the reply is still vague with no real number
   ("quick one", "not long"), leave effort_minutes_filled false — a vague
   scope signal isn't a real duration and must not be guessed into one.
   Only touch fields actually listed as missing, or explicitly updated
   during a correction. still_missing MUST be a subset of the given
   missing fields — the only four field names that can ever appear are
   "due_at", "email_recipient", "title", and "effort_minutes". title and
   effort_minutes are only ever listed missing for a scheduled event —
   never invent asking about either for a task/latent, even if the
   title/summary reads vague or effort is unstated; just work with what's
   given for those.

2. Intent (ONLY set this if awaiting_dedupe_reply is true — a dedupe
   question was already sent and this reply is answering it; otherwise
   leave intent null. There's no other kind of question left to classify
   a reply against — a fresh item never waits for an explicit confirmation
   anymore):

   This reply is answering "is this the same as an existing item", not
   confirming a new one. AFFIRM/DENY mean:
   - AFFIRM: yes, it's the same thing (will be merged into the existing
     item — "yeah", "same one", "that's it", etc.)
   - DENY: no, it's different / a separate new thing ("no", "nah, different
     thing", "not the same", etc.)
   - OTHER: genuinely unclear, off-topic, or a question — anything that
     isn't a clear yes/no on "is this the same item".

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
   - If still_missing is non-empty (fields remain missing): a short casual
     question asking for what's still missing, in one natural sentence,
     never a list.
   - If still_missing just became empty (nothing left missing, this turn is
     what completes the item): a short casual message stating what's
     locked in — the task/event, the date/time if any — as a DONE fact, not
     a question. This IS the commit, happening right now, no yes/no
     follow-up expected or wanted — never phrase it as asking for
     confirmation ("does that work?", "confirm?"), never leave it sounding
     provisional. Mention the thread-attach candidate naturally if one is
     given, as an FYI, not something to confirm.
   - If OTHER (a dedupe reply that's genuinely unclear): a short casual
     line asking them to clarify whether it's the same thing or not.
   If other_items is non-empty, use it like a friend who actually remembers
   what's going on would — bring one up ONLY when it's a genuine, concrete
   connection to what's being said right now (a real timing consideration:
   this new thing and something already on their plate land close together,
   the same day, back-to-back, etc.), phrased naturally as part of the
   reply, not a bolted-on aside. Never force a mention just because
   other_items is non-empty — most turns, none of it is actually relevant,
   and forcing one in anyway reads as noise, not attentiveness. Never treat
   anything in other_items as needing confirmation or a yes/no of its own —
   it's context for THIS item's reply, not a second topic to resolve.
   Whenever a date/time is mentioned anywhere in reply_text, how you word it
   depends on is_scheduled_event:
   - is_scheduled_event false (a task with a deadline — the default): word
     it as when the task is DUE, never as when a reminder will arrive — the
     actual reminder goes out in advance of the deadline, never at the
     deadline itself, so "I'll set a reminder for 6pm" or "it's locked in
     for 6" reads as the reminder arriving at the deadline, which isn't
     true and isn't what happens. Say "it's due at 6pm" (or similar)
     instead. Real finding, not theoretical: a live conversation phrased a
     6pm-due assignment exactly this wrong way in the locked-in message.
   - is_scheduled_event true (a real event you attend — a meeting, party,
     call, appointment): word it as when it STARTS, e.g. "it starts at
     3pm" or "you're at it at 3pm" — never "due at 3pm", which is deadline
     language and reads wrong for something you just show up to. Real
     finding: a live conversation confirmed a party as "due at 3pm
     tomorrow", which read oddly for exactly this reason.
   If reminder_1_at and/or reminder_2_at are given (non-null) on this
   turn's locked-in message, state whichever ones are actually given
   directly in the sentence, using those exact given values, not
   placeholders. Both given: "I'll remind you at 12pm and 3pm." Only one
   given (the other is null — this happens for real: locking something in
   within its own 30-minute reminder window means only the later one is
   still ahead of right now): "I'll remind you at 3pm," never mentioning a
   second time or implying there's an earlier one too. Never invent or
   infer a missing one from the other — only state exactly what's given,
   exactly as given. So the user knows exactly when they'll hear from the
   system again, not just that a reminder exists. Don't mention either at
   all on any other kind of turn (a still-missing question, a dedupe
   acknowledgment, etc.) — only when stating what's locked in has already
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
    title_filled: bool = False
    title: str | None = None
    effort_minutes_filled: bool = False
    # Plain int, not a Literal enum: Vertex AI structured output rejects an
    # integer Literal outright (extractor-svc's _ExtractionResult hit this
    # exact gap first, agent-contracts.md §2) but an unconstrained numeric
    # field works fine (confidence: float already proves this). Exact
    # minutes, never bucketed — migrations/0016's note on why.
    effort_minutes: int | None = None
    still_missing: list[str] = []
    # Only ever meaningfully set for a dedupe reply now (awaiting_dedupe_reply)
    # — CORRECTION/ATTACH are gone from the schema, both only ever existed
    # for the removed confirmation step (v1 polish, module docstring note).
    intent: Literal["AFFIRM", "DENY", "OTHER"] | None = None
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


def _reconcile_still_missing(missing_fields: list[str], result) -> list[str]:
    """Real production bug, not theoretical: a live call was given
    missing_fields=["due_at", "effort_minutes", "title"] on a fresh
    item's first turn (no latest_reply — nothing could have resolved
    anything yet) and came back with still_missing=["effort_minutes",
    "title"], due_at_filled=False, due_at=null — silently dropping
    "due_at" from still_missing despite never actually resolving it. The
    item then auto-committed with due_at=None and crashed committer-svc
    downstream (a null due_at has nowhere left to be caught once there's
    no confirmation step to fall back on). A field the caller listed as
    missing must never vanish from still_missing unless its own *_filled
    flag says this turn actually resolved it."""
    filled_this_turn = {
        "due_at": result.due_at_filled,
        "email_recipient": result.email_recipient_filled,
        "title": result.title_filled,
        "effort_minutes": result.effort_minutes_filled,
    }
    still_missing = list(result.still_missing)
    for field in missing_fields:
        if field in filled_this_turn and not filled_this_turn[field] and field not in still_missing:
            still_missing.append(field)
    return still_missing


async def converse(
    session_id: str,
    now_local: datetime,
    tz_name: str,
    title: str | None,
    item_type: str,
    summary: str,
    effort_minutes: int | None,
    known_fields: dict,
    missing_fields: list[str],
    thread_attach_title: str | None,
    history: list[str],
    latest_reply: str | None,
    dedupe_candidate_title: str | None = None,
    awaiting_dedupe_reply: bool = False,
    reminder_1_at: str | None = None,
    reminder_2_at: str | None = None,
    other_items: list[str] | None = None,
    is_scheduled_event: bool = False,
) -> ConversationTurnResult:
    _t0 = time.monotonic()
    await _session_service.create_session(
        app_name="conversation", user_id="conversation", session_id=session_id
    )
    _t1 = time.monotonic()
    runner = Runner(app_name="conversation", agent=_agent, session_service=_session_service)
    reply_text = f"'{latest_reply}'" if latest_reply else "(none, first turn)"
    hist_block = "\n".join(history) if history else "(none yet)"
    other_items_block = "\n".join(other_items) if other_items else "(none)"
    effort_line = f"{effort_minutes} min" if effort_minutes is not None else "unknown"
    title_display = title if title else "(untitled — not yet known, ask what it's for)"
    message_text = (
        f"Current date/time: {now_local.isoformat()}, timezone: {tz_name}\n"
        f"Item: {title_display} (type={item_type})\n"
        f"Summary: {summary}\n"
        f"is_scheduled_event: {is_scheduled_event}\n"
        f"Effort: {effort_line}\n"
        f"Known fields: {known_fields}\n"
        f"Missing fields: {missing_fields}\n"
        f"Thread-attach candidate: {thread_attach_title}\n"
        f"dedupe_candidate_title: {dedupe_candidate_title}\n"
        f"awaiting_dedupe_reply: {awaiting_dedupe_reply}\n"
        f"reminder_1_at: {reminder_1_at}\n"
        f"reminder_2_at: {reminder_2_at}\n"
        f"User's other real committed obligations (other_items, separate from this item):\n"
        f"{other_items_block}\n"
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
    # given the four field names are already known statically). A real run
    # once returned "title" alongside "due_at" despite the prompt's own
    # instruction at the time (title wasn't yet a mergeable field) — the
    # pipeline has no merge logic for anything outside this set, so an
    # unrecognized name would silently strand the item asking about a
    # field it can never resolve. Filtered here, the one choke point every
    # caller goes through, rather than trusting the prompt alone.
    result.still_missing = [
        f for f in result.still_missing
        if f in ("due_at", "email_recipient", "title", "effort_minutes")
    ]

    # Defensive sanity bound, same reasoning as still_missing above — not
    # bucketed anymore (migrations/0016), but still guarded against a
    # nonsensical raw number (0, negative, or absurdly long) reaching a
    # real Calendar event's end time unchecked. Matches items table's own
    # CHECK constraint range; genuinely out-of-range is treated as not
    # actually resolved rather than silently clamped, so the item keeps
    # asking instead of committing a wrong duration. Runs BEFORE
    # _reconcile_still_missing below so a rejected fill correctly lands
    # back in still_missing rather than vanishing.
    if result.effort_minutes_filled and (
        result.effort_minutes is None or not (0 < result.effort_minutes <= 1440)
    ):
        result.effort_minutes_filled = False
        result.effort_minutes = None

    # Defensive, real production bug (not theoretical, see
    # _reconcile_still_missing's own docstring) — re-added here, the one
    # choke point every caller goes through, rather than trusting the
    # model's still_missing list to be complete on its own.
    result.still_missing = _reconcile_still_missing(missing_fields, result)

    # Defensive, same reasoning as above: relates_to_item is only ever
    # meaningful when there's an actual reply to judge. Forcing it true on
    # the first turn (no latest_reply) means a caller never has to special-
    # case that path — it can check the field unconditionally.
    if latest_reply is None:
        result.relates_to_item = True
    return result
