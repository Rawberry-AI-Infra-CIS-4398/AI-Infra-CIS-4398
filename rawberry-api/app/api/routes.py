import anyio
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import HTTPException

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import iterate_in_threadpool, run_in_threadpool

from app.corpora import corpus_owner, require_developer
from app.models import HealthResponse, RootResponse, UploadErrorResponse, UploadResponse
from app.repository import Repository, StorageError
from app.services.chat_service import ChatService
from app.services.document_service import DocumentService
from app.user_context import require_user
from app.user_models import (ChatDeltaEvent, ChatDomain, ChatRequest, ChatResponse, ChatStatusEvent, ConversationRecord,
                             ConversationRequest, DocumentRecord, MessageRecord, ShareRequest, ShareResponse, SharedConversation,
                             SystemPromptRequest, SystemPromptResponse)

router = APIRouter()
User = Annotated[str, Depends(require_user)]


class _ChatStreamingResponse(StreamingResponse):
    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Ensure generator cleanup runs even if sending to a disconnected
            # client fails while the generator is suspended at a yield.
            with anyio.CancelScope(shield=True):
                await self.body_iterator.aclose()


def get_store(request: Request) -> Repository:
    return request.app.state.store


def get_chat_service(store: Repository = Depends(get_store)) -> ChatService:
    return ChatService(store)


def get_document_service(store: Repository = Depends(get_store)) -> DocumentService:
    return DocumentService(store)


@router.get('/', response_model=RootResponse)
def read_root() -> RootResponse:
    return RootResponse(message='RAWBerry API is running', available_endpoints=[
        '/health', '/health/db', '/upload', '/uploadmedical', '/uploadedu', '/chat',
        '/me/system-prompt', '/conversations', '/conversations/{conversation_id}', '/conversations/{conversation_id}/messages',
        '/conversations/{conversation_id}/shares', '/shared', '/shared/{conversation_id}',
    ])


@router.get('/health', response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status='healthy')


@router.get('/health/db', responses={503: {'description': 'Database unavailable or not configured'}})
def database_health(store: Repository = Depends(get_store)) -> JSONResponse:
    """Check a fresh PostgreSQL connection; memory mode is not database health."""
    from app.postgres_repository import PostgresRepository

    headers = {'Cache-Control': 'no-store'}
    if not isinstance(store, PostgresRepository):
        return JSONResponse(status_code=503, headers=headers,
                            content={'status': 'unhealthy', 'database': 'not_configured'})
    try:
        store.check_database_connection()
    except StorageError:
        return JSONResponse(status_code=503, headers=headers,
                            content={'status': 'unhealthy', 'database': 'unavailable'})
    return JSONResponse(headers=headers, content={'status': 'healthy', 'database': 'connected'})


def register_corpus_routes(path: str, domain: str) -> None:
    @router.get(path, response_model=list[DocumentRecord])
    def get_documents(user_id: User, store: Repository = Depends(get_store)):
        return store.list_documents(corpus_owner(domain))

    description = 'Developer-only multipart upload using the files field. Supports PDF, DOCX, and UTF-8 TXT.'
    description += (
        ' Also accepts UTF-8 .json source objects with a nonempty text array and optional '
        'authors, institution, citation, title, header, date_published, date_added, '
        'start_page, and source_id.'
    )
    if domain == 'medical':
        description += (
            ' Medical JSON may instead contain body sections with heading, paragraphs, '
            'and nested subsections, an optional abstract, and author objects with names.'
        )
    description += ' Source metadata is preserved separately from searchable text.'


    @router.post(path, response_model=list[UploadResponse], status_code=201, description=description,
                 dependencies=[Depends(require_developer)], responses={
                     400: {'model': UploadErrorResponse}, 413: {'model': UploadErrorResponse},
                 })
    def upload_document(
        files: Annotated[list[UploadFile], File(..., json_schema_extra={'items': {'type': 'string', 'format': 'binary'}})],
        service: DocumentService = Depends(get_document_service),
    ):
        return service.upload_document(corpus_owner(domain), files)


register_corpus_routes('/uploadmedical', 'medical')
register_corpus_routes('/uploadedu', 'education')


# User uploads go through the same ingestion as the corpora, scoped to X-User-ID and the
# frontend's domain; chats only search the user's uploads from their own domain.
@router.get('/upload', response_model=list[DocumentRecord])
def get_user_documents(domain: ChatDomain, user_id: User, store: Repository = Depends(get_store)):
    return store.list_documents(user_id, domain)


@router.post('/upload', response_model=list[UploadResponse], status_code=201, responses={
    400: {'model': UploadErrorResponse}, 413: {'model': UploadErrorResponse},
})
def upload_user_document(
    user_id: User,
    domain: Annotated[ChatDomain, Form()],
    files: Annotated[list[UploadFile], File(..., json_schema_extra={'items': {'type': 'string', 'format': 'binary'}})],
    service: DocumentService = Depends(get_document_service),
):
    """Upload one or more personal files using repeated multipart files fields.

    Set X-User-ID and select medical or education as domain. Each Swagger
    'Add string item' slot is a file picker, not a pasted-text input. Select a
    file in every added slot. All selected files are validated before any are stored.
    """
    return service.upload_document(user_id, files, domain)


