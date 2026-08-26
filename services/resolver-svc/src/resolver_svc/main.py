"""resolver-svc — step 12: dedupe via embeddings, on top of step 10's
real clarification loop and step 9's real confirmation.

state-machine.md §1.1: on entering EXTRACTED, before the completeness
check ever runs, check for a duplicate — a cheap dedupe_hash exact match
first (no embedding call needed), then a text-embedding-004 cosine
search over item_embeddings for this user. similarity >= 0.92 (or an
exact hash match) routes to DUPLICATE_SUSPECTED and asks "is this the
same as X?" — never silently merged (ADR 0003). A 0.82-0.92 match
against an existing *latent* is folded into the eventual confirmation
message as a non-blocking thread-attach offer instead of its own stage.

Every path that can reach AWAITING_CONFIRMATION carries a possible
thread-attach candidate forward through conversations.resolved_fields
(the documented scratchpad, data-model.md §2.4) under `_thread_attach_*`
keys, since the offer is decided once at the initial dedupe check but
may need to be rendered much later — after a full clarification
exchange, or after the user says a dedupe match "N, it's different."
Likewise `_dedupe_match_item_id`/`_dedupe_match_title` carry the
matched item across the DUPLICATE_SUSPECTED Y/N round trip; there is no
dedicated column for either, matching how `due_at` already had nowhere
else to live pre-commit (data-model.md §2.4's original resolved bug).

Phase G step D (agent-contracts.md §3.2/§3.3) replaces the old due_at-only
clarify() call, the fixed render_confirmation_card template, and strict
Y/N/ATTACH keyword matching with resolver_svc.conversation.converse(),
one Gemini call per turn that merges fields, classifies intent (AFFIRM/
DENY/CORRECTION/ATTACH/OTHER) when replying to an already-sent
confirmation, and writes the actual outbound SMS in the user's own
mirrored voice. Real correction handling (a reply that changes a detail
rather than confirming/denying) is genuinely new here — never built
before this step. CORRECTION never publishes on its own, no matter how
complete the merged fields look — only a subsequent, separate AFFIRM turn
ever triggers items.confirmed (ADR 0003's actual mechanism: the LLM only
fills a classified field, pipeline code below does a plain
`if result.intent == "AFFIRM":` before ever publishing — never a second
LLM call interpreting the first one's output).

The dedupe question was step D's one deliberate exception — left as the
fixed "Reply Y to merge, N if it's different" template/classifier, not
what that redesign was about. A user hit exactly that rigid script live
against the deployed demo and objected to it directly; a follow-up in
this same session (below) folds it into converse() too, via
dedupe_candidate_title/awaiting_dedupe_reply — no fixed template left
anywhere in this flow. classify_reply/render_dedupe_question/render_merged
are gone.

Step 13 adds an idempotency guard at the top of /pubsub/push — a real
bug found in step 11's live testing: a concurrent Pub/Sub redelivery of
the same items.extracted message during a slow cold start raced this
handler and 500'd on ADK's InMemorySessionService (its deterministic
session id isn't safe against two in-flight handlers for the same
item). That crash happened to be harmless purely by luck — the winning
request had already sent its SMS first — not because anything actually
guarded against it.

Phase G follow-up (same session as step D): converse() gained
`relates_to_item` (conversation.py's own docstring has the full design) —
when a reply during CLARIFYING or AWAITING_CONFIRMATION doesn't actually
relate to the open item, `_route_as_new_item()` leaves that item
completely untouched and gives the text its own new item via the same
path a first-contact message takes (`create_raw_item` + `items-raw`
publish). The DUPLICATE_SUSPECTED path applies the same `relates_to_item`
check now too (below), on top of its own separate dedupe-question fix.

The guard checks for an existing `conversations` row, not `items.state`
— an earlier draft checked state, and a second real bug (found
verifying this step, not hypothetical) showed why that's wrong:
`_write_item()` commits the state transition in its own transaction,
separate from the later `conversations` INSERT. A genuine failure
between those two writes (reproduced with a deliberately-bad user_id)
left the item stuck at a post-RECEIVED state with no conversation ever
created — a state-only guard would then swallow every future
redelivery as "already done" forever, so the message never reaches 5
delivery attempts and never reaches `dead_letters` at all, defeating
the exact mechanism this step exists to build. The `conversations` row
is only ever written as the last DB step of every success path
(data-model.md §2.4: created unconditionally, the instant processing
actually finishes), so its existence is the one true completion signal.
"""

import logging
import os
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from obligation_engine_shared.db import create_raw_item, get_connection, log_message
from obligation_engine_shared.pubsub import decode_push_envelope, publish
from obligation_engine_shared.schemas import (
    ConfirmedItemMessage,
    ExtractedItemMessage,
    RawItemMessage,
    RoutedReplyMessage,
)
from psycopg.types.json import Json
from twilio.rest import Client as TwilioClient

from resolver_svc.conversation import converse
from resolver_svc.dedupe import (
    DedupeResult,
    classify_match,
    compute_dedupe_hash,
    embed,
    vector_literal,
)
from resolver_svc.templates import render_needs_review

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("resolver_svc")
app = FastAPI()

CONFIDENCE_THRESHOLD = 0.75  # state-machine.md §1.2
MAX_EXCHANGES = 3  # state-machine.md §1.2

# Not secrets by Twilio's own credential model — a SID can't authenticate
# anything without its paired secret (infrastructure.md §4.1). Read from
# env anyway, not hardcoded: keeps them out of source scans entirely
# rather than relying on a reviewer knowing that distinction.
TWILIO_FROM_NUMBER = "+14152365420"


def _twilio_client() -> TwilioClient:
    return TwilioClient(
        os.environ["TWILIO_API_KEY_SID"],
        os.environ["TWILIO_API_KEY_SECRET"],
        os.environ["TWILIO_ACCOUNT_SID"],
    )


