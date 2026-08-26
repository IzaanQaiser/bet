"""Unit coverage for conversation.py's pure, non-prompt-dependent logic.
The prompt itself (field merging, phrasing rules) is verified empirically
against real Vertex AI per this project's established pattern, not here —
this file covers only the deterministic Python around that call."""

from resolver_svc.conversation import (
    ConversationTurnResult,
    _reconcile_still_missing,
    _round_to_bucket,
)


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


def test_reconcile_still_missing_readds_a_silently_dropped_field():
    """Regression guard for the real production bug _reconcile_still_missing's
    own docstring describes: the model was given due_at as missing and
    never resolved it (due_at_filled=False) but dropped it from its own
    still_missing output anyway."""
    result = ConversationTurnResult(
        due_at_filled=False,
        title_filled=True,
        title="Coding interview",
        effort_minutes_filled=True,
        effort_minutes=240,
        still_missing=["effort_minutes", "title"],  # due_at silently missing
        reply_text="got it, coding interview is locked in.",
    )
    reconciled = _reconcile_still_missing(["due_at", "effort_minutes", "title"], result)
    assert "due_at" in reconciled


def test_reconcile_still_missing_leaves_resolved_fields_out():
    result = ConversationTurnResult(
        due_at_filled=True,
        due_at="2026-08-27T14:00:00",
        still_missing=[],
        reply_text="got it, due friday at 2pm.",
    )
    reconciled = _reconcile_still_missing(["due_at"], result)
    assert reconciled == []


def test_reconcile_still_missing_no_duplicates_when_model_already_kept_it():
    result = ConversationTurnResult(
        due_at_filled=False, still_missing=["due_at"], reply_text="when's it due?"
    )
    reconciled = _reconcile_still_missing(["due_at"], result)
    assert reconciled == ["due_at"]
