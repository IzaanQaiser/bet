"""Outbound-SMS text helpers, shared so resolver-svc and dispatcher-svc
can't drift on something the user explicitly, repeatedly cares about —
same reasoning reply_classifier.py already uses for classify_reply."""

import re


def strip_em_dash(text: str) -> str:
    """No em dash, ever, in anything the bot says (user-directed). The
    real guarantee applied at each service's own _send_sms, the one
    choke point every outbound message body passes through regardless of
    where it came from (a template literal, an LLM's reply_text, or a
    user-supplied title/summary interpolated into either). Replaced with
    a comma rather than stripped outright so the sentence still reads
    naturally; the surrounding-whitespace collapse avoids "word , word"
    from the typical " — " case."""
    if "—" not in text:
        return text
    return re.sub(r"\s*—\s*", ", ", text).rstrip(", ")
