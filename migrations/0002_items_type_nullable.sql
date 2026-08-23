-- items.type was NOT NULL, which was wrong: ingest-svc writes the RECEIVED
-- row before extraction happens, and type (obligation vs latent) is exactly
-- what extractor-svc determines. See docs/architecture/data-model.md §2.6.

ALTER TABLE items ALTER COLUMN type DROP NOT NULL;
