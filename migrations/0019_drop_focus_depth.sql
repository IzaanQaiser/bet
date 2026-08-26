-- User-directed, v1: "deep work" (focus_depth) removed entirely — too
-- verbose for what it added. Was: a guessed shallow/deep classification
-- per item, consumed only by capacity_engine.py's depth_fit() (a
-- fragmentation-index reward/penalty curve) and block_fit()'s deep-work
-- margin (was 150% of the estimate vs. shallow's exact fit) — both
-- removed in the same change. block_fit is now one universal rule for
-- every idea. Column drop takes its inline CHECK constraint with it.
ALTER TABLE items DROP COLUMN focus_depth;
