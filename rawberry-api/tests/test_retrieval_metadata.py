"""Retrieval metadata describes supplied context, and survives history reads."""
import json
from pathlib import Path
from unittest.mock import MagicMock

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.api.routes import get_chat_service
from app.main import create_app
from app.postgres_repository import PostgresRepository
from app.services.chat_service import ChatService
from app.store import InMemoryStore
from test_api import ask, conversation
from test_postgres_integration import database_url  # noqa: F401
from pdf_samples import make_pdf


@pytest.fixture(params=['memory', 'postgres'])
def store(request):
    if request.param == 'postgres':
        return PostgresRepository(request.getfixturevalue('database_url'))
    return InMemoryStore()


def payload(response, stream):
    assert response.status_code == 200
    if stream:
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
        assert events[-1]['status'] == 'completed'
        return events[-1]['response']
    return response.json()


@pytest.mark.parametrize('stream', [False, True])
def test_citations_and_match_survive_history_sharing_and_restart(store, stream):
    app = create_app(store=store)
    with TestClient(app, headers={'X-User-ID': 'alice'}) as client:
        uploaded = client.post('/uploadedu', headers={'X-Developer-Key': 'test-upload-key'}, files=[
            ('files', ('lesson.pdf', make_pdf('unrelated introduction', 'zebra migration patterns'), 'application/pdf')),
            ('files', ('lesson.txt', b'zebra migration', 'text/plain')),
        ])
        assert uploaded.status_code == 200
        docs = {doc['filename']: doc['document_id'] for doc in uploaded.json()}
        # A matching document from the other corpus must never be cited.
        client.post('/uploadmedical', headers={'X-Developer-Key': 'test-upload-key'}, files=[
            ('files', ('private.txt', b'zebra migration patterns', 'text/plain'))])
        identifier = conversation(client)
        answer = payload(ask(client, identifier, 'zebra migration patterns', stream=stream), stream)
        assert answer['retrieval_match_percent'] == 100
        assert answer['sources'][0] == {
            'document_id': docs['lesson.pdf'], 'filename': 'lesson.pdf', 'page_number': 2,
            'chunk_index': 1, 'excerpt': 'zebra migration patterns',
            'source_start_line': 1, 'source_end_line': 1,
        }
        assert {s['filename'] for s in answer['sources']} == {'lesson.pdf', 'lesson.txt'}
        assert next(s for s in answer['sources'] if s['filename'] == 'lesson.txt')['page_number'] is None
        client.put(f'/conversations/{identifier}/shares', json={'user_ids': ['bob']})
        for message in client.get(f'/conversations/{identifier}/messages').json():
            if message['role'] == 'assistant':
                assert message['sources'] == answer['sources']
                assert message['retrieval_match_percent'] == 100
            else:
                assert message['sources'] == [] and message['retrieval_match_percent'] is None
        # No new matches: history must not be presented as freshly retrieved evidence.
        followup = payload(ask(client, identifier, 'qxvplkj', stream=stream), stream)
        assert followup['retrieval_match_percent'] is None and followup['sources'] == []

    restarted = PostgresRepository(store._database_url) if isinstance(store, PostgresRepository) else store
    with TestClient(create_app(store=restarted), headers={'X-User-ID': 'alice'}) as client:
        history = client.get(f'/conversations/{identifier}/messages').json()
        assert history[1]['sources'] == answer['sources']
        assert history[1]['retrieval_match_percent'] == 100
        shared = client.get(f'/shared/{identifier}', headers={'X-User-ID': 'bob'})
        assert shared.json()['messages'] == history
        assert client.get(f'/shared/{identifier}', headers={'X-User-ID': 'eve'}).status_code == 404


def test_percentage_uses_best_passage_and_retains_all_supplied_sources():
    store = InMemoryStore()
    store.search_chunks = MagicMock(return_value=[
        dict(document_id='doc', filename='same.txt', chunk_index=0, page_number=None, text='first', similarity=0.734),
        dict(document_id='doc', filename='same.txt', chunk_index=1, page_number=None, text='second', similarity=0.2),
    ])
    llm = MagicMock()
    llm.generate_reply.return_value = 'Answer'
    app = create_app(store=store)
    app.dependency_overrides[get_chat_service] = lambda: ChatService(store, llm_client=llm)
    with TestClient(app, headers={'X-User-ID': 'alice'}) as client:
        response = ask(client, conversation(client)).json()
    assert response['retrieval_match_percent'] == 73
    assert [s['excerpt'] for s in response['sources']] == ['first', 'second']
    assert all(source['excerpt'] in llm.generate_reply.call_args.args[0] for source in response['sources'])