def _send_sms(user_id, to: str, body: str) -> None:
    """Sends, then logs to the messages table (migrations/0007) in its own
    short transaction, decoupled from whatever transaction the caller is
    mid-way through — the SMS really was sent regardless of what the
    caller's transaction later does, so the log entry shouldn't be tied to
    its commit/rollback."""
    _t0 = time.monotonic()
    _twilio_client().messages.create(to=to, from_=TWILIO_FROM_NUMBER, body=body)
    _t1 = time.monotonic()
    with get_connection() as log_conn:
        _t2 = time.monotonic()
        log_message(log_conn, user_id, "out", body)
        log_conn.commit()
    _t3 = time.monotonic()
    logger.info(
        "TIMING _send_sms: twilio_send=%.2fs get_connection=%.2fs log_and_commit=%.2fs total=%.2fs",
        _t1 - _t0, _t2 - _t1, _t3 - _t2, _t3 - _t0,
    )


def _write_item(extracted: ExtractedItemMessage, state: str) -> None:
    dedupe_hash = compute_dedupe_hash(extracted.title, extracted.summary)
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE items
            SET type = %s, title = %s, summary = %s, effort_minutes = %s,
                focus_depth = %s, is_scheduled_event = %s, confidence = %s,
                dedupe_hash = %s, state = %s, updated_at = now()
            WHERE id = %s
            """,
            (
                extracted.type,
                extracted.title,
                extracted.summary,
                extracted.effort_minutes,
                extracted.focus_depth,
                extracted.is_scheduled_event,
                extracted.confidence,
                dedupe_hash,
                state,
                str(extracted.item_id),
            ),
        )
        conn.commit()


def _user_phone_and_timezone(conn, user_id) -> tuple[str, str]:
    row = conn.execute(
        "SELECT phone_e164, timezone FROM users WHERE id = %s", (str(user_id),)
    ).fetchone()
    return row[0], row[1]


def _recent_history(conn, user_id, limit: int = 10) -> list[str]:
    """Last N messages for this user, oldest first, formatted as plain
    "user:"/"assistant:" lines — the tone-mirroring context fed to
    converse() (agent-contracts.md §3, migrations/0007_messages_table.sql)."""
    rows = conn.execute(
        "SELECT direction, body FROM messages WHERE user_id = %s "
        "ORDER BY created_at DESC LIMIT %s",
        (str(user_id), limit),
    ).fetchall()
    return [
        f"{'user' if direction == 'in' else 'assistant'}: {body}"
        for direction, body in reversed(rows)
    ]


def _other_items_context(
    conn, user_id, exclude_item_id, tz_name: str, limit: int = 20
) -> list[str]:
    """Cross-item situational awareness (user-directed, real gap found live:
    an assignment confirmed in one conversation was never referenced in a
    completely separate party conversation right after — converse() had no
    structured awareness of anything beyond the current item). COMMITTED
    only, not AWAITING_CONFIRMATION/CLARIFYING — those are still being
    negotiated and could still change, so stating them as settled fact
    would be actively misleading. Same items JOIN obligations WHERE
    state = 'COMMITTED' ORDER BY due_at shape already proven in
    dashboard-svc's /me/items and dispatcher-svc's own queries."""
    rows = conn.execute(
        """
        SELECT i.title, o.due_at
        FROM items i JOIN obligations o ON o.item_id = i.id
        WHERE i.user_id = %s AND i.state = 'COMMITTED' AND i.id != %s AND o.due_at IS NOT NULL
        ORDER BY o.due_at ASC
        LIMIT %s
        """,
        (str(user_id), str(exclude_item_id), limit),
    ).fetchall()
    tz = ZoneInfo(tz_name)
    return [
        f"{title} — due {due_at.astimezone(tz).strftime('%a %-d %b, %-I:%M %p')}"
        for title, due_at in rows
    ]


def _route_as_new_item(conn, original_item_id, user_id, text: str, *, reason: str) -> dict:
    """Spins up a brand-new item for text that arrived while a different
    item was open but doesn't actually relate to it (agent-contracts.md
    §3.5's relates_to_item escape hatch, module docstring above). The open
    item that this text was originally routed against is left completely
    untouched — no state change, no timeout, it's just not force-fed this
    unrelated text. Mirrors ingest-svc's own fresh-message path exactly
    (INSERT RECEIVED row, publish items-raw) via the shared create_raw_item
    helper, so this text gets the same real extraction/dedupe/clarification
    treatment a first-contact message would."""
    new_item_id = create_raw_item(conn, user_id, text)
    conn.commit()
    publish(
        "items-raw",
        RawItemMessage(
            item_id=new_item_id, user_id=user_id, text=text, received_at=datetime.now(UTC)
        ),
    )
    logger.info(
        "item_id=%s %s, routed as new item_id=%s", original_item_id, reason, new_item_id
    )
    return {
        "status": "routed_as_new_item",
        "item_id": str(original_item_id),
        "new_item_id": str(new_item_id),
    }


def _initial_resolved_fields(extracted: ExtractedItemMessage) -> dict:
    """Builds resolved_fields for a freshly-extracted item — every path
    that creates a NEW conversations row from an ExtractedItemMessage
    uses this (data-model.md §2.4/§2.7's scratchpad). due_at stages here
    per the original design; step 15 adds action_type and, for an email
    action, email_recipient/email_draft — none of these have an items
    column either. action_type (and the email fields) are only staged
    for an obligation — omitted entirely for a latent, so a later
    `.get("action_type")` correctly reads back None, matching
    ConfirmedItemMessage's "null for a latent" convention."""
    fields: dict = {}
    if extracted.due_at:
        fields["due_at"] = extracted.due_at.isoformat()
    if extracted.type == "obligation":
        fields["action_type"] = extracted.action_type
        if extracted.action_type == "email":
            if extracted.email_recipient:
                fields["email_recipient"] = extracted.email_recipient
            if extracted.email_draft:
                fields["email_draft"] = extracted.email_draft
    return fields


def _merge_effort_minutes(effort_minutes: int | None, result) -> int | None:
    """effort_minutes lives on the items table itself, not the
    conversations.resolved_fields scratchpad due_at/email_recipient use
    (data-model.md §2.4) — merged into the in-memory value here;
    _persist_effort_minutes_fill writes it back once a DB connection is
    open. Only ever missing (and so only ever filled here) for a scheduled
    event with no stated duration — a task's effort_minutes is always
    already guessed by extractor-svc, so this is a no-op for every other
    item."""
    if result.effort_minutes_filled and result.effort_minutes is not None:
        return result.effort_minutes
    return effort_minutes


def _persist_effort_minutes_fill(conn, item_id, result) -> None:
    if result.effort_minutes_filled and result.effort_minutes is not None:
        conn.execute(
            "UPDATE items SET effort_minutes = %s, updated_at = now() WHERE id = %s",
            (result.effort_minutes, str(item_id)),
        )


def _merge_title(title: str | None, result) -> str | None:
    """Same items-column pattern as effort_minutes above — only ever
    missing (and so only ever filled here) for a scheduled event with no
    identifying detail yet."""
    if result.title_filled and result.title:
        return result.title
    return title


def _persist_title_fill(conn, item_id, result) -> None:
    if result.title_filled and result.title:
        conn.execute(
            "UPDATE items SET title = %s, updated_at = now() WHERE id = %s",
            (result.title, str(item_id)),
        )


_REMINDER_LEAD = timedelta(minutes=30)


def _compute_reminder_times(due_at_iso: str | None) -> tuple[datetime, datetime] | None:
    """One universal rule, user-specified, no effort/type distinction:
    a fixed 30-minute heads-up before due_at, and a reminder AT due_at
    itself. Confirm 30+ minutes ahead of due_at and you get both; confirm
    within 30 minutes of it and you get ONLY the at-due-time reminder,
    nothing more — that half is committer-svc's job (_enqueue_reminder_task
    skips any slot already in the past at commit time), not this
    function's; this always returns both computed instants, before-the-fact.

    Deliberately not effort-scaled — user-directed simplification: effort
    (asking about it, bucketing it, scaling reminder timing off it) added
    real complexity and real bugs for little value. effort_minutes still
    exists elsewhere purely for Calendar event sizing, unrelated to this.

    Naive local, same "naive means local" convention due_at itself already
    carries through this whole pipeline (committer-svc attaches the real
    timezone at commit time). None when due_at isn't known yet — a latent,
    or an obligation still missing it."""
    if not due_at_iso:
        return None
    due_at = datetime.fromisoformat(due_at_iso)
    return due_at - _REMINDER_LEAD, due_at


def _format_reminder_time(dt: datetime) -> str:
    return dt.strftime("%-I:%M %p")


def _future_reminder_strings(
    due_at_iso: str | None, now_local: datetime
) -> tuple[str | None, str | None]:
    """The formatted, message-facing counterpart to _compute_reminder_times
    — filters out any computed instant that's already in the past relative
    to right now, not just at commit time. Real bug, found live: a
    confirmation sent well after the 30-min-heads-up mark had already
    passed still told the user "I'll remind you at 9:53pm" — a time that
    had already come and gone before the message was even composed. This
    is a separate check from committer-svc's own overdue-slot skip (which
    only protects the real send, not what the text claims will happen) —
    both are needed, since the text can be composed well before the
    eventual commit (the very first clarifying/confirmation turn, not
    just the AFFIRM one)."""
    times = _compute_reminder_times(due_at_iso)
    if times is None:
        return None, None
    now_naive = now_local.replace(tzinfo=None)
    r1, r2 = times
    return (
        _format_reminder_time(r1) if r1 > now_naive else None,
        _format_reminder_time(r2) if r2 > now_naive else None,
    )


def _ensure_reminder_mention(
    reply_text: str,
    reminder_1_at_passed: str | None,
    due_at_iso: str | None,
    now_local: datetime,
) -> str:
    """converse() is already asked to state reminder_1_at/reminder_2_at
    naturally whenever they're given as known context going into that
    call. But the one reply that resolves the LAST missing piece needed to
    compute them (due_at itself, on the very turn still_missing empties
    out) can't have had them passed in — nothing could compute them before
    a call that is itself what fills them. This deterministic append
    covers exactly that one gap (reminder_1_at_passed was None going in,
    but due_at is known after merging this turn's result); every other
    case already got a natural, in-voice mention from converse() itself
    and this is a no-op. Mentions only whichever of the two is still
    genuinely ahead of now — one, both, or (rare) neither."""
    if reminder_1_at_passed is not None:
        return reply_text
    r1_str, r2_str = _future_reminder_strings(due_at_iso, now_local)
    times = [t for t in (r1_str, r2_str) if t is not None]
    if not times:
        return reply_text
    if len(times) == 1:
        return f"{reply_text} I'll remind you at {times[0]}."
    return f"{reply_text} I'll remind you at {times[0]} and {times[1]}."


async def _handle_chat(extracted: ExtractedItemMessage) -> None:
    """Phase G step B: a pure-chat message never reaches dedupe/clarification/
    confirmation — extractor-svc already generated the reply, this just sends
    it and closes the item out at CHATTED (state-machine.md's own reasoning
    for not reusing CANCELLED/NEEDS_REVIEW here — different failure/outcome
    semantics). A conversations row is still written (empty resolved_fields)
    purely as the existing idempotency guard's completion signal — reusing
    that mechanism rather than inventing a second one for this one path."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE items SET state = 'CHATTED', updated_at = now() WHERE id = %s",
            (str(extracted.item_id),),
        )
        conn.execute(
            "INSERT INTO conversations (user_id, item_id, resolved_fields) VALUES (%s, %s, %s)",
            (str(extracted.user_id), str(extracted.item_id), Json({})),
        )
        phone, _tz = _user_phone_and_timezone(conn, extracted.user_id)
        conn.commit()

    _send_sms(extracted.user_id, phone, extracted.chat_reply or "hey")
    logger.info("CHATTED item_id=%s", extracted.item_id)


_DEDUPE_INELIGIBLE_STATES = ("CANCELLED", "MERGED", "FAILED")


def _check_duplicate(extracted: ExtractedItemMessage) -> DedupeResult:
    dedupe_hash = compute_dedupe_hash(extracted.title, extracted.summary)

    with get_connection() as conn:
        exact = conn.execute(
            "SELECT id, title FROM items "
            "WHERE user_id = %s AND dedupe_hash = %s AND id != %s AND state != ALL(%s) LIMIT 1",
            (
                str(extracted.user_id),
                dedupe_hash,
                str(extracted.item_id),
                list(_DEDUPE_INELIGIBLE_STATES),
            ),
        ).fetchone()
        if exact is not None:
            return DedupeResult(duplicate_item_id=exact[0], duplicate_title=exact[1])

        vector = vector_literal(embed(extracted.title, extracted.summary))
        # A cancelled/merged/failed item is dead — the user deleted it, or
        # it already got absorbed into something else, or it never
        # completed. None of those are "the same live thing" a fresh
        # message could be a duplicate of, so they're excluded from ever
        # being offered as a dedupe match — real finding: without this, a
        # deleted item stayed a permanently eligible "duplicate" forever,
        # and confirming the match resurrected it via the merge path
        # below instead of actually clearing it.
        match = conn.execute(
            """
            SELECT i.id, i.title, i.type, 1 - (e.embedding <=> %s::vector) AS similarity
            FROM item_embeddings e JOIN items i ON i.id = e.item_id
            WHERE i.user_id = %s AND i.state != ALL(%s)
            ORDER BY e.embedding <=> %s::vector
            LIMIT 1
            """,
            (vector, str(extracted.user_id), list(_DEDUPE_INELIGIBLE_STATES), vector),
        ).fetchone()
        conn.execute(
            "INSERT INTO item_embeddings (item_id, embedding) VALUES (%s, %s::vector)",
            (str(extracted.item_id), vector),
        )
        conn.commit()

    if match is None:
        return DedupeResult()
    match_id, match_title, match_type, similarity = match
    return classify_match(similarity, match_type, match_id, match_title)


async def _start_duplicate_suspected(extracted: ExtractedItemMessage, dedupe: DedupeResult) -> None:
    """The dedupe question itself, in voice (conversation.py's dedupe-
    question note) — replaces the old fixed render_dedupe_question
    template, the one Y/N script left standing through step D and the
    continuity fix, and the direct trigger for finally retiring it."""
    _write_item(extracted, "DUPLICATE_SUSPECTED")

    resolved_fields = _initial_resolved_fields(extracted)
    resolved_fields["_dedupe_match_item_id"] = str(dedupe.duplicate_item_id)
    resolved_fields["_dedupe_match_title"] = dedupe.duplicate_title
    if dedupe.thread_attach_item_id:
        resolved_fields["_thread_attach_item_id"] = str(dedupe.thread_attach_item_id)
        resolved_fields["_thread_attach_title"] = dedupe.thread_attach_title

    with get_connection() as conn:
        phone, tz_name = _user_phone_and_timezone(conn, extracted.user_id)
        history = _recent_history(conn, extracted.user_id)
        other_items = _other_items_context(conn, extracted.user_id, extracted.item_id, tz_name)

    now_local = datetime.now(UTC).astimezone(ZoneInfo(tz_name))
    result = await converse(
        session_id=f"{extracted.item_id}-{uuid4().hex[:8]}",
        now_local=now_local,
        tz_name=tz_name,
        title=extracted.title,
        item_type=extracted.type,
        summary=extracted.summary,
        effort_minutes=extracted.effort_minutes,
        known_fields=resolved_fields,
        missing_fields=extracted.missing_fields,
        awaiting_confirmation=False,
        thread_attach_title=None,
        history=history,
        latest_reply=None,
        dedupe_candidate_title=dedupe.duplicate_title,
        awaiting_dedupe_reply=False,
        other_items=other_items,
        is_scheduled_event=extracted.is_scheduled_event,
    )

    with get_connection() as conn:
        conn.execute(
            "INSERT INTO conversations (user_id, item_id, pending_fields, resolved_fields) "
            "VALUES (%s, %s, %s, %s)",
            (
                str(extracted.user_id),
                str(extracted.item_id),
                extracted.missing_fields,
                Json(resolved_fields),
            ),
        )
        conn.commit()

    _send_sms(extracted.user_id, phone, result.reply_text)
    logger.info(
        "DUPLICATE_SUSPECTED item_id=%s matched_item_id=%s",
        extracted.item_id,
        dedupe.duplicate_item_id,
    )


async def _start_clarification(
    extracted: ExtractedItemMessage, thread_attach: tuple | None = None
) -> str:
    """Runs the first conversation turn for a freshly-extracted item —
    whether or not any fields are missing. Always calls converse() (even
    with an empty missing_fields list) so every item gets a natural,
    in-voice confirmation message rather than a fixed template; this is
    what replaced the old two-branch split in /pubsub/push (a separate
    inline "complete extraction" block that skipped the LLM call
    entirely)."""
    _write_item(extracted, "CLARIFYING")

    with get_connection() as conn:
        phone, tz_name = _user_phone_and_timezone(conn, extracted.user_id)
        history = _recent_history(conn, extracted.user_id)
        other_items = _other_items_context(conn, extracted.user_id, extracted.item_id, tz_name)

    resolved_fields = _initial_resolved_fields(extracted)
    effort_minutes = extracted.effort_minutes
    title = extracted.title
    is_scheduled_event = extracted.is_scheduled_event
    thread_attach_title = thread_attach[1] if thread_attach else None
    now_local = datetime.now(UTC).astimezone(ZoneInfo(tz_name))
    reminder_1_at, reminder_2_at = _future_reminder_strings(
        resolved_fields.get("due_at"), now_local
    )
    result = await converse(
        session_id=f"{extracted.item_id}-{uuid4().hex[:8]}",
        now_local=now_local,
        tz_name=tz_name,
        title=title,
        item_type=extracted.type,
        summary=extracted.summary,
        effort_minutes=effort_minutes,
        known_fields=resolved_fields,
        missing_fields=extracted.missing_fields,
        awaiting_confirmation=False,
        thread_attach_title=thread_attach_title,
        history=history,
        latest_reply=None,
        reminder_1_at=reminder_1_at,
        reminder_2_at=reminder_2_at,
        other_items=other_items,
        is_scheduled_event=is_scheduled_event,
    )
    if result.due_at_filled and result.due_at:
        resolved_fields["due_at"] = result.due_at
    if result.email_recipient_filled and result.email_recipient:
        resolved_fields["email_recipient"] = result.email_recipient
    if thread_attach:
        resolved_fields["_thread_attach_item_id"] = str(thread_attach[0])
        resolved_fields["_thread_attach_title"] = thread_attach[1]
    effort_minutes = _merge_effort_minutes(effort_minutes, result)
    title = _merge_title(title, result)
    reply_text = _ensure_reminder_mention(
        result.reply_text, reminder_1_at, resolved_fields.get("due_at"), now_local
    )

    with get_connection() as conn:
        _persist_effort_minutes_fill(conn, extracted.item_id, result)
        _persist_title_fill(conn, extracted.item_id, result)
        conn.execute(
            """
            INSERT INTO conversations
                (user_id, item_id, exchange_count, pending_fields, resolved_fields)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                str(extracted.user_id),
                str(extracted.item_id),
                1 if result.still_missing else 0,
                result.still_missing,
                Json(resolved_fields),
            ),
        )
        conn.commit()

    if result.still_missing:
        _send_sms(extracted.user_id, phone, result.reply_text)
        logger.info("CLARIFYING item_id=%s sent question 1/%d", extracted.item_id, MAX_EXCHANGES)
        return "clarifying"

    with get_connection() as conn:
        conn.execute(
            "UPDATE items SET state = 'AWAITING_CONFIRMATION', updated_at = now() WHERE id = %s",
            (str(extracted.item_id),),
        )
        conn.commit()
    _send_sms(extracted.user_id, phone, reply_text)
    logger.info("AWAITING_CONFIRMATION item_id=%s (resolved on first pass)", extracted.item_id)
    return "awaiting_confirmation"


