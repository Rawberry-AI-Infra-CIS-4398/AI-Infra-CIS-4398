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
| `DEVELOPER_UPLOAD_API_KEY` | Secret required for shared corpus uploads. Developer uploads are disabled if unset. |
| `USE_MOCK_CHAT` | Defaults to `true`. Set to `false` for real Gemini answers; a missing Google Cloud project then prevents startup instead of falling back to mock chat. |
| `GOOGLE_CLOUD_PROJECT` | Google Cloud project for Vertex AI. |
| `GOOGLE_CLOUD_LOCATION` | Vertex AI location; defaults to `global`. |
| `GEMINI_MODEL` | Chat model; defaults to `gemini-2.5-flash`. |

PostgreSQL mode requires the supplied database schema and Vertex AI access for
embeddings, even when chat responses are mocked. The API does not apply migrations.
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
| `POST /uploadmedical` | Upload source documents, including source JSON files, to the shared medical RAG corpus. |
| `POST /uploadedu` | Upload source documents, including education JSON files, to the shared education RAG corpus. |
| `POST /upload` | Upload personal PDF, DOCX, or TXT files; requires `X-User-ID` and multipart `domain` (`medical` or `education`). |
| `GET /upload` | List the current user's document metadata; requires `X-User-ID` and query parameter `domain` (`medical` or `education`). |
| `GET /uploadmedical` | List metadata for medical source documents. |
| `GET /uploadedu` | List metadata for education source documents. |
| `GET /me/system-prompt` | Read the current user's response preferences. |
| `PUT /me/system-prompt` | Set or clear the current user's response preferences. |
| `POST /conversations` | Create a conversation with a fixed medical or education domain. |
| `GET /conversations` | List the current user's non-expired conversations. |
| `DELETE /conversations/{conversation_id}` | Permanently delete an owned conversation, its messages, and sharing access. |
| `GET /conversations/{conversation_id}/messages` | Read the owner's saved conversation messages. |
| `POST /chat` | Stream progress updates and Gemini answer chunks over SSE, then save the completed exchange. |
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
Use the returned `id` in `POST /chat` with the same user header:

```json
{
  "conversation_id": "550e8400-e29b-41d4-a716-446655440000",
  "message": "What do the education sources say?"
}
```

`/chat` always returns `text/event-stream` (SSE). It sends `status` events:
`received`, `generating`, then `completed` or `failed`. Between those statuses,
`delta` events carry answer text as Gemini generates it. Append each `delta` to
the displayed answer; chunk boundaries may split words and do not necessarily
match individual tokens.

```text
event: delta
data: {"request_id":"...","conversation_id":"...","turn_id":"...","delta":"Hello"}

event: delta
data: {"request_id":"...","conversation_id":"...","turn_id":"...","delta":" world"}

```

The final `completed` status includes the full saved answer in `response.reply`;
use it to confirm or replace the assembled text, rather than appending it again.
On `failed`, any displayed partial text is incomplete and is not saved as an
assistant reply. A stream ending without either final status is interrupted;
check history before retrying. The endpoint requires a POST-capable streaming
client with `X-User-ID`; the browser's native `EventSource` cannot make that POST.
Existing `/chat/status` and `/chat/stream` clients must switch to `/chat`; those routes have been removed.

Real streaming requires `USE_MOCK_CHAT=false` and a configured Google Cloud project.
Mock mode emits chunks of its local echo response without calling Gemini.

The endpoint saves the question before generation and the answer on success.
Failed questions remain in history with `status: "failed"`. History includes
ordered messages with role, content, turn ID, status, and timestamp.

- `GET /conversations` lists the user's conversations, including titles and descriptions.
- `GET /conversations/{conversation_id}/messages` returns an owned conversation's messages.
- `DELETE /conversations/{conversation_id}` requires the owner's `X-User-ID` header and returns **204 with no body**. Missing, expired, already deleted, or another user's conversations return 404. No JSON body is required.
- Deletion removes saved turns, messages/citations, and sharing access; uploaded documents and response preferences remain. A pending generation cannot restore a deleted conversation.
- The first successful exchange supplies a short description; the title remains caller-supplied.
- Conversations expire after **24 hours of inactivity**. Reading history does not extend this period.
- Only one generation may run per conversation. Concurrent requests return 409; abandoned pending turns can recover after five minutes.
- Requests are not idempotent. Check history before retrying after a network failure; disconnecting does not guarantee cancellation.

