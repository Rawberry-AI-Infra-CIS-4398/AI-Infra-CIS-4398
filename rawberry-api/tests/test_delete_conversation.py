from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.postgres_repository import PostgresRepository
from app.repository import ResourceNotFound, StorageError
from app.store import InMemoryStore
from test_postgres_integration import database_url  # noqa: F401


@pytest.fixture(params=['memory', 'postgres'])
def store(request):
    if request.param == 'postgres':
        return PostgresRepository(request.getfixturevalue('database_url'))
    return InMemoryStore()


def test_owner_deletes_history_and_shares_but_preserves_other_data(store):
    identifier = store.create_conversation('alice', 'Delete me')['id']
    other = store.create_conversation('alice', 'Keep me')['id']
    turn = store.start_turn('alice', identifier, 'Question')
    store.finish_turn('alice', identifier, turn['id'], 'Answer')
    store.set_conversation_shares('alice', identifier, ['bob'])
    store.set_system_prompt('alice', 'Keep preferences')
    document = {'id': str(uuid4()), 'filename': 'source.txt', 'file_type': '.txt',
                'size_bytes': 5, 'embedding_status': 'pending'}
    store.save_documents('corpus:education', [(document, b'alpha', [{'chunk_index': 0, 'text': 'alpha'}])])
    with TestClient(create_app(store=store)) as client:
        response = client.delete(f'/conversations/{identifier}', headers={'X-User-ID': 'alice'})
        assert response.status_code == 204 and response.content == b''
        assert [row['id'] for row in store.list_conversations('alice')] == [other]
        assert store.list_shared_conversations('bob') == []
        assert client.get(f'/shared/{identifier}', headers={'X-User-ID': 'bob'}).status_code == 404
        assert client.get(f'/conversations/{identifier}/messages', headers={'X-User-ID': 'alice'}).status_code == 404
        assert client.post('/chat', headers={'X-User-ID': 'alice'}, json={
            'conversation_id': identifier, 'message': 'Cannot revive',
        }).status_code == 404
        assert client.delete(f'/conversations/{identifier}', headers={'X-User-ID': 'alice'}).status_code == 404
    assert store.get_document_content('corpus:education', document['id']) == b'alpha'
    assert store.get_system_prompt('alice') == 'Keep preferences'
    if isinstance(store, InMemoryStore):
        assert identifier not in store._conversations
        assert identifier not in store._turns
        assert identifier not in store._shares
    else:
        with store._connection() as connection:
            assert connection.execute('SELECT id FROM chat_session WHERE public_id = %s', (identifier,)).fetchone() is None
            assert connection.execute('SELECT id FROM chat_turn WHERE id = %s', (turn['id'],)).fetchone() is None
            assert connection.execute('SELECT id FROM chat_session_access').fetchone() is None


def test_recipients_and_unknown_users_cannot_delete(store):
    identifier = store.create_conversation('alice', 'Private')['id']
    store.set_conversation_shares('alice', identifier, ['bob'])
    with TestClient(create_app(store=store)) as client:
        for user in ['bob', 'outsider']:
            assert client.delete(f'/conversations/{identifier}', headers={'X-User-ID': user}).status_code == 404
        assert client.delete(f'/conversations/{uuid4()}', headers={'X-User-ID': 'alice'}).status_code == 404
    assert store.list_conversations('alice')[0]['id'] == identifier
    assert store.get_shared_conversation('bob', identifier)['conversation']['id'] == identifier


def test_delete_pending_turn_prevents_late_completion(store):
    identifier = store.create_conversation('alice', 'Pending')['id']
    turn = store.start_turn('alice', identifier, 'Still generating')
    with TestClient(create_app(store=store)) as client:
        assert client.delete(f'/conversations/{identifier}', headers={'X-User-ID': 'alice'}).status_code == 204
    with pytest.raises(ResourceNotFound):
        store.finish_turn('alice', identifier, turn['id'], 'Late answer')
    assert store.list_conversations('alice') == []


def test_delete_validates_user_header_and_uuid():
    store = InMemoryStore()
    identifier = store.create_conversation('alice', 'Keep')['id']
    with TestClient(create_app(store=store)) as client:
        assert client.delete(f'/conversations/{identifier}').status_code == 422
        assert client.delete(f'/conversations/{identifier}', headers={'X-User-ID': 'has space'}).status_code == 422
        assert client.delete('/conversations/not-a-uuid', headers={'X-User-ID': 'alice'}).status_code == 422
    assert store.list_conversations('alice')[0]['id'] == identifier


def test_expired_conversation_is_not_found(monkeypatch):
    store = InMemoryStore()
    identifier = store.create_conversation('alice', 'Expired')['id']
    now = store._now()
    monkeypatch.setattr(store, '_now', lambda: now + timedelta(hours=25))
    with TestClient(create_app(store=store)) as client:
        assert client.delete(f'/conversations/{identifier}', headers={'X-User-ID': 'alice'}).status_code == 404


def test_storage_failure_keeps_safe_response(monkeypatch):
    store = InMemoryStore()
    def fail(*args):
        raise StorageError('private database details')
    monkeypatch.setattr(store, 'delete_conversation', fail)
    with TestClient(create_app(store=store)) as client:
        response = client.delete(f'/conversations/{uuid4()}', headers={'X-User-ID': 'alice'})
        assert response.status_code == 503
        assert response.json()['error_code'] == 'STORAGE_UNAVAILABLE'
        assert 'private database details' not in response.text


def test_postgres_delete_cascades_legacy_messages_and_citations(database_url):
    store = PostgresRepository(database_url)
    identifier = store.create_conversation('alice', 'Legacy')['id']
    document = {'id': str(uuid4()), 'filename': 'source.txt', 'file_type': '.txt', 'size_bytes': 5}
    store.save_documents('corpus:education', [(document, b'alpha', [{'chunk_index': 0, 'text': 'alpha'}])])
    with store._connection() as connection:
        session_id = connection.execute('SELECT id FROM chat_session WHERE public_id = %s', (identifier,)).fetchone()['id']
        role_id = connection.execute("SELECT id FROM role WHERE name = 'user'").fetchone()['id']
        chunk_id = connection.execute('SELECT id FROM chunk LIMIT 1').fetchone()['id']
        message_id = connection.execute(
            "INSERT INTO message(session_id, role_id, content) VALUES (%s, %s, 'Legacy') RETURNING id",
            (session_id, role_id)).fetchone()['id']
        connection.execute('INSERT INTO citation(message_id, chunk_id, retrieval_score) VALUES (%s, %s, 0.5)',
                           (message_id, chunk_id))
    with TestClient(create_app(store=store)) as client:
        assert client.delete(f'/conversations/{identifier}', headers={'X-User-ID': 'alice'}).status_code == 204
    with store._connection() as connection:
        assert connection.execute('SELECT id FROM message WHERE id = %s', (message_id,)).fetchone() is None
        assert connection.execute('SELECT id FROM citation WHERE message_id = %s', (message_id,)).fetchone() is None
    assert store.get_document_content('corpus:education', document['id']) == b'alpha'