@app.post("/pubsub/push")
async def pubsub_push(request: Request):
    envelope = await request.json()
    try:
        extracted = decode_push_envelope(envelope, ExtractedItemMessage)
    except Exception:
        logger.exception("malformed push envelope, could not decode ExtractedItemMessage")
        raise HTTPException(status_code=500, detail="malformed envelope") from None

    with get_connection() as conn:
        convo_exists = conn.execute(
            "SELECT 1 FROM conversations WHERE item_id = %s LIMIT 1", (str(extracted.item_id),)
        ).fetchone()
    if convo_exists is not None:
        # A concurrent redelivery of the same items.extracted message,
        # arriving after the first delivery already finished — the real
        # race found in step 11 (ADK's InMemorySession crashed on the
        # second create_session() call, which happened to 500 before any
        # visible harm, but that was luck, not a guard). Checked against
        # the conversations row rather than items.state != 'RECEIVED':
        # _write_item() commits the state transition in its own earlier
        # transaction, separate from the conversations INSERT — a real
        # failure found empirically verifying this step, where the
        # conversations write itself failed (a bad user_id reference),
        # left the item stuck at a post-RECEIVED state with the
        # conversation never created. A state-only check would have
        # swallowed every future redelivery as "already done" forever,
        # silently defeating the dead-letter path this step exists to
        # build. The conversations row is only ever written as the last
        # DB step of every success path (data-model.md §2.4: created
        # unconditionally, the moment resolver-svc actually finishes),
        # so its existence is the one true completion signal.
        logger.info("skipping already-processed item_id=%s (redelivery)", extracted.item_id)
        return {"status": "already_processed", "item_id": str(extracted.item_id)}

    if not extracted.is_actionable:
        try:
            await _handle_chat(extracted)
        except Exception:
            logger.exception("failed to handle chat item_id=%s", extracted.item_id)
            raise HTTPException(status_code=500, detail="chat handling failed") from None
        return {"status": "chatted", "item_id": str(extracted.item_id)}

    try:
        dedupe = _check_duplicate(extracted)
    except Exception:
        logger.exception("dedupe check failed item_id=%s", extracted.item_id)
        raise HTTPException(status_code=500, detail="dedupe check failed") from None

    if dedupe.duplicate_item_id is not None:
        try:
            await _start_duplicate_suspected(extracted, dedupe)
        except Exception:
            logger.exception("failed to start duplicate-suspected item_id=%s", extracted.item_id)
            raise HTTPException(status_code=500, detail="duplicate check start failed") from None
        return {"status": "duplicate_suspected", "item_id": str(extracted.item_id)}

    thread_attach = (
        (dedupe.thread_attach_item_id, dedupe.thread_attach_title)
        if dedupe.thread_attach_item_id
        else None
    )

    # Every non-duplicate item runs one conversation turn, whether or not
    # any fields are missing — state-machine.md §1.2's "Resolved gap": low
    # confidence alone doesn't get a manufactured question about nothing,
    # the natural confirmation message's own implicit "or send a
    # correction" is the safety net for it.
    try:
        status = await _start_clarification(extracted, thread_attach)
    except Exception:
        logger.exception("failed to start conversation item_id=%s", extracted.item_id)
        raise HTTPException(status_code=500, detail="conversation start failed") from None

    return {"status": status, "item_id": str(extracted.item_id)}