def test_forward_migration_preserves_old_turns_and_is_repeatable(database_url):
    repository = PostgresRepository(database_url)
    identifier = repository.create_conversation('alice', 'Legacy')['id']
    turn = repository.start_turn('alice', identifier, 'Question')
    repository.finish_turn('alice', identifier, turn['id'], 'Existing answer')
    migration = Path(__file__).resolve().parents[2] / 'rawberry-db/migrations/002_retrieval_metadata.sql'
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute('ALTER TABLE chat_turn DROP COLUMN retrieval_match_percent, DROP COLUMN sources')
        connection.execute("DELETE FROM schema_migration WHERE version = 'retrieval_metadata_002'")
        connection.execute(migration.read_text())
        connection.execute(migration.read_text())
    repository.check_ready()
    saved = repository.list_messages('alice', identifier)[1]
    assert saved['content'] == 'Existing answer'
    assert saved['retrieval_match_percent'] is None and saved['sources'] == []


@pytest.mark.parametrize('name, raw, expected_page, first_line, last_line, excerpt', [
    ('lines.pdf', make_pdf('intro', 'Heading\n\n\nzebra migration patterns\nFinal note'),
     2, 1, 5, 'Heading\n\nzebra migration patterns\nFinal note'),
    ('lines.txt', b'\r\nHeading\r\n\r\n\r\nzebra migration patterns\r\nFinal note',
     None, 2, 6, 'Heading\n\nzebra migration patterns\nFinal note'),
])
def test_citation_lines_refer_to_extracted_source_before_cleaning(
        store, name, raw, expected_page, first_line, last_line, excerpt):
    with TestClient(create_app(store=store), headers={'X-User-ID': 'alice'}) as client:
        uploaded = client.post('/uploadedu', headers={'X-Developer-Key': 'test-upload-key'},
                               files=[('files', (name, raw, 'application/octet-stream'))])
        assert uploaded.status_code == 200
        identifier = conversation(client)
        source = ask(client, identifier, 'zebra migration patterns').json()['sources'][0]
        assert source['document_id'] == uploaded.json()[0]['document_id']
        assert source['page_number'] == expected_page
        assert source['source_start_line'] == first_line
        assert source['source_end_line'] == last_line
        assert source['excerpt'] == excerpt
        saved = client.get(f'/conversations/{identifier}/messages').json()[1]
        assert saved['sources'][0] == source


def test_legacy_document_returns_null_line_ranges(store):
    from uuid import uuid4
    from app.user_models import DocumentRecord

    identifier = str(uuid4())
    document = DocumentRecord(id=identifier, user_id='corpus:education', filename='legacy.txt',
                              file_type='.txt', size_bytes=5, metadata={})
    store.save_documents('corpus:education', [(document.model_dump(), b'zebra', [
        dict(document_id=identifier, chunk_index=0, page_number=None, text='zebra', status='pending'),
    ])])
    with TestClient(create_app(store=store), headers={'X-User-ID': 'alice'}) as client:
        source = ask(client, conversation(client), 'zebra').json()['sources'][0]
    assert source['excerpt'] == 'zebra'
    assert source['source_start_line'] is None and source['source_end_line'] is None


@pytest.mark.parametrize('stream', [False, True])
def test_uploaded_filename_and_pdf_position_reach_model_despite_embedded_labels(store, stream):
    llm = MagicMock()
    llm.generate_reply.return_value = 'Cornwallis sent a deputy.'
    app = create_app(store=store)
    app.dependency_overrides[get_chat_service] = lambda: ChatService(store, llm_client=llm)
    with TestClient(app, headers={'X-User-ID': 'alice'}) as client:
        uploaded = client.post('/uploadedu', headers={'X-Developer-Key': 'test-upload-key'}, files=[
            ('files', ('Ch. 5 Brinkley 14.pdf', make_pdf(
                'Cornwallis sent a deputy. bri38559_ch05_130-161.indd Page 130'), 'application/pdf')),
        ])
        assert uploaded.status_code == 200
        answer = payload(ask(client, conversation(client), 'Cornwallis deputy', stream=stream), stream)
    source = answer['sources'][0]
    assert source['filename'] == 'Ch. 5 Brinkley 14.pdf'
    assert source['page_number'] == 1
    prompt = llm.generate_reply.call_args.args[0]
    metadata = json.loads(prompt.split('Document sources (JSON):\n', 1)[1].split('\nQuestion:', 1)[0])
    assert metadata[0]['filename'] == source['filename']
    assert metadata[0]['page_number'] == source['page_number']
    assert 'bri38559_ch05_130-161.indd Page 130' in metadata[0]['excerpt']
    assert 'copy the filename exactly from the source metadata' in prompt
    assert 'Do not substitute filenames or printed page labels found inside excerpts' in prompt
