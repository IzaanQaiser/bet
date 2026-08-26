"""Unit coverage for conversation.py's pure, non-prompt-dependent logic.
The prompt itself (field merging, phrasing rules) is verified empirically
against real Vertex AI per this project's established pattern, not here —
this file covers only the deterministic Python around that call."""

from resolver_svc.conversation import _round_to_bucket


def test_round_to_bucket_exact_values_unchanged():
    assert _round_to_bucket(15) == 15
    assert _round_to_bucket(30) == 30
    assert _round_to_bucket(60) == 60
    assert _round_to_bucket(120) == 120
    assert _round_to_bucket(240) == 240


def test_round_to_bucket_rounds_to_nearest():
    assert _round_to_bucket(20) == 15
    assert _round_to_bucket(50) == 60
    assert _round_to_bucket(200) == 240


def test_round_to_bucket_rounds_up_on_exact_tie():
    # 90 is equidistant from 60 and 120 — underestimating available work
    # time is the worse failure mode, so ties round up.
    assert _round_to_bucket(90) == 120
    # 180 is equidistant from 120 and 240.
    assert _round_to_bucket(180) == 240
