"""Shared, deterministic reply classifier — agent-contracts.md §4.3. Used
identically by resolver-svc (confirmation Y/N) and dispatcher-svc
(suggestion Y/N/Later) — one implementation, not two copies that could
drift, per docs/engineering/conventions.md."""

from typing import Literal

AFFIRMATIVE = {"y", "yes", "yeah", "yep", "confirm", "ok", "okay", "sure"}
NEGATIVE = {"n", "no", "nope", "cancel", "nah"}
SNOOZE = {"later", "snooze", "not now", "l"}
ATTACH = {"a", "attach"}


def classify_reply(text: str) -> Literal["Y", "N", "LATER", "ATTACH", "OTHER"]:
    normalized = text.strip().lower()
    if normalized in AFFIRMATIVE:
        return "Y"
    if normalized in NEGATIVE:
        return "N"
    if normalized in SNOOZE:
        return "LATER"
    if normalized in ATTACH:
        return "ATTACH"
    return "OTHER"
