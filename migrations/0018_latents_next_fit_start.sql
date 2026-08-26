-- User-directed: the dashboard should show, for each idea, when it could
-- actually happen — not just a static "someday" label. dispatcher-svc
-- recomputes this on every /dispatch run for every committed latent
-- (capacity-engine.md's own note on why eligibility gates don't apply to
-- this computation — it's a display preview, not a proactive-suggestion
-- decision). NULL when no day in the current 7-day forward window has a
-- block big enough for this item's effort/depth.
ALTER TABLE latents ADD COLUMN next_fit_start timestamptz;
