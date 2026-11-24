BEGIN;

-- Old turns have no recorded retrieval evidence.
ALTER TABLE chat_turn
    ADD COLUMN IF NOT EXISTS retrieval_match_percent INTEGER
        CHECK (retrieval_match_percent BETWEEN 0 AND 100),
    ADD COLUMN IF NOT EXISTS sources JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(sources) = 'array');

INSERT INTO schema_migration (version)
VALUES ('retrieval_metadata_002')
ON CONFLICT DO NOTHING;

COMMIT;