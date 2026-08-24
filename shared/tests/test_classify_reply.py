"""docs/engineering/test-plan.md step 9 — the one shared implementation
reused identically by resolver-svc and dispatcher-svc (agent-contracts.md
§4.3). No I/O."""

import pytest
from obligation_engine_shared.reply_classifier import (
    AFFIRMATIVE,
    ATTACH,
    NEGATIVE,
    SNOOZE,
    classify_reply,
)


@pytest.mark.parametrize("text", sorted(AFFIRMATIVE))
def test_affirmative_strings_map_to_y(text):
    assert classify_reply(text) == "Y"


@pytest.mark.parametrize("text", sorted(NEGATIVE))
def test_negative_strings_map_to_n(text):
    assert classify_reply(text) == "N"


@pytest.mark.parametrize("text", sorted(SNOOZE))
def test_snooze_strings_map_to_later(text):
    assert classify_reply(text) == "LATER"


@pytest.mark.parametrize("text", sorted(ATTACH))
def test_attach_strings_map_to_attach(text):
    assert classify_reply(text) == "ATTACH"


def test_case_insensitive_and_whitespace_trimmed():
    assert classify_reply("  YES  ") == "Y"
    assert classify_reply("No\n") == "N"
    assert classify_reply("Later") == "LATER"


def test_arbitrary_strings_map_to_other():
    assert classify_reply("actually can we move it to Friday") == "OTHER"
    assert classify_reply("") == "OTHER"
    assert classify_reply("maybe") == "OTHER"
