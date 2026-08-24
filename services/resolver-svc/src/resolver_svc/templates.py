"""Deterministic SMS templates — agent-contracts.md §3.1/§3.4. No LLM call
for any of this; every field is computed, never generated.

Phase G step D retired render_confirmation_card/render_attached/
render_cancelled: the main obligation confirm flow (including AFFIRM/DENY/
CORRECTION/ATTACH replies) now gets its outbound text from
resolver_svc.conversation's LLM call instead of fixed templates
(agent-contracts.md §3.3). What's left here is the dedupe question and the
terminal messages — deliberately untouched by step D, not what the
redesign was about."""


def render_dedupe_question(existing_title: str) -> str:
    return f'Is this the same as "{existing_title}"?\nReply Y to merge, N if it\'s different.'


def render_merged(existing_title: str) -> str:
    return f'Got it — that\'s the same as "{existing_title}". Nothing new added.'


def render_needs_review(title: str) -> str:
    return (
        f"I couldn't get all the details for \"{title}\" — I've saved what I have. "
        f"Send it again with more detail if you'd like me to try again."
    )
