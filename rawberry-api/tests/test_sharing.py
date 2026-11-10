"""Owners share conversations read-only with specific users (FR-06)."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.store import InMemoryStore
from test_api import ask, conversation


@pytest.fixture
def client():
    with TestClient(create_app(store=InMemoryStore()), headers={'X-User-ID': 'alice'}) as client:
        yield client


def share(client, identifier, user_ids, user='alice'):
    return client.put(f'/conversations/{identifier}/shares', headers={'X-User-ID': user},
                      json={'user_ids': user_ids})


def shared(client, identifier, user='bob'):
    return client.get(f'/shared/{identifier}', headers={'X-User-ID': user})


def test_recipient_lists_and_reads_shared_conversation(client):
    identifier = conversation(client)
    assert ask(client, identifier, 'What is gamma?').status_code == 200
    response = share(client, identifier, ['bob'])
    assert response.status_code == 200 and response.json() == {'user_ids': ['bob']}
    assert client.get(f'/conversations/{identifier}/shares').json() == {'user_ids': ['bob']}

    listing = client.get('/shared', headers={'X-User-ID': 'bob'}).json()
    assert [(c['id'], c['user_id']) for c in listing] == [(identifier, 'alice')]
    view = shared(client, identifier).json()
    assert view['conversation']['id'] == identifier
    assert [(m['role'], m['content']) for m in view['messages']][0] == ('user', 'What is gamma?')
    assert [m['role'] for m in view['messages']] == ['user', 'assistant']


def test_recipient_cannot_write_or_use_owner_routes(client):
    identifier = conversation(client)
    share(client, identifier, ['bob'])
    assert ask(client, identifier, user='bob').status_code == 404
    assert ask(client, identifier, user='bob', stream=True).status_code == 404
    assert client.get(f'/conversations/{identifier}/messages', headers={'X-User-ID': 'bob'}).status_code == 404
    # Recipients can't see or change who else has access.
    assert client.get(f'/conversations/{identifier}/shares', headers={'X-User-ID': 'bob'}).status_code == 404
    assert share(client, identifier, ['carol'], user='bob').status_code == 404
    assert shared(client, identifier, user='carol').status_code == 404


def test_users_without_a_share_get_not_found(client):
    identifier = conversation(client)
    share(client, identifier, ['bob'])
    assert shared(client, identifier, user='carol').status_code == 404
    assert client.get('/shared', headers={'X-User-ID': 'carol'}).json() == []
    assert shared(client, conversation(client)).status_code == 404  # never shared


def test_revoking_and_resharing(client):
    identifier = conversation(client)
    share(client, identifier, ['bob', 'carol'])
    assert share(client, identifier, ['carol']).json() == {'user_ids': ['carol']}
    assert shared(client, identifier).status_code == 404
    assert shared(client, identifier, user='carol').status_code == 200
    assert share(client, identifier, []).json() == {'user_ids': []}
    assert shared(client, identifier, user='carol').status_code == 404
    # Sharing again restores access without duplicate recipients.
    assert share(client, identifier, ['bob', 'bob']).json() == {'user_ids': ['bob']}
    assert shared(client, identifier).status_code == 200


@pytest.mark.parametrize('user_ids', [['alice'], ['has space'], [''], ['x' * 129], ['u'] * 51])
def test_invalid_share_requests(client, user_ids):
    identifier = conversation(client)
    assert share(client, identifier, user_ids).status_code == 422
    assert client.get(f'/conversations/{identifier}/shares').json() == {'user_ids': []}


def test_other_users_conversation_cannot_be_shared(client):
    identifier = conversation(client, user='bob')
    assert share(client, identifier, ['carol']).status_code == 404


def test_polling_sees_new_messages_and_newer_updated_at(client):
    identifier = conversation(client)
    share(client, identifier, ['bob'])
    before = shared(client, identifier).json()
    assert before['messages'] == []
    ask(client, identifier, 'Follow up')
    after = shared(client, identifier).json()
    assert [m['content'] for m in after['messages']][0] == 'Follow up'
    assert after['conversation']['updated_at'] > before['conversation']['updated_at']


def test_expired_and_purged_conversations_are_no_longer_shared(monkeypatch):
    store = InMemoryStore()
    started = store._now()
    with TestClient(create_app(store=store), headers={'X-User-ID': 'alice'}) as client:
        identifier = conversation(client)
        share(client, identifier, ['bob'])
        monkeypatch.setattr(store, '_now', lambda: started + timedelta(hours=25))
        assert shared(client, identifier).status_code == 404
        assert client.get('/shared', headers={'X-User-ID': 'bob'}).json() == []
        assert store.purge_expired_conversations() == 1
        assert identifier not in store._shares
