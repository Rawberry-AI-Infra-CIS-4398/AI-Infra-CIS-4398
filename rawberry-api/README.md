# RAWBerry API

FastAPI backend for medical and education document retrieval, Gemini chat,
conversation history, and read-only conversation sharing.

## Run locally

Requires Python 3.12+. From the repository root:

```powershell
.\run.ps1 -InstallDependencies
```

For later runs, use `.\run.ps1`. Alternatively, from `rawberry-api`:

```powershell
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000/docs> for request schemas and interactive testing.
See [run.md](run.md) for Google Cloud setup and [the database README](../rawberry-db/README.MD)
for PostgreSQL schema setup.

## Configuration

Set environment variables before starting the API; `.env` files are not loaded automatically.

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | PostgreSQL connection string. Without it, storage is in memory and disappears on restart; separate processes do not share data. |
| `DEVELOPER_UPLOAD_API_KEY` | Secret required for document uploads. Uploads are disabled if unset. |
| `USE_MOCK_CHAT` | Defaults to `true`. Set to `false` for real Gemini answers. |
| `GOOGLE_CLOUD_PROJECT` | Google Cloud project for Vertex AI. |
| `GOOGLE_CLOUD_LOCATION` | Vertex AI location; defaults to `global`. |
| `GEMINI_MODEL` | Chat model; defaults to `gemini-2.5-flash`. |

PostgreSQL mode requires the supplied database schema and Vertex AI access for
embeddings, even when chat responses are mocked. Apply the baseline schema and all numbered migrations before startup; the API does not apply migrations.
Local development uses Application Default Credentials; Cloud Run uses its service account.

## Authentication boundary and user context

Authentication and account management belong to the upstream application.

- User data routes require `X-User-ID`, supplied by the authenticated upstream backend or gateway.
- IDs are case-sensitive, 1-128 printable ASCII characters without spaces. The reserved IDs `corpus:medical` and `corpus:education` cannot identify users.
- `X-User-ID` is not authentication. The upstream service must strip client-supplied identity headers, supply the authenticated user's ID, and restrict direct API access.
- Upload POST routes require `X-Developer-Key` matching `DEVELOPER_UPLOAD_API_KEY`; they do not require `X-User-ID`. Keep the key out of frontend code and Git.
- `/`, `/health`, `/health/db`, and documentation routes require neither header. Cloud Run IAM access rules still apply.

In Swagger, enter the appropriate header under **Try it out**. JSON request bodies
reject extra fields; identity belongs in the header, not the body.

## Routes

| Route | Purpose |
| --- | --- |
| `GET /` | Return the API status message and endpoint list. |
| `GET /health` | Check that the API process is running. |
| `GET /health/db` | Check connectivity to the configured PostgreSQL database. |
| `POST /uploadmedical` | Upload source documents to the shared medical RAG corpus. |
| `POST /uploadedu` | Upload source documents to the shared education RAG corpus. |
| `GET /uploadmedical` | List metadata for medical source documents. |
| `GET /uploadedu` | List metadata for education source documents. |
| `GET /me/system-prompt` | Read the current user's response preferences. |
| `PUT /me/system-prompt` | Set or clear the current user's response preferences. |
| `POST /conversations` | Create a conversation with a fixed medical or education domain. |
| `GET /conversations` | List the current user's non-expired conversations. |
| `GET /conversations/{conversation_id}/messages` | Read the owner's saved conversation messages. |
| `POST /chat` | Generate an answer and save the exchange, returning the full answer when finished. |
| `POST /chat/stream` | Generate and save the same exchange while sending progress events, followed by the full answer; does not stream individual words or tokens. |
| `PUT /conversations/{conversation_id}/shares` | Replace the users granted read-only access to an owned conversation. |
| `GET /conversations/{conversation_id}/shares` | List users granted access to an owned conversation. |
| `GET /shared` | List conversations shared with the current user. |
| `GET /shared/{conversation_id}` | Read a conversation and its messages shared with the current user. |
| `GET /docs` | Open the interactive Swagger API documentation. |
| `GET /redoc` | Open the ReDoc API reference. |
| `GET /openapi.json` | Return the machine-readable OpenAPI specification. |

## Chat and history

Create a conversation with `POST /conversations` and `X-User-ID`:

```json
{"title": "Research questions", "domain": "education"}
```

`domain` is required (`medical` or `education`) and stays fixed. The title is optional.
Use the returned `id` in `POST /chat` or `POST /chat/stream` with the same user header:

```json
{
  "conversation_id": "550e8400-e29b-41d4-a716-446655440000",
  "message": "What do the education sources say?"
}
```

`/chat` returns `conversation_id`, `turn_id`, `reply`, `retrieval_match_percent`, and `sources` in one JSON response.

`retrieval_match_percent` is the highest current-question passage similarity multiplied
by 100, rounded to the nearest whole percent (ties to even). It describes retrieval
relevance, not model confidence, factual accuracy, or verified answer support. No
retrieved passages means `null` and an empty `sources` list; answers can still be generated.
In-memory hash retrieval and PostgreSQL model embeddings can produce different scores.

`sources` contains every retrieved passage supplied to the model (up to the existing
three-passage limit), in retrieval order. `filename` is exactly the uploaded filename,
read from storage. The model receives these source labels beside the passages and is
instructed to use them when citing, rather than production filenames or page labels
embedded in document text. Structured `sources` remains the authoritative citation
list; generated citation text can still contain model errors.
Each entry includes `document_id`, `filename`,
`page_number` (null for TXT/DOCX), zero-based `chunk_index`, and `excerpt` (the supplied
chunk text), `source_start_line`, and `source_end_line` (inclusive, starting at 1).
PDF line numbers refer to extracted text and restart on each page; TXT/DOCX line
numbers span the extracted document. They may differ from visual wrapping or printed
line labels in the PDF. Blank lines count toward the original extracted locations,
even when cleaning collapses them. These ranges cover the supplied passage, rather
than identifying exactly which lines the model relied on. Line ranges are null for
older documents without a saved extraction map; re-uploading a document creates one.
Multiple passages can reference the same document. This is a context
list, not a list of verified supporting claims. Earlier conversation context is not
included in the current score or source list.

```json
{
  "conversation_id": "550e8400-e29b-41d4-a716-446655440000",
  "turn_id": "550e8400-e29b-41d4-a716-446655440001",
  "reply": "The registry code is PINE-724.",
  "retrieval_match_percent": 83,
  "sources": [{
    "document_id": "550e8400-e29b-41d4-a716-446655440002",
    "filename": "registry.pdf",
    "page_number": 2,
    "chunk_index": 1,
    "excerpt": "The registry code is PINE-724.",
    "source_start_line": 12,
    "source_end_line": 12
  }]
}
```

The same fields appear in the stream's completed `response` and are saved with the
answer for history and shared conversation reads. User messages and older assistant
messages have `retrieval_match_percent: null` and `sources: []`. Saved source excerpts
are snapshots of the supplied passages; history reads do not rerun retrieval.
`/chat/stream` returns SSE `status` events: `received`, `generating`, then
`completed` or `failed`. The completed event contains the answer in `response.reply`.
It reports progress, not individual words or tokens. There is no `/chat/status` route.
Clients must inspect the stream's final event to determine success.

Both endpoints save the question before generation and the answer on success.
Failed questions remain in history with `status: "failed"`. History includes
ordered messages with role, content, turn ID, status, and timestamp.

- `GET /conversations` lists the user's conversations, including titles and descriptions.
- `GET /conversations/{conversation_id}/messages` returns an owned conversation's messages.
- The first successful exchange supplies a short description; the title remains caller-supplied.
- Conversations expire after **24 hours of inactivity**. Reading history does not extend this period.
- Only one generation may run per conversation. Concurrent requests return 409; abandoned pending turns can recover after five minutes.
- Requests are not idempotent. Check history before retrying after a network failure; disconnecting does not guarantee cancellation.

Each conversation uses its own history and retrieves sources from its selected
shared corpus. Set response preferences using `PUT /me/system-prompt` with
`{"prompt":"Keep answers concise."}`; an empty prompt clears them. Preferences
apply to subsequent turns across the user's conversations.

## Document uploads

Developers upload sources through `POST /uploadmedical` or `POST /uploadedu` using
`X-Developer-Key`. Send multipart form data under `files`, repeating that field for
multiple files. Let your HTTP client set the multipart boundary.

```powershell
curl.exe -X POST "http://127.0.0.1:8000/uploadedu" -H "X-Developer-Key: $env:DEVELOPER_UPLOAD_API_KEY" -F "files=@example.txt"
```

Supported files: **PDF, DOCX, and UTF-8 TXT**, up to **25 MiB per file**. Readable
text is extracted, cleaned, and chunked for retrieval. OCR is not supported.
Unreadable files or files with no readable text are rejected.

Upload responses include document IDs, chunk counts, and `embedding_status`.
PostgreSQL mode reports `completed` after indexing succeeds; in-memory development
uses temporary hash-based retrieval and reports `pending`. Processing is synchronous.

Sources are shared within the medical or education corpus, independently of user
identity. Uploading adds documents; there is no replacement or deletion route.
GET on either upload path requires `X-User-ID` and returns document metadata,
not file contents. Chat messages are history, not RAG source documents.

## Shared conversations

Owners grant read-only access to specific user IDs with
`PUT /conversations/{conversation_id}/shares`:

```json
{"user_ids": ["bob", "carol"]}
```

This replaces the entire recipient list. Omitted users lose access;
`{"user_ids":[]}` revokes everyone. A request accepts at most 50 recipient entries
and cannot include the owner.

Recipients use their own `X-User-ID` with `GET /shared` and
`GET /shared/{conversation_id}`. The detail response contains `conversation` and
`messages`. Recipients cannot send messages or manage sharing on the owner's chat.
Public share links are not supported, and access ends when the conversation expires.

Shared views receive no push updates. Poll the shared detail route every 15 seconds
and refresh when `conversation.updated_at` changes.

## Health checks

`GET /health` checks process liveness. `GET /health/db` opens a fresh PostgreSQL
connection and runs `SELECT 1`, returning:

| HTTP status | Response |
| --- | --- |
| 200 | `{"status":"healthy","database":"connected"}` |
| 503 | `{"status":"unhealthy","database":"not_configured"}` in memory mode |
| 503 | `{"status":"unhealthy","database":"unavailable"}` on connection or query failure |

The database probe uses a five-second connection timeout and a three-second
statement timeout. It does not verify Vertex AI or every application table.
If the startup database/schema check fails, the API cannot serve requests;
inspect startup logs instead.

## Errors

| Status | Meaning |
| --- | --- |
| 400 | Invalid, unreadable, empty, or unsupported file. |
| 403 | Missing or invalid developer upload key. |
| 404 | Missing, unauthorized, revoked, or expired resource. |
| 409 | Conversation already generating or a superseded turn. |
| 413 | File exceeds 25 MiB. |
| 422 | Missing/invalid user header or invalid request payload. |
| 502 | Answer generation failed. |
| 503 | Storage/embeddings unavailable, uploads not configured, or database health check failed. |

After an SSE stream starts, generation failures arrive as a `failed` event rather
than a new HTTP error status. Internal database and provider details are not exposed.
