"""Opt-in tests: TEST_DATABASE_URL must point to a disposable pgvector database.

Tests create/drop an isolated schema and enable the vector extension if needed.
They never read DATABASE_URL, preventing accidental use of an application database.
"""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.routes import get_chat_service
from app.main import create_app
from app.postgres_repository import PostgresRepository
from app.repository import ConversationBusy, ResourceNotFound, StorageError
from app.retrieval.embeddings import HashEmbeddingProvider
from app.services.chat_service import ChatService
from pdf_samples import make_pdf
from test_api import ask, conversation, exercise_isolation

TEXT_PDF = make_pdf('gamma from PDF')


@pytest.fixture
def database_url(monkeypatch):
    base_url = os.getenv("TEST_DATABASE_URL")
    if not base_url:
        pytest.skip("Set TEST_DATABASE_URL to a disposable PostgreSQL/pgvector database")
    # Exercise real pgvector SQL without making paid Vertex AI requests.
    monkeypatch.setattr('app.postgres_repository.VertexEmbeddingProvider', lambda: HashEmbeddingProvider(768))
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    root = Path(__file__).resolve().parents[2] / "rawberry-db"
    schema = "test_rawberry_" + uuid4().hex
    with psycopg.connect(base_url, autocommit=True) as admin:
        admin.execute("CREATE EXTENSION IF NOT EXISTS vector")
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    url = make_conninfo(base_url, options=f"-c search_path={schema},public")
    try:
        with psycopg.connect(url, autocommit=True) as connection:
            connection.execute((root / "init/schema.sql").read_text())
        yield url
    finally:
        with psycopg.connect(base_url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_data_survives_application_recreation_and_is_shared(database_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("USE_MOCK_CHAT", "true")
    with TestClient(create_app(), headers={'X-User-ID': 'alice', 'X-Developer-Key': 'test-upload-key'}) as first:
        uploaded = first.post("/uploadedu", headers={'X-Developer-Key': 'test-upload-key'}, files=[
            ("files", ("notes.txt", b"alpha beta gamma", "text/plain")),
            ("files", ("paper.pdf", TEXT_PDF, "application/pdf")),
        ])
        assert uploaded.status_code == 200
        assert [d['embedding_status'] for d in uploaded.json()] == ['completed', 'completed']
        assert first.put("/me/system-prompt", json={"prompt": "Be concise"}).status_code == 200
        with TestClient(create_app(), headers={'X-User-ID': 'alice', 'X-Developer-Key': 'test-upload-key'}) as second:
            assert len(second.get("/uploadedu").json()) == 2

    # New app/connection after both previous apps have shut down.
    with TestClient(create_app(), headers={'X-User-ID': 'alice', 'X-Developer-Key': 'test-upload-key'}) as restarted:
        documents = restarted.get("/uploadedu").json()
        assert [d["chunk_count"] for d in documents] == [1, 1]
        assert restarted.get("/me/system-prompt").json()["prompt"] == "Be concise"
        assert restarted.get("/me/system-prompt", headers={"X-User-ID": "bob"}).json()["prompt"] == ""
        conversation_id = restarted.post('/conversations', json={'domain': 'education', 'title': 'Persistent'}).json()['id']
        chat = restarted.post("/chat", json={"conversation_id": conversation_id, "message": "gamma?"})
        assert chat.status_code == 200
        assert "alpha beta gamma" in chat.json()["reply"]
        assert restarted.app.state.store.get_document_content("corpus:education", documents[1]["id"]) == TEXT_PDF
        assert restarted.put("/me/system-prompt", json={"prompt": ""}).status_code == 200
    with TestClient(create_app(), headers={'X-User-ID': 'alice', 'X-Developer-Key': 'test-upload-key'}) as final:
        assert final.get("/me/system-prompt").json()["prompt"] == ""
        history = final.get('/conversations/' + conversation_id + '/messages').json()
        assert [message['role'] for message in history] == ['user', 'assistant']
        assert final.get('/conversations/' + conversation_id + '/messages', headers={'X-User-ID': 'bob'}).status_code == 404


def test_failed_batch_rolls_back_documents_bytes_and_chunks(database_url):
    repository = PostgresRepository(database_url)
    first_id, second_id = str(uuid4()), str(uuid4())

    def document(identifier):
        return {"id": identifier, "owner_id": None, "filename": "notes.txt", "file_type": ".txt",
                "size_bytes": 5, "embedding_status": "pending", "metadata": {}}

    chunk = {"chunk_index": 0, "text": "alpha", "page_number": None, "status": "pending"}
    with pytest.raises(StorageError):
        repository.save_documents("alice", [
            (document(first_id), b"alpha", [chunk]),
            (document(second_id), b"alpha", [chunk, chunk]),  # duplicate index fails after earlier writes
        ])
    assert repository.list_documents("alice") == []
    assert repository.list_chunks("alice") == []
    for identifier in (first_id, second_id):
        with pytest.raises(ResourceNotFound):
            repository.get_document_content('alice', identifier)


def test_uploaded_pdf_is_embedded_and_answers_chat_with_its_page(database_url):
    repository = PostgresRepository(database_url)
    with TestClient(create_app(store=repository), headers={'X-User-ID': 'alice'}) as client:
        uploaded = client.post('/uploadedu', headers={'X-Developer-Key': 'test-upload-key'}, files=[
            ('files', ('paper.pdf', make_pdf('intro page', 'zebra migration patterns'), 'application/pdf')),
        ])
        assert uploaded.status_code == 200
        assert uploaded.json()[0]['embedding_status'] == 'completed'
        assert uploaded.json()[0]['chunk_count'] == 2
        results = repository.search_chunks('corpus:education', 'zebra migration')
        assert (results[0]['text'], results[0]['page_number']) == ('zebra migration patterns', 2)
        conversation_id = client.post('/conversations', json={'domain': 'education', 'title': 'PDF'}).json()['id']
        chat = client.post('/chat', json={'conversation_id': conversation_id, 'message': 'zebra migration?'})
        assert 'zebra migration patterns' in chat.json()['reply']
    assert repository.search_chunks('bob', 'zebra migration') == []


def test_ingestion_pipeline_indexes_documents_for_search(database_url):
    import ingestion_pipeline

    path = Path(__file__).resolve().parents[2] / 'testdocs' / 'google.txt'
    raw_bytes = path.read_bytes()
    repository = PostgresRepository(database_url)
    document_id = ingestion_pipeline.ingest_document(
        repository, 'alice', str(path), raw_bytes, ingestion_pipeline.extract_pages('.txt', raw_bytes))

    documents = repository.list_documents('alice')
    assert [(d['id'], d['embedding_status']) for d in documents] == [(document_id, 'completed')]
    assert documents[0]['chunk_count'] == len(repository.list_chunks('alice')) > 1
    results = repository.search_chunks('alice', path.read_text().split('.')[0])
    assert results and results[0]['document_id'] == document_id
    assert repository.search_chunks('bob', 'google') == []


def test_invalid_api_batch_writes_nothing(database_url):
    repository = PostgresRepository(database_url)
    with TestClient(create_app(store=repository), headers={'X-User-ID': 'alice'}) as client:
        response = client.post("/uploadedu", headers={'X-Developer-Key': 'test-upload-key'}, files=[
            ("files", ("notes.txt", b"valid text", "text/plain")),
            ("files", ("invalid.pdf", b"invalid", "application/pdf")),
        ])
    assert response.status_code == 400
    assert repository.list_documents("alice") == []


def test_postgres_isolation_and_conversations(database_url):
    exercise_isolation(PostgresRepository(database_url))


@pytest.mark.parametrize('padding,expected_turns', [(0, 10), (3000, 3)])
def test_model_context_is_bounded_while_postgres_history_stays_complete(database_url, padding, expected_turns):
    store = PostgresRepository(database_url)
    app = create_app(store=store)
    model = MagicMock()
    model.generate_reply.return_value = 'Recorded answer'
    app.dependency_overrides[get_chat_service] = lambda: ChatService(store, llm_client=model)
    messages = [f'Prior question {index} ' + 'x' * padding for index in range(12)]
    messages = [message.rstrip() for message in messages]
    preference = 'Keep responses brief.'

    with TestClient(app, headers={'X-User-ID': 'alice'}) as client:
        identifier = conversation(client)
        assert client.put('/me/system-prompt', json={'prompt': preference}).status_code == 200
        for message in messages:
            assert ask(client, identifier, message).status_code == 200
        model.generate_reply.side_effect = RuntimeError('Test model failure')
        assert ask(client, identifier, 'Failed question').status_code == 502
        model.generate_reply.side_effect = None

        assert ask(client, identifier, 'Current question').status_code == 200
        prompt, = model.generate_reply.call_args.args
        context, current = prompt.split('\nCurrent message:\n')
        assert current == 'Current question'
        assert [json.loads(line) for line in context.splitlines()[1:]] == [
            {'user': message, 'assistant': 'Recorded answer'} for message in messages[-expected_turns:]
        ]
        assert model.generate_reply.call_args.kwargs == {'system_instruction': preference}
        assert preference not in prompt

        history = client.get(f'/conversations/{identifier}/messages')
        assert history.status_code == 200
        assert [(entry['content'], entry['status']) for entry in history.json() if entry['role'] == 'user'] == [
            *((message, 'completed') for message in messages),
            ('Failed question', 'failed'),
            ('Current question', 'completed'),
        ]


def test_pgvector_persists_ranks_and_filters_owners_and_models(database_url):
    import psycopg
    from app.retrieval.embeddings import EMBEDDING_MODEL

    class Vectors:
        def embed_document(self, text):
            return {'exact': [1.0, 0.0], 'related': [0.8, 0.6], 'unrelated': [0.0, 1.0]}[text] + [0.0] * 766

        def embed_query(self, text):
            return self.embed_document('exact')

    repository = PostgresRepository(database_url, embeddings=Vectors())
    def save(owner, text):
        document = {'id': str(uuid4()), 'filename': 'notes.txt', 'file_type': '.txt',
                    'size_bytes': len(text), 'metadata': {}}
        statuses = repository.save_documents(owner, [(document, text.encode(), [
            {'chunk_index': 0, 'text': text, 'page_number': 4, 'status': 'pending'},
        ])])
        assert statuses == ['completed']
        return document['id']

    related = save('alice', 'related')
    exact = save('alice', 'exact')
    save('alice', 'unrelated')
    save('bob', 'exact')
    # A recreated repository reads the stored vectors, not regenerated document vectors.
    restarted = PostgresRepository(database_url, embeddings=Vectors())
    results = restarted.search_chunks('alice', 'question', limit=5)
    assert [r['document_id'] for r in results] == [exact, related]
    assert [r['similarity'] for r in results] == pytest.approx([1.0, 0.8])
    assert results[0]['page_number'] == 4
    assert len(restarted.search_chunks('alice', 'question', limit=1)) == 1
    assert restarted.search_chunks('nobody', 'question') == []
    assert restarted.search_chunks('alice', '  ') == []
    assert restarted.search_chunks('alice', 'question', limit=0) == []

    with psycopg.connect(database_url) as connection:
        assert connection.execute('SELECT DISTINCT vector_dims(vector) FROM embedding').fetchall() == [(768,)]
        connection.execute('UPDATE embedding SET model = %s WHERE model = %s AND vector_dims(vector) = 768',
                           ('another-model', EMBEDDING_MODEL))
    assert restarted.search_chunks('alice', 'question') == []


def test_embedding_failure_writes_no_documents(database_url):
    class FailingEmbeddings:
        def embed_document(self, text):
            raise RuntimeError('private provider details')

    repository = PostgresRepository(database_url, embeddings=FailingEmbeddings())
    document = {'id': str(uuid4()), 'filename': 'notes.txt', 'file_type': '.txt', 'size_bytes': 5}
    with pytest.raises(StorageError, match='Embedding generation failed'):
        repository.save_documents('alice', [(document, b'alpha', [{'chunk_index': 0, 'text': 'alpha'}])])
    assert repository.list_documents('alice') == []


def test_postgres_serializes_conversation_turns_and_recovers_abandoned_work(database_url):
    import psycopg
    repository = PostgresRepository(database_url)
    other_instance = PostgresRepository(database_url)
    conversation = repository.create_conversation('alice', 'Concurrent')
    identifier = conversation['id']
    first = repository.start_turn('alice', identifier, 'first')
    with pytest.raises(ConversationBusy):
        other_instance.start_turn('alice', identifier, 'second')
    with psycopg.connect(database_url) as connection:
        connection.execute("UPDATE chat_turn SET created_at = CURRENT_TIMESTAMP - INTERVAL '6 minutes' WHERE id = %s", (first['id'],))
    second = other_instance.start_turn('alice', identifier, 'recovered')
    with pytest.raises(ConversationBusy):
        repository.finish_turn('alice', identifier, first['id'], 'late')
    other_instance.finish_turn('alice', identifier, second['id'], 'success')
    assert [m['status'] for m in repository.list_messages('alice', identifier)] == ['failed', 'completed', 'completed']


@pytest.mark.parametrize('endpoint', ['/chat', '/chat/stream'])
def test_persistent_retrieval_reaches_gemini_with_only_owned_context(database_url, monkeypatch, endpoint):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from app.api.routes import get_chat_service
    from app.config import Settings
    from app.retrieval.embeddings import VertexEmbeddingProvider
    from app.services.chat_service import ChatService

    settings = Settings(google_cloud_project='test-project', use_mock_chat=False)
    sdk_client = MagicMock()
    sdk_client.__enter__.return_value = sdk_client
    monkeypatch.setattr('google.genai.Client', MagicMock(return_value=sdk_client))
    selected = 'The Continental Army prevailed at Yorktown.'
    irrelevant = 'An apple pie needs flour and butter.'
    private = 'BOB PRIVATE PAYROLL'
    question = 'Who won the revolutionary battle?'

    def embed(*, model, contents, config):
        # Controlled semantic matches: the question and passage share a vector,
        # while the unrelated passage is orthogonal. No Google calls are made.
        values = ([0.0, 1.0] if contents == irrelevant else [1.0, 0.0]) + [0.0] * 766
        return SimpleNamespace(embeddings=[SimpleNamespace(values=values)])

    sdk_client.models.embed_content.side_effect = embed
    sdk_client.models.generate_content.return_value = SimpleNamespace(text='The Continental Army won.')
    store = PostgresRepository(database_url, embeddings=VertexEmbeddingProvider(settings))
    with TestClient(create_app(store=store), headers={'X-User-ID': 'alice'}) as client:
        for owner, text in [('alice', selected), ('alice', irrelevant), ('bob', private)]:
            uploaded = client.post('/uploadmedical' if owner == 'bob' else '/uploadedu', headers={'X-Developer-Key': 'test-upload-key'}, files=[
                ('files', ('notes.txt', text.encode(), 'text/plain')),
            ])
            assert uploaded.status_code == 200
            assert uploaded.json()[0]['embedding_status'] == 'completed'

    # Recreate application/storage to prove generation uses durable vectors.
    sdk_client.models.embed_content.reset_mock()
    restarted = PostgresRepository(database_url, embeddings=VertexEmbeddingProvider(settings))
    app = create_app(store=restarted)
    app.dependency_overrides[get_chat_service] = lambda: ChatService(restarted, settings=settings)
    with TestClient(app, headers={'X-User-ID': 'alice'}) as client:
        identifier = client.post('/conversations', json={'domain': 'education', 'title': 'History'}).json()['id']
        response = client.post(endpoint, json={'conversation_id': identifier, 'message': question})
        assert response.status_code == 200
        if endpoint == '/chat':
            assert response.json()['reply'] == 'The Continental Army won.'
        else:
            assert '"status":"completed"' in response.text
            assert 'The Continental Army won.' in response.text
        history = client.get(f'/conversations/{identifier}/messages').json()
        assert [entry['status'] for entry in history] == ['completed', 'completed']

    sdk_client.models.embed_content.assert_called_once()
    assert sdk_client.models.embed_content.call_args.kwargs['config']['task_type'] == 'RETRIEVAL_QUERY'
    generation = sdk_client.models.generate_content.call_args.kwargs
    assert generation['model'] == 'gemini-2.5-flash'
    assert selected in generation['contents']
    assert question in generation['contents']
    assert irrelevant not in generation['contents']
    assert private not in generation['contents']
    sdk_client.models.generate_content.assert_called_once()


def test_no_match_does_not_inject_another_users_sources(database_url):
    from unittest.mock import MagicMock

    from app.api.routes import get_chat_service
    from app.services.chat_service import ChatService

    store = PostgresRepository(database_url)
    document = {'id': str(uuid4()), 'filename': 'private.txt', 'file_type': '.txt', 'size_bytes': 32}
    store.save_documents('bob', [(document, b'BOB CONFIDENTIAL PAYROLL NUMBERS', [
        {'chunk_index': 0, 'text': 'BOB CONFIDENTIAL PAYROLL NUMBERS'},
    ])])
    llm = MagicMock()
    llm.generate_reply.return_value = 'A response without source context.'
    app = create_app(store=store)
    app.dependency_overrides[get_chat_service] = lambda: ChatService(store, llm_client=llm)
    with TestClient(app, headers={'X-User-ID': 'alice'}) as client:
        identifier = client.post('/conversations', json={'domain': 'education'}).json()['id']
        response = client.post('/chat', json={'conversation_id': identifier, 'message': 'PAYROLL'})
        assert response.status_code == 200
        assert response.json()['reply'] == 'A response without source context.'
    # Preserve the current no-match contract: generation receives the question
    # without claiming any retrieved evidence; it is not forced to abstain.
    llm.generate_reply.assert_called_once_with('PAYROLL', system_instruction=None)


@pytest.mark.parametrize('endpoint', ['/chat', '/chat/stream'])
def test_query_embedding_failure_records_failed_turn_without_generation(database_url, endpoint):
    from unittest.mock import MagicMock

    from app.api.routes import get_chat_service
    from app.services.chat_service import ChatService

    embeddings = MagicMock()
    embeddings.embed_query.side_effect = RuntimeError('sensitive provider details')
    store = PostgresRepository(database_url, embeddings=embeddings)
    llm = MagicMock()
    app = create_app(store=store)
    app.dependency_overrides[get_chat_service] = lambda: ChatService(store, llm_client=llm)
    with TestClient(app, headers={'X-User-ID': 'alice'}) as client:
        identifier = client.post('/conversations', json={'domain': 'education'}).json()['id']
        response = client.post(endpoint, json={'conversation_id': identifier, 'message': 'question'})
        if endpoint == '/chat':
            assert response.status_code == 503
            assert response.json()['error_code'] == 'STORAGE_UNAVAILABLE'
        else:
            assert response.status_code == 200
            assert '"status":"failed"' in response.text
        assert 'sensitive provider details' not in response.text
        history = client.get(f'/conversations/{identifier}/messages').json()
        assert len(history) == 1 and history[0]['status'] == 'failed'
    llm.generate_reply.assert_not_called()


def test_postgres_sharing_is_read_only_revocable_and_expires(database_url):
    import psycopg

    store = PostgresRepository(database_url)
    identifier = store.create_conversation('alice', 'Shared')['id']
    turn = store.start_turn('alice', identifier, 'What is gamma?')
    store.finish_turn('alice', identifier, turn['id'], 'Gamma is a letter.')

    assert store.set_conversation_shares('alice', identifier, ['carol', 'bob', 'bob']) == ['bob', 'carol']
    assert store.list_conversation_shares('alice', identifier) == ['bob', 'carol']
    assert [c['id'] for c in store.list_shared_conversations('bob')] == [identifier]
    view = store.get_shared_conversation('bob', identifier)
    assert view['conversation']['user_id'] == 'alice' and 'session_id' not in view['conversation']
    assert [m['content'] for m in view['messages']] == ['What is gamma?', 'Gamma is a letter.']
    with pytest.raises(ResourceNotFound):
        store.start_turn('bob', identifier, 'Recipients cannot write')
    with pytest.raises(ResourceNotFound):
        store.set_conversation_shares('bob', identifier, ['dave'])

    # Revoking flags the row; sharing again restores the same row.
    assert store.set_conversation_shares('alice', identifier, ['carol']) == ['carol']
    with pytest.raises(ResourceNotFound):
        store.get_shared_conversation('bob', identifier)
    store.set_conversation_shares('alice', identifier, ['bob'])
    assert store.get_shared_conversation('bob', identifier)['messages']
    with psycopg.connect(database_url) as connection:
        assert connection.execute("SELECT count(*) FROM chat_session_access WHERE user_id = 'bob'").fetchone()[0] == 1

    with psycopg.connect(database_url) as connection:
        connection.execute("UPDATE chat_session SET updated_ts = CURRENT_TIMESTAMP - INTERVAL '25 hours' "
                           "WHERE public_id = %s", (identifier,))
    assert store.list_shared_conversations('bob') == []
    with pytest.raises(ResourceNotFound):
        store.get_shared_conversation('bob', identifier)
