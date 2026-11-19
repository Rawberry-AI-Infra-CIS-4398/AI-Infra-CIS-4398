# RAWBerry database

The database is created from `init/schema.sql`. It enables pgvector and creates
every table, index, constraint, and seed row the API needs: documents and original
file bytes, chunks, 768-dimensional embeddings (`gemini-embedding-001`), user
preferences, conversations, and conversation turns.

`schema.sql` is safe to re-run: existing tables, indexes, and seed rows are left as
they are. It contains no `DROP` statements and does not upgrade tables from an older
schema; it runs in one transaction, so a failure leaves nothing behind. The
application never creates or changes the schema.

## Create the database

Install pgvector, set `DATABASE_URL`, and run from the repository root:

```powershell
psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 -f rawberry-db/init/schema.sql
psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 -f rawberry-db/migrations/002_retrieval_metadata.sql
```

Alternatively, `python init_db.py` runs `schema.sql` against the Cloud SQL instance
configured in `.env`; then apply the numbered migration above.

## Schema version

`schema.sql` records `baseline_001` in the `schema_migration` table. In PostgreSQL
mode, the API requires both `baseline_001` and `retrieval_metadata_002` at startup.
Apply `migrations/002_retrieval_metadata.sql` to existing and fresh databases before
deploying the updated API. It adds a nullable percentage and a JSONB source snapshot
to each chat turn. Existing rows retain their answers and receive null/empty metadata.
The migration is transactional and safe to rerun. For rollback, roll back the API
while retaining these additive columns; no data removal is needed.
Future schema changes go in a new numbered file (for example
`migrations/003_<change>.sql`) that records its own version, and `check_ready()`
is updated to require it.

## Connecting the API

The API uses PostgreSQL only when `DATABASE_URL` is set in its environment. Without
it, the API uses an in-memory store, so data is lost on restart and not shared
between developers. The API does not load `.env` itself.

## Corpora and ownership

`user_id` columns hold the opaque upstream ID from `X-User-ID`. Medical and education
documents are owned by the reserved IDs `corpus:medical` and `corpus:education` and
are uploaded through the developer-only `/uploadmedical` and `/uploadedu` endpoints.
Each conversation has a fixed `domain` (`medical` or `education`) that selects its
corpus.

## Conversation retention

Conversations expire after 24 hours of inactivity. The API hides expired
conversations immediately; physical deletion requires a scheduled
`python -m app.cleanup_conversations` run from `rawberry-api`. Cleanup never deletes
documents or preferences. See the API README for batching and deployment details.

## Tests

To run the database integration tests, set `TEST_DATABASE_URL` to a disposable
pgvector database and run `python -m pytest tests rawberry-api/tests` from the
repository root. Each test builds the schema from `init/schema.sql` and applies numbered migrations in an
isolated PostgreSQL schema, then drops it, so the
test user needs extension and schema privileges. Without that setting, database
integration tests are skipped. Tests use deterministic 768-dimensional fake
embeddings and never call Vertex AI. Do not use the shared Cloud SQL instance as
`TEST_DATABASE_URL`.

See the [API README](../rawberry-api/README.md) for current behavior and limitations.