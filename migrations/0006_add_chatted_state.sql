-- Phase G step B: pure-chat messages ("hello", "yo wsg bro") short-circuit
-- to a new terminal state instead of being forced into an obligation/latent
-- shape. Not CANCELLED (implies a rejected real candidate) or NEEDS_REVIEW
-- (implies an incomplete obligation) — state-machine.md already rejected
-- folding a different failure semantic into an existing state for the same
-- reason (see its EXPIRED/CANCELLED discussion). No column changes needed:
-- items.type is already nullable (migration 0002), so a chat item has
-- type=NULL, title=NULL, summary=NULL.

ALTER TABLE items DROP CONSTRAINT items_state_check;
ALTER TABLE items ADD CONSTRAINT items_state_check CHECK (state IN (
    'RECEIVED', 'EXTRACTED', 'DUPLICATE_SUSPECTED',
    'CLARIFYING', 'NEEDS_REVIEW', 'AWAITING_CONFIRMATION',
    'CANCELLED', 'CONFIRMED', 'COMMITTED', 'MERGED', 'FAILED', 'CHATTED'
));
