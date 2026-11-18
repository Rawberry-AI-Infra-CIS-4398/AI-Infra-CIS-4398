import asyncio
import json
from datetime import timedelta
from threading import Event
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from app.api.routes import get_chat_service
from app.config import Settings
from app.main import create_app
from app.repository import StorageError
from app.services.chat_service import ChatService
from app.services.gemini_service import GeminiChatClient
from app.store import InMemoryStore
from app.user_models import ChatRequest


def sse_events(text):
    events = []
    for block in text.split('\n\n'):
        if block:
            name, data = block.split('\n', 1)
            events.append((name.removeprefix('event: '), json.loads(data.removeprefix('data: '))))
    return events


@pytest.fixture
def setup():
    store = InMemoryStore()
    llm = MagicMock()
    llm.stream_reply.side_effect = lambda *args, **kwargs: iter(['Hello', ' world', '!\n'])
    llm.generate_reply.return_value = 'Status answer'
    service = ChatService(store, llm_client=llm)
    app = create_app(store=store)
    app.dependency_overrides[get_chat_service] = lambda: service
    identifier = store.create_conversation('alice', 'Streaming', 'education')['id']
    with TestClient(app, headers={'X-User-ID': 'alice'}) as client:
        yield store, llm, service, client, {'conversation_id': identifier, 'message': 'hello'}


def test_stream_deltas_match_completed_answer_and_saved_history(setup):
    store, llm, _, client, body = setup
    response = client.post('/chat', json=body)
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/event-stream')
    assert response.headers['x-accel-buffering'] == 'no'
    events = sse_events(response.text)
    assert [name for name, _ in events] == ['status', 'status', 'delta', 'delta', 'delta', 'status']
    assert [event['status'] for name, event in events if name == 'status'] == ['received', 'generating', 'completed']
    assert len({event['request_id'] for _, event in events}) == 1
    deltas = [event for name, event in events if name == 'delta']
    answer = ''.join(event['delta'] for event in deltas)
    assert answer == 'Hello world!\n'  # Spaces and newlines survive chunk boundaries.
    completed = events[-1][1]['response']
    assert completed['reply'] == answer
    assert all(event['conversation_id'] == body['conversation_id'] and event['turn_id'] == completed['turn_id'] for event in deltas)
    history = store.list_messages('alice', body['conversation_id'])
    assert [(m['role'], m['content'], m['status']) for m in history] == [
        ('user', 'hello', 'completed'), ('assistant', answer, 'completed')]
    assert store.list_conversations('alice')[0]['description'] is not None
    llm.generate_reply.assert_not_called()


def test_only_chat_is_exposed(setup):
    _, _, _, client, body = setup
    paths = client.app.openapi()['paths']
    assert '/chat' in paths
    assert 'text/event-stream' in paths['/chat']['post']['responses']['200']['content']
    for removed in ['/chat/status', '/chat/stream']:
        assert removed not in paths
        assert removed not in client.get('/').json()['available_endpoints']
        assert client.post(removed, json=body).status_code == 404


@pytest.mark.parametrize('failure', ['provider', 'empty', 'retrieval', 'persistence'])
def test_stream_failure_does_not_save_a_partial_answer(setup, monkeypatch, failure):
    store, llm, _, client, body = setup
    def broken_stream(*args, **kwargs):
        yield 'Partial'
        raise RuntimeError('private provider details')
    if failure == 'provider':
        llm.stream_reply.side_effect = broken_stream
    elif failure == 'empty':
        llm.stream_reply.side_effect = lambda *args, **kwargs: iter([])
    elif failure == 'retrieval':
        monkeypatch.setattr(store, 'search_chunks', MagicMock(side_effect=StorageError('private provider details')))
    else:
        finish = store.finish_turn
        def fail_completion(user, identifier, turn, reply):
            if reply is not None:
                raise StorageError('private provider details')
            return finish(user, identifier, turn, reply)
        monkeypatch.setattr(store, 'finish_turn', fail_completion)
    response = client.post('/chat', json=body)
    events = sse_events(response.text)
    assert response.status_code == 200
    assert events[-1][1]['status'] == 'failed'
    assert 'private provider details' not in response.text
    assert not any(event.get('status') == 'completed' for _, event in events)
    messages = store.list_messages('alice', body['conversation_id'])
    assert len(messages) == 1 and messages[0]['status'] == 'failed'


def test_stream_preflight_checks(setup, monkeypatch):
    path = '/chat'
    store, _, _, client, body = setup
    assert client.post(path, json=body, headers={'X-User-ID': 'bob'}).status_code == 404
    assert client.post(path, json={**body, 'conversation_id': str(uuid4())}).status_code == 404
    assert client.post(path, json=body, headers={'X-User-ID': ''}).status_code == 422
    store.start_turn('alice', body['conversation_id'], 'pending')
    assert client.post(path, json=body).status_code == 409
    now = store._now()
    monkeypatch.setattr(store, '_now', lambda: now + timedelta(hours=25))
    assert client.post(path, json=body).status_code == 404


