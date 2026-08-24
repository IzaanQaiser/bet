-- Phase G follow-up (conversation-continuity fix, same session as step D):
-- resolver-svc's relates_to_item escape hatch (agent-contracts.md §3.5)
-- spins up a brand-new RECEIVED items row itself, via the shared
-- create_raw_item helper, when a reply arrives while an item is open but
-- doesn't actually relate to it — exactly the row shape ingest-svc's own
-- fresh-message path writes. Before this, sa-resolver only ever needed
-- SELECT/UPDATE on items (migration 0001) since it never originated a row,
-- only mutated ones ingest-svc had already created. Found as a real bug in
-- live testing, not anticipated up front: the first live unrelated-reply
-- test 500'd with "permission denied for table items" the moment
-- create_raw_item's INSERT actually ran against the deployed DB user.

GRANT INSERT ON items TO "sa-resolver@obligation-engine-hack.iam";