@router.get('/me/system-prompt', response_model=SystemPromptResponse)
def get_system_prompt(user_id: User, store: Repository = Depends(get_store)):
    return SystemPromptResponse(user_id=user_id, prompt=store.get_system_prompt(user_id))


@router.put('/me/system-prompt', response_model=SystemPromptResponse)
def set_system_prompt(request: SystemPromptRequest, user_id: User, store: Repository = Depends(get_store)):
    prompt = request.prompt.strip()
    store.set_system_prompt(user_id, prompt)
    return SystemPromptResponse(user_id=user_id, prompt=prompt)


@router.post('/conversations', response_model=ConversationRecord, status_code=201)
def create_conversation(request: ConversationRequest, user_id: User, store: Repository = Depends(get_store)):
    return store.create_conversation(user_id, request.title, request.domain)


@router.get('/conversations', response_model=list[ConversationRecord])
def list_conversations(user_id: User, store: Repository = Depends(get_store)):
    return store.list_conversations(user_id)


@router.delete('/conversations/{conversation_id}', status_code=204, response_class=Response,
               responses={404: {'description': 'Conversation missing, expired, or not owned by this user'}})
def delete_conversation(conversation_id: UUID, user_id: User, store: Repository = Depends(get_store)):
    """Permanently delete an owned conversation, its messages, and sharing access.

    Supply the owner's X-User-ID header. Uploaded documents and user preferences
    are preserved. Deletion is allowed during generation; late results cannot
    restore the conversation. Success returns 204 with no response body.
    """
    store.delete_conversation(user_id, str(conversation_id))
    return Response(status_code=204)


@router.get('/conversations/{conversation_id}/messages', response_model=list[MessageRecord])
def list_messages(conversation_id: UUID, user_id: User, store: Repository = Depends(get_store)):
    return store.list_messages(user_id, str(conversation_id))


@router.post('/chat', response_class=StreamingResponse,
             responses={200: {'content': {'text/event-stream': {}}}})
def chat(request: ChatRequest, user_id: User, service: ChatService = Depends(get_chat_service)):
    """Stream progress status and incremental Gemini answer text over SSE.

    Append each delta event to the displayed answer. The completed status contains
    the full saved reply; failed marks an incomplete answer. Requires an owned
    conversation and X-User-ID. The response is always text/event-stream.
    """
    turn = service.prepare_turn(user_id, request)
    request_id = str(uuid4())

    async def events():
        # Run each blocking provider step in a worker and explicitly close the
        # iterator on disconnect, including when no model chunk was requested.
        stream = service.stream_reply(user_id, request, turn)
        started = False
        completed = False
        parts = []
        try:
            for status, message in [('received', 'Your message was received.'), ('generating', 'Generating a response.')]:
                yield _format_status_event(ChatStatusEvent(request_id=request_id, status=status, message=message))
            try:
                started = True
                async for delta in iterate_in_threadpool(stream):
                    parts.append(delta)
                    event = ChatDeltaEvent(request_id=request_id, conversation_id=str(request.conversation_id),
                                           turn_id=turn['id'], delta=delta)
                    yield f'event: delta\ndata: {event.model_dump_json()}\n\n'
            except Exception:
                yield _format_status_event(ChatStatusEvent(request_id=request_id, status='failed',
                                                           message='The response could not be generated.'))
                return
            completed = True
            response = ChatResponse(conversation_id=str(request.conversation_id), turn_id=turn['id'], reply=''.join(parts))
            yield _format_status_event(ChatStatusEvent(request_id=request_id, status='completed',
                                                       message='Response complete.', response=response))
        finally:
            with anyio.CancelScope(shield=True):
                await run_in_threadpool(stream.close)
                if not started and not completed:
                    await run_in_threadpool(service.fail_turn, user_id, request, turn)

    return _ChatStreamingResponse(events(), media_type='text/event-stream',
                                  headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


def _format_status_event(event: ChatStatusEvent) -> str:
    return f'event: status\ndata: {event.model_dump_json()}\n\n'

@router.put('/conversations/{conversation_id}/shares', response_model=ShareResponse)
def set_shares(conversation_id: UUID, request: ShareRequest, user_id: User, store: Repository = Depends(get_store)):
    if user_id in request.user_ids:
        raise HTTPException(422, 'You cannot share a conversation with yourself.')
    return ShareResponse(user_ids=store.set_conversation_shares(user_id, str(conversation_id), request.user_ids))

@router.get('/conversations/{conversation_id}/shares', response_model=ShareResponse)
def get_shares(conversation_id: UUID, user_id: User, store: Repository = Depends(get_store)):
    return ShareResponse(user_ids=store.list_conversation_shares(user_id, str(conversation_id)))

@router.get('/shared', response_model=list[ConversationRecord])
def list_shared(user_id: User, store: Repository = Depends(get_store)):
    return store.list_shared_conversations(user_id)

@router.get('/shared/{conversation_id}', response_model=SharedConversation)
def get_shared(conversation_id: UUID, user_id: User, store: Repository = Depends(get_store)):
    return store.get_shared_conversation(user_id, str(conversation_id))
