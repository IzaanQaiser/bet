-- sa-committer was only ever granted INSERT on obligations/latents (write-only
-- by the original design — committer-svc never needed to read either table
-- back). Step 14's idempotency guard (_already_committed(), main.py) needs to
-- check whether a row already exists before writing, to distinguish "this
-- exact message already succeeded" from "this item has an unrelated COMMITTED
-- history" (a latent's original commit, before dispatcher-svc's accept-path
-- publish tries to commit it a second time as an obligation) — found via a
-- real permission-denied error verifying step 14 against real infra.
GRANT SELECT ON obligations TO "sa-committer@obligation-engine-hack.iam";
GRANT SELECT ON latents TO "sa-committer@obligation-engine-hack.iam";