async def _handle_clarification_reply(
    conn,
    user_id,
    item_id,
    phone,
    tz_name,
    title,
    item_type,
    summary,
    effort_minutes,
    is_scheduled_event,
    latest_reply,
) -> dict:
    convo_row = conn.execute(
        "SELECT pending_fields, resolved_fields, exchange_count FROM conversations "
        "WHERE item_id = %s ORDER BY last_message_at DESC LIMIT 1",
        (str(item_id),),
    ).fetchone()
    pending_fields, resolved_fields, exchange_count = convo_row
    thread_attach_title = resolved_fields.get("_thread_attach_title")
    history = _recent_history(conn, user_id)
    other_items = _other_items_context(conn, user_id, item_id, tz_name)

    now_local = datetime.now(UTC).astimezone(ZoneInfo(tz_name))
    reminder_1_at, reminder_2_at = _future_reminder_strings(
        resolved_fields.get("due_at"), now_local
    )
    result = await converse(
        session_id=f"{item_id}-{uuid4().hex[:8]}",
        now_local=now_local,
        tz_name=tz_name,
        title=title,
        item_type=item_type,
        summary=summary,
        effort_minutes=effort_minutes,
        known_fields=resolved_fields,
        missing_fields=pending_fields,
        awaiting_confirmation=False,
        thread_attach_title=thread_attach_title,
        history=history,
        latest_reply=latest_reply,
        reminder_1_at=reminder_1_at,
        reminder_2_at=reminder_2_at,
        other_items=other_items,
        is_scheduled_event=is_scheduled_event,
    )
    if not result.relates_to_item:
        return _route_as_new_item(
            conn, item_id, user_id, latest_reply, reason="reply unrelated during CLARIFYING"
        )

    if result.due_at_filled and result.due_at:
        resolved_fields = {**resolved_fields, "due_at": result.due_at}
    if result.email_recipient_filled and result.email_recipient:
        resolved_fields = {**resolved_fields, "email_recipient": result.email_recipient}
    effort_minutes = _merge_effort_minutes(effort_minutes, result)
    _persist_effort_minutes_fill(conn, item_id, result)
    title = _merge_title(title, result)
    _persist_title_fill(conn, item_id, result)

    if result.still_missing:
        if exchange_count < MAX_EXCHANGES:
            next_count = exchange_count + 1
            conn.execute(
                """
                UPDATE conversations
                SET exchange_count = %s, pending_fields = %s, resolved_fields = %s,
                    last_message_at = now()
                WHERE item_id = %s
                """,
                (next_count, result.still_missing, Json(resolved_fields), str(item_id)),
            )
            conn.commit()
            _send_sms(user_id, phone, result.reply_text)
            logger.info(
                "CLARIFYING item_id=%s sent question %d/%d", item_id, next_count, MAX_EXCHANGES
            )
            return {"status": "clarifying", "item_id": str(item_id)}

        conn.execute(
            "UPDATE items SET state = 'NEEDS_REVIEW', updated_at = now() WHERE id = %s",
            (str(item_id),),
        )
        conn.execute(
            "UPDATE conversations SET resolved_fields = %s, last_message_at = now() "
            "WHERE item_id = %s",
            (Json(resolved_fields), str(item_id)),
        )
        conn.commit()
        _send_sms(user_id, phone, render_needs_review(title))
        logger.info("NEEDS_REVIEW item_id=%s (exhausted %d exchanges)", item_id, MAX_EXCHANGES)
        return {"status": "needs_review", "item_id": str(item_id)}

    reply_text = _ensure_reminder_mention(
        result.reply_text, reminder_1_at, resolved_fields.get("due_at"), now_local
    )
    conn.execute(
        "UPDATE items SET state = 'AWAITING_CONFIRMATION', updated_at = now() WHERE id = %s",
        (str(item_id),),
    )
    conn.execute(
        "UPDATE conversations SET resolved_fields = %s, pending_fields = %s, "
        "last_message_at = now() WHERE item_id = %s",
        (Json(resolved_fields), [], str(item_id)),
    )
    conn.commit()
    _send_sms(user_id, phone, reply_text)
    logger.info("AWAITING_CONFIRMATION item_id=%s (clarification resolved)", item_id)
    return {"status": "awaiting_confirmation", "item_id": str(item_id)}


