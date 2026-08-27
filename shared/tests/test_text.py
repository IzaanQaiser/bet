from obligation_engine_shared.text import strip_em_dash


def test_strip_em_dash_leaves_dash_free_text_untouched():
    assert strip_em_dash("bet, locked in.") == "bet, locked in."


def test_strip_em_dash_replaces_spaced_dash_with_comma():
    """No em dash, ever, in anything the bot says (user-directed)."""
    assert strip_em_dash("alright, sounds good — confirm?") == "alright, sounds good, confirm?"


def test_strip_em_dash_handles_multiple_occurrences():
    assert strip_em_dash("a — b — c") == "a, b, c"


def test_strip_em_dash_trailing_dash_leaves_no_dangling_comma():
    assert strip_em_dash("locked in —") == "locked in"
