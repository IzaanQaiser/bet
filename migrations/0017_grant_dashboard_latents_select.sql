-- Real bug, found live: /me/items' committed query now UNIONs in
-- committed ideas (type='latent') alongside obligations (dashboard-svc
-- code change, not a migration) — dashboard-svc's role never had SELECT
-- on latents at all (0011_dashboard_grants.sql granted items/
-- conversations/messages/obligations/suggestions, since nothing before
-- this needed to read latents directly), so every /me/items call started
-- 500ing with "permission denied for table latents" the instant the
-- query touched it.
GRANT SELECT ON latents TO "sa-dashboard@obligation-engine-hack.iam";