async def _handle_duplicate_reply(
    conn,
    user_id,
    item_id,
    phone,
    tz_name,
    title,
    item_type,
    summary,
    effort_minutes,
    is_scheduled_event,
    text,
) -> dict:
    """The dedupe reply, in voice (conversation.py's dedupe-question note):
    classify_reply()'s plain Y/N keyword matching is retired here too —
    the exact same "Reply Y to merge, N if it's different" script the
    question itself used to send, now interpreted by converse() with
    awaiting_dedupe_reply=True instead. relates_to_item still applies:
    genuinely unrelated text gets its own new item, same as every other
    open-item path."""
    convo_row = conn.execute(
        "SELECT pending_fields, resolved_fields FROM conversations WHERE item_id = %s "
        "ORDER BY last_message_at DESC LIMIT 1",
        (str(item_id),),
    ).fetchone()
    pending_fields, resolved_fields = convo_row
    match_title = resolved_fields.get("_dedupe_match_title") or "that item"
    thread_attach_title = resolved_fields.get("_thread_attach_title")
    history = _recent_history(conn, user_id)
    other_items = _other_items_context(conn, user_id, item_id, tz_name)
    now_local = datetime.now(UTC).astimezone(ZoneInfo(tz_name))
    dedupe_result = await converse(
        session_id=f"{item_id}-{uuid4().hex[:8]}",
        now_local=now_local,
        tz_name=tz_name,
        title=title,
        item_type=item_type,
        summary=summary,
        effort_minutes=effort_minutes,
        known_fields=resolved_fields,
        missing_fields=[],
        awaiting_confirmation=False,
        thread_attach_title=thread_attach_title,
        history=history,
        latest_reply=text,
        dedupe_candidate_title=match_title,
        awaiting_dedupe_reply=True,
        other_items=other_items,
        is_scheduled_event=is_scheduled_event,
    )
    if not dedupe_result.relates_to_item:
        return _route_as_new_item(
            conn, item_id, user_id, text, reason="dedupe reply unrelated"
        )

    if dedupe_result.intent == "AFFIRM":
        # Real bug, found live: this used to write state='MERGED' without
        # ever setting parent_item_id, despite the log line below already
        # claiming "into=<match>" — the link it named was never actually
        # written anywhere.
        conn.execute(
            "UPDATE items SET state = 'MERGED', parent_item_id = %s, updated_at = now() "
            "WHERE id = %s",
            (resolved_fields.get("_dedupe_match_item_id"), str(item_id)),
        )
        conn.commit()
        _send_sms(user_id, phone, dedupe_result.reply_text)
        logger.info(
            "MERGED item_id=%s into=%s", item_id, resolved_fields.get("_dedupe_match_item_id")
        )
        return {"status": "merged", "item_id": str(item_id)}

    if dedupe_result.intent == "DENY":
        # DENY proceeds to the completeness check as if no match existed
        # (state-machine.md §1.1 point 2) — pending_fields here is the
        # original missing_fields staged by _start_duplicate_suspected.
        # A second, separate converse() call, exactly the multi-turn
        # pattern every other path already uses (this reply already did
        # its one job — classifying the dedupe question — dedupe_result's
        # own reply_text was just the "keeping separate" acknowledgment,
        # not a real attempt at resolving missing fields).
        reminder_1_at, reminder_2_at = _future_reminder_strings(
        resolved_fields.get("due_at"), now_local
    )
        result = await converse(
            session_id=f"{item_id}-{uuid4().hex[:8]}",
            now_local=now_local,
            tz_name=tz_name,
            title=title,
            item_type=item_type,
            summary=summary,
            effort_minutes=effort_minutes,
            known_fields=resolved_fields,
            missing_fields=pending_fields,
            awaiting_confirmation=False,
            thread_attach_title=thread_attach_title,
            history=history,
            latest_reply=None,
            reminder_1_at=reminder_1_at,
            reminder_2_at=reminder_2_at,
            other_items=other_items,
            is_scheduled_event=is_scheduled_event,
        )
        if result.due_at_filled and result.due_at:
            resolved_fields = {**resolved_fields, "due_at": result.due_at}
        if result.email_recipient_filled and result.email_recipient:
            resolved_fields = {**resolved_fields, "email_recipient": result.email_recipient}
        effort_minutes = _merge_effort_minutes(effort_minutes, result)
        _persist_effort_minutes_fill(conn, item_id, result)
        title = _merge_title(title, result)
        _persist_title_fill(conn, item_id, result)

        if result.still_missing:
            conn.execute(
                "UPDATE items SET state = 'CLARIFYING', updated_at = now() WHERE id = %s",
                (str(item_id),),
            )
            conn.execute(
                "UPDATE conversations SET exchange_count = 1, pending_fields = %s, "
                "resolved_fields = %s, last_message_at = now() WHERE item_id = %s",
                (result.still_missing, Json(resolved_fields), str(item_id)),
            )
            conn.commit()
            _send_sms(user_id, phone, result.reply_text)
            logger.info(
                "CLARIFYING item_id=%s sent question 1/%d (post-dedupe)", item_id, MAX_EXCHANGES
            )
            return {"status": "clarifying", "item_id": str(item_id)}

        reply_text = _ensure_reminder_mention(
            result.reply_text, reminder_1_at, resolved_fields.get("due_at"), now_local
        )
        conn.execute(
            "UPDATE items SET state = 'AWAITING_CONFIRMATION', updated_at = now() WHERE id = %s",
            (str(item_id),),
        )
        conn.execute(
            "UPDATE conversations SET pending_fields = %s, resolved_fields = %s, "
            "last_message_at = now() WHERE item_id = %s",
            ([], Json(resolved_fields), str(item_id)),
        )
        conn.commit()
        _send_sms(user_id, phone, reply_text)
        logger.info("AWAITING_CONFIRMATION item_id=%s (post-dedupe, resolved)", item_id)
        return {"status": "awaiting_confirmation", "item_id": str(item_id)}

    # OTHER — genuinely ambiguous about whether this is the same item
    # (relates_to_item already routed genuinely unrelated text away above).
    # Used to go completely silent here (the old classify_reply-based
    # system had no OTHER case at all — anything that wasn't a literal Y/N
    # match was dropped with no SMS sent); now, like every other OTHER
    # case in this flow, it gets a real natural clarifying reply instead.
    _send_sms(user_id, phone, dedupe_result.reply_text)
    logger.info("dedupe reply ambiguous item_id=%s text=%r", item_id, text)
    return {"status": "unhandled_reply", "item_id": str(item_id)}