def test_stream_uses_domain_history_and_separate_preferences(setup, monkeypatch):
    store, llm, _, client, body = setup
    first = store.start_turn('alice', body['conversation_id'], 'Prior question')
    store.finish_turn('alice', body['conversation_id'], first['id'], 'Prior answer')
    store.set_system_prompt('alice', 'Be concise')
    search = MagicMock(return_value=[{'text': 'Education context'}])
    monkeypatch.setattr(store, 'search_chunks', search)
    client.post('/chat', json=body)
    search.assert_called_once_with('corpus:education', 'hello', limit=3)
    prompt = llm.stream_reply.call_args.args[0]
    assert all(text in prompt for text in ['Prior question', 'Prior answer', 'Education context', 'hello'])
    assert 'Be concise' not in prompt
    assert llm.stream_reply.call_args.kwargs == {'system_instruction': 'Be concise'}


def test_closing_service_stream_marks_turn_failed_and_closes_provider(setup):
    store, llm, service, _, body = setup
    closed = []
    def provider(*args, **kwargs):
        try:
            yield 'First'
            yield 'Second'
        finally:
            closed.append(True)
    llm.stream_reply.side_effect = provider
    request = ChatRequest(**body)
    stream = service.stream_reply('alice', request, service.prepare_turn('alice', request))
    assert next(stream) == 'First'
    stream.close()
    assert closed == [True]
    assert store.list_messages('alice', body['conversation_id'])[0]['status'] == 'failed'


def test_asgi_sends_first_delta_before_provider_generates_the_next(setup):
    _, llm, _, client, body = setup
    delivered = Event()
    def provider(*args, **kwargs):
        yield 'First'
        assert delivered.is_set(), 'Server buffered the first delta instead of sending it'
        yield 'Second'
    llm.stream_reply.side_effect = provider
    response_parts = []
    async def invoke():
        sent_request = False
        async def receive():
            nonlocal sent_request
            if not sent_request:
                sent_request = True
                return {'type': 'http.request', 'body': json.dumps(body).encode(), 'more_body': False}
            await asyncio.Event().wait()
        async def send(message):
            if message['type'] == 'http.response.body':
                part = message.get('body', b'')
                response_parts.append(part)
                if b'event: delta' in part:
                    delivered.set()
        scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'},
                 'http_version': '1.1', 'method': 'POST', 'scheme': 'http', 'path': '/chat',
                 'raw_path': b'/chat', 'query_string': b'', 'root_path': '',
                 'headers': [(b'content-type', b'application/json'), (b'x-user-id', b'alice')],
                 'server': ('testserver', 80), 'client': ('testclient', 123)}
        await client.app(scope, receive, send)
    asyncio.run(invoke())
    events = sse_events(b''.join(response_parts).decode())
    assert events[-1][1]['status'] == 'completed'
    assert events[-1][1]['response']['reply'] == 'FirstSecond'


@pytest.mark.parametrize('after_delta', [False, True])
def test_disconnect_cleans_up_pending_turn(setup, after_delta):
    store, llm, _, client, body = setup
    closed = []
    def provider(*args, **kwargs):
        try:
            yield 'First'
            yield 'Second'
        finally:
            closed.append(True)
    llm.stream_reply.side_effect = provider
    async def invoke():
        async def receive():
            return {'type': 'http.request', 'body': json.dumps(body).encode(), 'more_body': False}
        async def send(message):
            if message['type'] == 'http.response.body':
                if not after_delta or b'event: delta' in message.get('body', b''):
                    raise OSError('Client disconnected')
        scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'},
                 'http_version': '1.1', 'method': 'POST', 'scheme': 'http', 'path': '/chat',
                 'raw_path': b'/chat', 'query_string': b'', 'root_path': '',
                 'headers': [(b'content-type', b'application/json'), (b'x-user-id', b'alice')],
                 'server': ('testserver', 80), 'client': ('testclient', 123)}
        with pytest.raises(ClientDisconnect):
            await client.app(scope, receive, send)
    asyncio.run(invoke())
    history = store.list_messages('alice', body['conversation_id'])
    assert len(history) == 1 and history[0]['status'] == 'failed'
    assert closed == ([True] if after_delta else [])


@pytest.mark.parametrize('instruction', [None, 'Be concise'])
def test_gemini_stream_uses_provider_chunks_and_closes_resources(monkeypatch, instruction):
    sdk = MagicMock()
    sdk.__enter__.return_value = sdk
    def chunks(**kwargs):
        yield SimpleNamespace(text=' First')
        yield SimpleNamespace(text=None)  # Metadata-only event.
        yield SimpleNamespace(text=' second\n')
    sdk.models.generate_content_stream.side_effect = chunks
    monkeypatch.setattr('app.services.gemini_service.genai', SimpleNamespace(Client=MagicMock(return_value=sdk)))
    client = GeminiChatClient(Settings(google_cloud_project='project', use_mock_chat=False))
    stream = client.stream_reply('question', system_instruction=instruction)
    assert next(stream) == ' First'
    assert list(stream) == [' second\n']
    sdk.models.generate_content_stream.assert_called_once_with(
        model='gemini-2.5-flash', contents='question',
        config={'system_instruction': instruction} if instruction else None)
    sdk.models.generate_content.assert_not_called()
    sdk.__exit__.assert_called_once()
