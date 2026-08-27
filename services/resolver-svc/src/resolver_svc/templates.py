"""Deterministic SMS templates — agent-contracts.md §3.4. No LLM call for
any of this; every field is computed, never generated.

Phase G step D retired render_confirmation_card/render_attached/
render_cancelled: the main obligation confirm flow (including AFFIRM/DENY/
CORRECTION/ATTACH replies) now gets its outbound text from
resolver_svc.conversation's LLM call instead of fixed templates
(agent-contracts.md §3.3). A same-session follow-up then retired
render_dedupe_question/render_merged too — the dedupe question's own
fixed "Reply Y to merge, N if it's different" script, left standing
through step D as a deliberate exception, hit a real user directly
against the deployed demo; §3.5's converse() call absorbed that flow
(dedupe_candidate_title/awaiting_dedupe_reply). What's left here is only
the exhaustion terminal message — a deliberate, still-templated exception
of its own, unrelated to either of the above."""


def render_needs_review(title: str | None) -> str:
    return (
        f"I couldn't get all the details for \"{title or 'that'}\". I've saved what I have. "
        f"Send it again with more detail if you'd like me to try again."
    )