Each conversation uses its own history and retrieves sources from its selected
shared corpus. Set response preferences using `PUT /me/system-prompt` with
`{"prompt":"Keep answers concise."}`; an empty prompt clears them. Preferences
apply to subsequent turns across the user's conversations.

## Document uploads

Users upload personal files through `POST /upload` with `X-User-ID`, a required
multipart `domain` field (`medical` or `education`), and one or more `files`.
List those files with `GET /upload?domain=education` (or `medical`) using the
same user header. Both methods use the same path; no `/documents` endpoint is exposed.
Personal uploads accept PDF, DOCX, and TXT. Chat searches the conversation's
shared corpus and that user's personal uploads in the same domain, then uses
the top three matches overall. Other users' uploads and other domains are excluded.
Existing personal documents without a domain tag are excluded from these domain-filtered
lists and searches; re-upload them with the appropriate domain. No schema migration is required.

Developers upload sources through `POST /uploadmedical` or `POST /uploadedu` using
`X-Developer-Key`. Send multipart form data under `files`, repeating that field for
multiple files. Let your HTTP client set the multipart boundary.

```powershell
curl.exe -X POST "http://127.0.0.1:8000/uploadedu" -H "X-Developer-Key: $env:DEVELOPER_UPLOAD_API_KEY" -F "files=@example.txt"
```

Supported files: **PDF, DOCX, and UTF-8 TXT**, up to **25 MiB per file**. Readable
text is extracted, cleaned, and chunked for retrieval. OCR is not supported.
Unreadable files or files with no readable text are rejected.

`/uploadedu` and `/uploadmedical` also accept **UTF-8 `.json` files**, with the same developer key,
multipart `files` field, and 25 MiB limit. Each file contains one source object:

```json
{
  "authors": ["Henretta, James A."],
  "institution": "",
  "text": ["First paragraph or fragment.", "Next paragraph or fragment."],
  "citation": "Full bibliographic citation",
  "title": "America's History, For the AP* Course",
  "header": "The Perils of War",
  "date_published": "2014-01-10T05:00:00.000Z",
  "date_added": "2026-09-13T23:50:21.443Z",
  "start_page": 188
}
```

Only `text` is required: a nonempty array of nonblank strings. Entries are
paragraphs/fragments, joined in order with paragraph breaks before cleaning,
chunking, and embedding. Metadata is not embedded as source text.

Other supported fields are optional: `authors` (string array), `institution`
(string or null), `citation`, `title`, `header`, and `source_id` (strings),
`date_published` and `date_added` (ISO 8601 timestamps with time zones, or null),
and `start_page` (positive integer or null). Unknown fields, arrays of source
objects, malformed JSON, and invalid field types return 400 `INVALID_EDU_JSON`
for education or `INVALID_MEDICAL_JSON` for medical.

Source fields are stored under each document's `metadata.education` or
`metadata.medical`, visible through GET on the corresponding upload path.
`date_added` remains a source-supplied date. An empty
institution becomes null. `start_page` is a source reference; chunk page numbers
remain null because fragments do not identify page boundaries. Saving citation
metadata does not automatically add citations to chat answers. Re-uploading adds
a new document, even if its optional `source_id` matches an existing document.

```powershell
curl.exe -X POST "http://127.0.0.1:8000/uploadedu" -H "X-Developer-Key: $env:DEVELOPER_UPLOAD_API_KEY" -F "files=@rawberry-api/examples/education-source.json;type=application/json"
```

Run this example from the repository root. JSON can be mixed with other supported
file types in a batch; one invalid file rejects the batch. Both developer upload
paths accept PDF, DOCX, TXT, and the same source JSON structure. Personal `/upload`
continues to accept PDF, DOCX, and TXT only.

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
| 503 | Storage/embeddings unavailable, uploads not configured, or database health check failed. |

After an SSE stream starts, generation failures arrive as a `failed` event rather
than a new HTTP error status. Internal database and provider details are not exposed.
