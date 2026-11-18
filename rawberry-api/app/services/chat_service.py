import json
import logging
from collections.abc import Iterator

from app.config import Settings, get_settings
from app.corpora import corpus_owner
from app.repository import Repository, StorageError, ConversationBusy, ResourceNotFound
from app.services.gemini_service import GeminiChatClient, MockChatClient
from app.user_models import ChatRequest, ChatResponse

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

    def fail_turn(self, user_id: str, request: ChatRequest, turn: dict) -> None:
        try:
            self.store.finish_turn(user_id, str(request.conversation_id), turn['id'], None)
        except (StorageError, ConversationBusy, ResourceNotFound):
            logger.warning('Could not record failed generation; pending turn will expire if still active.')

    def stream_reply(self, user_id: str, request: ChatRequest, turn: dict) -> Iterator[str]:
        """Yield deltas immediately; persist one complete answer only on success."""
        conversation_id = str(request.conversation_id)
        completed = False
        try:
            message, preferences = self._generation_input(user_id, request, turn)
            parts = []
            stream = self.llm_client.stream_reply(message, system_instruction=preferences)
            try:
                for delta in stream:
                    if delta:
                        parts.append(delta)
                        yield delta
            finally:
                close = getattr(stream, 'close', None)
                if close is not None:
                    close()
            reply = ''.join(parts)
            if not reply.strip():
                raise GenerationError('No answer text was generated.')
            self.store.finish_turn(user_id, conversation_id, turn['id'], reply)
            completed = True
        except Exception as exc:
            if isinstance(exc, StorageError):
                raise
            raise GenerationError() from exc
        finally:
            # Also runs when a consumer closes a partially consumed generator.
            if not completed:
                self.fail_turn(user_id, request, turn)

    def generate_reply(self, user_id: str, request: ChatRequest, turn: dict | None = None) -> ChatResponse:
        conversation_id = str(request.conversation_id)
        turn = turn if turn is not None else self.prepare_turn(user_id, request)
        try:
            message, preferences = self._generation_input(user_id, request, turn)
            reply = self.llm_client.generate_reply(message, system_instruction=preferences or None)
        except Exception as exc:
            try:
                self.store.finish_turn(user_id, conversation_id, turn['id'], None)
            except (StorageError, ConversationBusy):
                logger.warning('Could not record failed generation; pending turn will expire.')
            if isinstance(exc, StorageError):
                raise
            raise GenerationError() from exc
        self.store.finish_turn(user_id, conversation_id, turn['id'], reply)
        return ChatResponse(conversation_id=conversation_id, turn_id=turn['id'], reply=reply)

    def _generation_input(self, user_id: str, request: ChatRequest, turn: dict) -> tuple[str, str | None]:
        # Retrieval combines the conversation's shared corpus with the user's own uploads from the same
        # domain; the best matches across both win, so the corpus still answers when the user uploaded nothing.
        chunks = (self.store.search_chunks(corpus_owner(turn['domain']), request.message, limit=3)
                  + self.store.search_chunks(user_id, request.message, limit=3, domain=turn['domain']))
        chunks = sorted(chunks, key=lambda chunk: chunk['similarity'], reverse=True)[:3]
        preferences = self.store.get_system_prompt(user_id)
        message = request.message
        # Only retrieved source passages enter document context; history is added separately below.
        # With no matches, preserve the existing conversation behavior without claiming source evidence.
        if chunks:
            context = ' '.join(chunk['text'] for chunk in chunks)
            message = f'Use the following document context to answer the question: {context}. Question: {message}'
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
        return message, preferences or None
