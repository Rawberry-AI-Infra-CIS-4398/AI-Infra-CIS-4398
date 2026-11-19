import json
import logging

from app.config import Settings, get_settings
from app.corpora import corpus_owner
from app.repository import Repository, StorageError, ConversationBusy
from app.services.gemini_service import GeminiChatClient, MockChatClient
from app.user_models import ChatRequest, ChatResponse, SourceCitation

logger = logging.getLogger(__name__)


class GenerationError(Exception):
    pass


class ChatService:
    def __init__(self, store: Repository, settings: Settings | None = None, llm_client=None) -> None:
        self.store = store
        self.settings = settings or get_settings()
        self.llm_client = llm_client or self._build_llm_client()

    def _build_llm_client(self):
        client = GeminiChatClient(self.settings)
        return client if client.should_use_real_client() else MockChatClient()

    def prepare_turn(self, user_id: str, request: ChatRequest) -> dict:
        return self.store.start_turn(user_id, str(request.conversation_id), request.message)

    def generate_reply(self, user_id: str, request: ChatRequest, turn: dict | None = None) -> ChatResponse:
        conversation_id = str(request.conversation_id)
        turn = turn if turn is not None else self.prepare_turn(user_id, request)
        try:
            # Retrieval uses the conversation's fixed shared corpus, never the user's uploads.
            chunks = self.store.search_chunks(corpus_owner(turn['domain']), request.message, limit=3)
            # Snapshot the exact passages used for this turn; history is not fresh evidence.
            sources = [SourceCitation(
                document_id=chunk['document_id'], filename=chunk['filename'],
                page_number=chunk.get('page_number'), chunk_index=chunk['chunk_index'],
                excerpt=chunk['text'], source_start_line=chunk.get('source_start_line'),
                source_end_line=chunk.get('source_end_line'),
            ) for chunk in chunks]
            match_percent = round(100 * max(chunk['similarity'] for chunk in chunks)) if chunks else None
            preferences = self.store.get_system_prompt(user_id)
            message = request.message
            # Only retrieved source passages enter document context; history is added separately below.
            # With no matches, preserve the existing conversation behavior without claiming source evidence.
            if chunks:
                # Keep stored source identity beside each excerpt so embedded production footers cannot supply citation labels.
                context = json.dumps([source.model_dump() for source in sources], ensure_ascii=False)
                message = (
                    'Use the following document context to answer the question. '
                    'For citations, copy the filename exactly from the source metadata. '
                    'The page_number is the 1-based page position in the uploaded PDF; '
                    'source_start_line and source_end_line are extracted text line locations. '
                    'Do not substitute filenames or printed page labels found inside excerpts. '
                    'If a location is null, report it as unavailable rather than guessing. '
                    f'Document sources (JSON):\n{context}\nQuestion: {message}'
                )
            # Only completed turns from this conversation, bounded by count and size.
            history = []
            remaining = 12000
            for previous in reversed(turn['history']):
                entry = json.dumps({'user': previous['prompt'], 'assistant': previous['reply']})
                if len(entry) > remaining:
                    break
                history.append(entry)
                remaining -= len(entry)
            if history:
                message = 'Conversation history (prior messages):\n' + '\n'.join(reversed(history)) + '\nCurrent message:\n' + message
            reply = self.llm_client.generate_reply(message, system_instruction=preferences or None)
        except Exception as exc:
            try:
                self.store.finish_turn(user_id, conversation_id, turn['id'], None)
            except (StorageError, ConversationBusy):
                logger.warning('Could not record failed generation; pending turn will expire.')
            if isinstance(exc, StorageError):
                raise
            raise GenerationError() from exc
        response = ChatResponse(conversation_id=conversation_id, turn_id=turn['id'], reply=reply,
                                retrieval_match_percent=match_percent, sources=sources)
        self.store.finish_turn(user_id, conversation_id, turn['id'], reply,
                               retrieval_match_percent=response.retrieval_match_percent,
                               sources=[source.model_dump() for source in response.sources])
        return response
