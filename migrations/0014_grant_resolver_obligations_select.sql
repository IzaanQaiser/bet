-- resolver-svc gets read access to a user's other committed obligations
-- so the conversational turn can reason about what else is on their
-- plate (cross-item situational awareness) — never granted before now,
-- since resolver-svc never queried obligations at all until this change.

GRANT SELECT ON obligations TO "sa-resolver@obligation-engine-hack.iam";