async def _handle_confirmation_reply(
    conn,
    user_id,
    item_id,
    phone,
    tz_name,
    title,
    item_type,
    summary,
    effort_minutes,
    is_scheduled_event,
    latest_reply,
) -> dict:
    convo_row = conn.execute(
        "SELECT resolved_fields FROM conversations WHERE item_id = %s "
        "ORDER BY last_message_at DESC LIMIT 1",
        (str(item_id),),
    ).fetchone()
    resolved_fields = convo_row[0] if convo_row else {}
    thread_attach_title = resolved_fields.get("_thread_attach_title")
    history = _recent_history(conn, user_id)
    other_items = _other_items_context(conn, user_id, item_id, tz_name)

    now_local = datetime.now(UTC).astimezone(ZoneInfo(tz_name))
    reminder_1_at, reminder_2_at = _future_reminder_strings(
        resolved_fields.get("due_at"), now_local
    )
    result = await converse(
        session_id=f"{item_id}-{uuid4().hex[:8]}",
        now_local=now_local,
        tz_name=tz_name,
        title=title,
        item_type=item_type,
        summary=summary,
        effort_minutes=effort_minutes,
        known_fields=resolved_fields,
        missing_fields=[],
        awaiting_confirmation=True,
        thread_attach_title=thread_attach_title,
        history=history,
        latest_reply=latest_reply,
        reminder_1_at=reminder_1_at,
        reminder_2_at=reminder_2_at,
        other_items=other_items,
        is_scheduled_event=is_scheduled_event,
    )
    if not result.relates_to_item:
        return _route_as_new_item(
            conn, item_id, user_id, latest_reply,
            reason="reply unrelated during AWAITING_CONFIRMATION",
        )

    if result.intent == "AFFIRM":
        # Deliberately the raw, unfiltered _compute_reminder_times here —
        # this becomes the real persisted obligations.reminder_1_at/
        # reminder_2_at row, and committer-svc's own overdue-slot check
        # (_enqueue_reminder_task, "skip if already past at commit time")
        # is what actually decides whether it fires, not this function.
        # Storing the value even when it'll never fire is correct, not a
        # bug — it's a real record of what the ideal early heads-up would
        # have been.
        reminder_times = _compute_reminder_times(resolved_fields.get("due_at"))
        confirmed = ConfirmedItemMessage(
            item_id=item_id,
            user_id=user_id,
            type=item_type,
            title=title,
            summary=summary,
            due_at=resolved_fields.get("due_at"),
            effort_minutes=effort_minutes,
            action_type=resolved_fields.get("action_type"),
            email_recipient=resolved_fields.get("email_recipient"),
            email_draft=resolved_fields.get("email_draft"),
            reminder_1_at=reminder_times[0] if reminder_times else None,
            reminder_2_at=reminder_times[1] if reminder_times else None,
        )
        publish("items-confirmed", confirmed)
        conn.execute(
            "UPDATE items SET state = 'CONFIRMED', updated_at = now() WHERE id = %s",
            (str(item_id),),
        )
        conn.commit()
        _send_sms(user_id, phone, result.reply_text)
        logger.info("CONFIRMED item_id=%s (real AFFIRM reply)", item_id)
        return {"status": "confirmed", "item_id": str(item_id)}

    if result.intent == "DENY":
        conn.execute(
            "UPDATE items SET state = 'CANCELLED', updated_at = now() WHERE id = %s",
            (str(item_id),),
        )
        conn.commit()
        _send_sms(user_id, phone, result.reply_text)
        logger.info("CANCELLED item_id=%s (real DENY reply)", item_id)
        return {"status": "cancelled", "item_id": str(item_id)}

    if result.intent == "CORRECTION":
        if result.due_at_filled and result.due_at:
            resolved_fields = {**resolved_fields, "due_at": result.due_at}
        if result.email_recipient_filled and result.email_recipient:
            resolved_fields = {**resolved_fields, "email_recipient": result.email_recipient}
        _persist_effort_minutes_fill(conn, item_id, result)
        _persist_title_fill(conn, item_id, result)
        conn.execute(
            "UPDATE conversations SET resolved_fields = %s, last_message_at = now() "
            "WHERE item_id = %s",
            (Json(resolved_fields), str(item_id)),
        )
        # Deliberately stays AWAITING_CONFIRMATION — see module docstring:
        # a correction never publishes on its own, no matter how complete
        # the merged fields look. Only a subsequent, separate AFFIRM turn
        # ever triggers items.confirmed.
        conn.commit()
        _send_sms(user_id, phone, result.reply_text)
        logger.info("AWAITING_CONFIRMATION item_id=%s (correction applied)", item_id)
        return {"status": "awaiting_confirmation", "item_id": str(item_id)}

    if result.intent == "ATTACH":
        target_id = resolved_fields.get("_thread_attach_item_id")
        if target_id:
            conn.execute(
                "UPDATE items SET parent_item_id = %s, updated_at = now() WHERE id = %s",
                (target_id, str(item_id)),
            )
            conn.commit()
            _send_sms(user_id, phone, result.reply_text)
            logger.info("thread-attached item_id=%s to=%s", item_id, target_id)
            return {"status": "attached", "item_id": str(item_id)}
        # No candidate on record for this item — falls through to the
        # generic reply below, same as any other stray text.

    # OTHER (or ATTACH with no real candidate on record) — reply naturally,
    # no state change, no write.
    _send_sms(user_id, phone, result.reply_text)
    logger.info(
        "reply outside AFFIRM/DENY item_id=%s intent=%s", item_id, result.intent
    )
    return {"status": "unhandled_reply", "item_id": str(item_id)}


