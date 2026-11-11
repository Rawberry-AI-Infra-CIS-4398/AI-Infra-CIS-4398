# Validation and testing

The normal CI suite runs the API and retrieval tests with mocked chat generation
and a disposable pgvector PostgreSQL service. PostgreSQL integration tests create
and drop isolated schemas from `rawberry-db/init/schema.sql`; never point
`TEST_DATABASE_URL` at a shared database.

## Backend and database suites

Install the API requirements, start Docker Desktop, and run from the repository
root with the Python environment activated:

```bash
docker run --detach --rm --name rawberry-validation-pg16 \
  --publish 127.0.0.1:55432:5432 \
  --env POSTGRES_DB=rawberry_test --env POSTGRES_USER=postgres \
  --env POSTGRES_PASSWORD=contract-local-only pgvector/pgvector:pg16
docker exec rawberry-validation-pg16 pg_isready -U postgres -d rawberry_test
```

Once PostgreSQL accepts connections, run the suite and stop the container after it finishes:

```bash
TEST_DATABASE_URL=postgresql://postgres:contract-local-only@127.0.0.1:55432/rawberry_test \
  USE_MOCK_CHAT=true python -B -m pytest tests rawberry-api/tests -p no:cacheprovider
docker stop rawberry-validation-pg16
```

Expect zero PostgreSQL skips. For PowerShell setup, see the
[database contract guide](database-contract-verification.md).

The tests cover endpoint contracts, developer-only upload authorization, document
cleaning and chunking, persistence, retrieval, conversation and preference
isolation, and generation/storage recovery. Model and embedding clients are stubbed
or deterministic; the suite makes no paid model calls.

## Local QuagBot and API end-to-end tests

This test checks the real QuagBot UI and server routes against a local AI Infra API.
It uses mock chat and the API's in-memory store, so it does not need PostgreSQL or
Google credentials. QuagBot still needs its normal Supabase URL/key configuration
and a dedicated test account; sign-in itself is not asserted.

1. Start the API from `rawberry-api` with the Python environment activated:

   ```bash
   env -u DATABASE_URL USE_MOCK_CHAT=true DEVELOPER_UPLOAD_API_KEY=test-upload-key \
     python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
   ```

2. In the QuagBot directory containing `package.json`, use Node 24 and configure
   `.env.local` with your test account's Supabase URL and publishable key. See
   QuagBot's `.env.example` for the variable names. Install and start QuagBot:

   ```bash
   npm ci
   AI_API_URL=http://127.0.0.1:8000 AI_MAINTENANCE_MODE=false \
     npm run dev -- --hostname 127.0.0.1 --port 3000
   ```

   If `npm ci` reports missing `@emnapi/runtime` lockfile entries, run these
   commands from the same QuagBot directory to install in a temporary copy:

   ```bash
   quagbot_test_dir=$(mktemp -d)
   git -C .. archive HEAD QuagBot | tar -x -C "$quagbot_test_dir"
   cp .env.local "$quagbot_test_dir/QuagBot/.env.local"
   cd "$quagbot_test_dir/QuagBot"
   npm install --no-audit --no-fund
   AI_API_URL=http://127.0.0.1:8000 AI_MAINTENANCE_MODE=false \
     npm run dev -- --hostname 127.0.0.1 --port 3000
   ```

3. In this repository, install the browser test package and Chromium:

   ```bash
   cd tests/e2e
   npm ci
   npx playwright install chromium
   (umask 077; mkdir -p .auth; npx playwright open --save-storage=.auth/quagbot.json http://127.0.0.1:3000/login)
   ```

   Sign into QuagBot with the dedicated test account in the opened browser, then
   close it. The saved browser state is ignored by Git.
4. Run the QuagBot browser and HTTP tests from `tests/e2e`:

   ```bash
   npm test -- specs/quagbot-chat.spec.js specs/rawberry-demo.spec.js
   ```

Expect one Chromium test and one HTTP test to pass. The HTTP test can run with
only the local API using `npm test -- specs/rawberry-demo.spec.js`. Stop the two
servers with Ctrl+C when finished.

The browser is visible by default. Screenshots of the follow-up, reopened chat,
and document answer are saved under `tests/e2e/.artifacts/`. Set
`PLAYWRIGHT_HEADLESS=true` to run without showing the browser.

## Demo walkthrough

Use the [Cloud Run Swagger page](https://runberry-694004167309.us-east4.run.app/docs)
for the deployed demo. Before presenting, run this read-only smoke check from the
repository root after installing the Playwright dependencies and Chromium:

```bash
node tests/e2e/check-cloud-run.mjs https://runberry-694004167309.us-east4.run.app/docs
```

The check opens Swagger in Chromium, executes `/health`, reads the default prompt
and empty history for a synthetic user, and compares the deployed OpenAPI schema
with the demo routes. It exits with status 1 and lists any missing operations.
On November 11, 2026, the shared Cloud Run URL passed the read checks and its
schema lacked all four sharing operations. Rehearse sharing locally until the
deployed schema lists the recipient sharing routes.

### Rehearse Swagger with synthetic data

Start the disposable PostgreSQL container from the backend setup above. Apply the
schema and start the local API with deterministic chat and embeddings:

```bash
docker exec -i rawberry-validation-pg16 psql -U postgres -d rawberry_test \
  -v ON_ERROR_STOP=1 < rawberry-db/init/schema.sql
env -u DATABASE_URL PYTHONPATH=rawberry-api DEVELOPER_UPLOAD_API_KEY=test-upload-key \
  TEST_DATABASE_URL=postgresql://postgres:contract-local-only@127.0.0.1:55432/rawberry_test \
  python -B tests/e2e/serve_demo.py
```

Open [local Swagger](http://127.0.0.1:8000/docs), or run the Chromium rehearsal
from `tests/e2e` with the same Python environment activated:

```bash
PLAYWRIGHT_HEADLESS=true npm test -- specs/rawberry-swagger.spec.js specs/rawberry-demo.spec.js
```

Expect two Swagger browser tests and one HTTP test. Stop the server with Ctrl+C
and remove the disposable database with `docker stop rawberry-validation-pg16`.

### Feature steps

| Feature | What to show |
| --- | --- |
| System prompts | In RAWBerry `/docs`, set and read a prompt with the same `X-User-ID`. Read it with another ID to see the empty default. Integration tests verify that the model receives the prompt separately as `system_instruction`. |
| Conversation history | Create a conversation with `POST /conversations`. Use its ID for two `POST /chat` calls and open `GET /conversations/{id}/messages` with the same user ID to see all four messages. |
| Conversation context | Ask a follow-up that refers to the first question. In the local rehearsal, the mock reply shows the earlier turn. PostgreSQL tests exercise the 10 completed turn cap and 12,000-character history budget while retaining the full transcript. |
| Sharing | In RAWBerry `/docs`, use the owner's ID to `PUT /conversations/{id}/shares` with `{"user_ids":["demo-recipient"]}`. Read `GET /shared/{id}` as the recipient. Clear the list and repeat the recipient read to show the `404`. The API supports recipient permissions. Shareable URLs have no route in this build. |
| Document processing | Use the developer key to upload synthetic PDF, DOCX, and TXT files to the medical and education corpora. Check the chunk count and ask a question in the matching conversation domain. The Swagger rehearsal verifies both upload routes and corpus isolation. |
