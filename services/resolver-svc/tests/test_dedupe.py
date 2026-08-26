"""docs/engineering/test-plan.md step 12 — pure unit tests for the dedupe
hash and the similarity-threshold decision, no DB/embedding API involved."""

from uuid import uuid4

from resolver_svc.dedupe import (
    DUPLICATE_THRESHOLD,
    THREAD_ATTACH_THRESHOLD,
    classify_match,
    compute_dedupe_hash,
    cosine_similarity,
)


def test_dedupe_hash_normalizes_case_and_whitespace():
    a = compute_dedupe_hash("Pay Rent", "  Pay rent by Friday.  ")
    b = compute_dedupe_hash("  pay rent  ", "pay RENT by friday.")
    assert a == b


def test_dedupe_hash_differs_for_different_content():
    a = compute_dedupe_hash("Pay rent", "Pay rent by Friday.")
    b = compute_dedupe_hash("Buy milk", "Buy milk and eggs.")
    assert a != b


def test_dedupe_hash_handles_null_title():
    """A scheduled event with no identifying detail yet (extractor-svc's
    title/missing_fields rule) has title=None until clarification resolves
    it — must not crash, normalizes the same as an empty title."""
    a = compute_dedupe_hash(None, "A meeting.")
    b = compute_dedupe_hash("", "A meeting.")
    assert a == b


def test_similarity_boundary_at_0_92():
    match_id = uuid4()
    at_threshold = classify_match(DUPLICATE_THRESHOLD, "obligation", match_id, "Pay rent")
    assert at_threshold.duplicate_item_id == match_id

    just_below = classify_match(DUPLICATE_THRESHOLD - 0.001, "obligation", match_id, "Pay rent")
    assert just_below.duplicate_item_id is None


def test_similarity_boundary_at_0_82():
    match_id = uuid4()
    at_threshold = classify_match(THREAD_ATTACH_THRESHOLD, "latent", match_id, "Learn pottery")
    assert at_threshold.thread_attach_item_id == match_id

    just_below = classify_match(
        THREAD_ATTACH_THRESHOLD - 0.001, "latent", match_id, "Learn pottery"
    )
    assert just_below.thread_attach_item_id is None
    assert just_below.duplicate_item_id is None


def test_thread_attach_band_ignored_for_obligation_match():
    """state-machine.md §1.1 point 3: the 0.82-0.92 offer only applies
    when the existing match is a latent — an obligation match in that
    band gets no dedupe action at all."""
    match_id = uuid4()
    result = classify_match(0.85, "obligation", match_id, "Pay rent")
    assert result.duplicate_item_id is None
    assert result.thread_attach_item_id is None


def test_below_thread_attach_threshold_no_action():
    match_id = uuid4()
    result = classify_match(0.5, "latent", match_id, "Learn pottery")
    assert result.duplicate_item_id is None
    assert result.thread_attach_item_id is None


def test_cosine_similarity_identical_vectors_is_one():
    v = [0.6, 0.8, 0.0]
    assert abs(cosine_similarity(v, v) - 1.0) < 1e-9


def test_cosine_similarity_orthogonal_vectors_is_zero():
    assert abs(cosine_similarity([1.0, 0.0], [0.0, 1.0])) < 1e-9


def test_cosine_similarity_real_near_duplicate_fixture_crosses_thread_attach_band():
    """Fixture pair loosely modeled on the real scratch-test finding
    documented in agent-contracts.md §3.1 (a close paraphrase scored
    ~0.92 for real) — a near-duplicate pair should land clearly above
    the thread-attach floor, an unrelated pair clearly below it."""
    near_dup_a = [0.9, 0.1, 0.05]
    near_dup_b = [0.88, 0.12, 0.04]
    unrelated = [0.1, 0.9, 0.4]

    assert cosine_similarity(near_dup_a, near_dup_b) >= THREAD_ATTACH_THRESHOLD
    assert cosine_similarity(near_dup_a, unrelated) < THREAD_ATTACH_THRESHOLD