@app.post("/reply")
async def reply(payload: RoutedReplyMessage):
    _req_t0 = time.monotonic()
    try:
        with get_connection() as conn:
            _req_t1 = time.monotonic()
            logger.info("TIMING /reply: get_connection=%.2fs", _req_t1 - _req_t0)
            item_row = conn.execute(
                "SELECT type, title, summary, effort_minutes, is_scheduled_event, state "
                "FROM items WHERE id = %s",
                (str(payload.item_id),),
            ).fetchone()
            if item_row is None:
                raise HTTPException(status_code=404, detail="unknown item_id")
            item_type, title, summary, effort_minutes, is_scheduled_event, state = item_row
            phone, tz_name = _user_phone_and_timezone(conn, payload.user_id)

            if state == "DUPLICATE_SUSPECTED":
                return await _handle_duplicate_reply(
                    conn, payload.user_id, payload.item_id, phone, tz_name, title, item_type,
                    summary, effort_minutes, is_scheduled_event, payload.text,
                )

            if state == "CLARIFYING":
                return await _handle_clarification_reply(
                    conn,
                    payload.user_id,
                    payload.item_id,
                    phone,
                    tz_name,
                    title,
                    item_type,
                    summary,
                    effort_minutes,
                    is_scheduled_event,
                    payload.text,
                )

            if state == "AWAITING_CONFIRMATION":
                return await _handle_confirmation_reply(
                    conn,
                    payload.user_id,
                    payload.item_id,
                    phone,
                    tz_name,
                    title,
                    item_type,
                    summary,
                    effort_minutes,
                    is_scheduled_event,
                    payload.text,
                )

            logger.warning(
                "reply routed for item_id=%s in unexpected state=%s", payload.item_id, state
            )
            return {"status": "unexpected_state", "item_id": str(payload.item_id)}
    except HTTPException:
        raise
    except Exception:
        logger.exception("reply handling failed item_id=%s", payload.item_id)
        raise HTTPException(status_code=500, detail="reply handling failed") from None
    finally:
        logger.info("TIMING /reply: total=%.2fs", time.monotonic() - _req_t0)


@app.get("/health")
async def health():
    return {"status": "ok"}
