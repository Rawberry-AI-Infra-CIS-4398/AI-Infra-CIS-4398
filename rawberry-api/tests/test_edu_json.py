import json

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.store import InMemoryStore
from test_api import chat_result


@pytest.fixture
def client():
    with TestClient(create_app(store=InMemoryStore())) as client:
        yield client


def source(**changes):
    return {
        'authors': ['Henretta, James A.'],
        'institution': '',
        'text': ['The Saratoga reference code is MAPLE-188.', 'Patriot women produced cloth.'],
        'citation': 'SOURCE METADATA ONLY',
        'title': "America's History",
        'header': 'The Perils of War',
        'date_published': '2014-01-10T05:00:00.000Z',
        'date_added': '2026-09-13T23:50:21.443Z',
        'start_page': 188,
        **changes,
    }


def upload(client, record, *, path='/uploadedu', headers=None, filename='source.json'):
    raw = record if isinstance(record, bytes) else json.dumps(record, ensure_ascii=False).encode('utf-8')
    return client.post(path, headers=headers if headers is not None else {'X-Developer-Key': 'test-upload-key'},
                       files=[('files', (filename, raw, 'application/json'))])


def test_source_is_searchable_but_metadata_is_not_in_chat_context(client):
    record = source()
    raw = json.dumps(record, ensure_ascii=False).encode('utf-8')
    response = upload(client, raw, filename='source.JSON')
    assert response.status_code == 200
    uploaded = response.json()[0]
    assert uploaded['chunk_count'] > 0
    assert uploaded['embedding_status'] == 'pending'  # Memory mode.
    store = client.app.state.store
    document = store.list_documents('corpus:education')[0]
    assert document['file_type'] == '.json'
    metadata = document['metadata']['education']
    assert metadata['authors'] == record['authors']
    assert metadata['title'] == record['title']
    assert metadata['header'] == record['header']
    assert metadata['citation'] == record['citation']
    assert metadata['start_page'] == 188
    assert metadata['institution'] is None
    assert metadata['date_added'].startswith('2026-09-13T23:50:21.443')
    assert 'text' not in metadata
    assert store.get_document_content('corpus:education', uploaded['document_id']) == raw
    chunks = store.list_chunks('corpus:education')
    assert all(chunk['page_number'] is None for chunk in chunks)
    assert '\n\n'.join(record['text']) == chunks[0]['text']
    headers = {'X-User-ID': 'alice'}
    listed = client.get('/uploadedu', headers=headers).json()[0]
    assert listed['metadata']['education'] == metadata
    conversation = client.post('/conversations', headers=headers, json={'domain': 'education'}).json()
    chat = client.post('/chat', headers=headers, json={
        'conversation_id': conversation['id'], 'message': 'What is the Saratoga reference code?',
    })
    answer = chat_result(chat)['reply']
    assert 'MAPLE-188' in answer
    assert 'SOURCE METADATA ONLY' not in answer


def test_minimal_source_bom_and_unicode_are_supported(client):
    raw = b'\xef\xbb\xbf' + json.dumps({'text': ['Patriots\u2019 triumph.']}, ensure_ascii=False).encode()
    assert upload(client, raw).status_code == 200
    assert client.app.state.store.list_chunks('corpus:education')[0]['text'] == 'Patriots\u2019 triumph.'


@pytest.mark.parametrize('record', [
    b'{broken json', [], [source()], {},
    {'text': 'not an array'}, {'text': []}, {'text': ['']}, {'text': ['  ']},
    {'text': [12]}, {'text': [None]},
    source(authors='not an array'), source(authors=[42]),
    source(start_page=0), source(start_page=True), source(start_page='188'),
    source(date_published='not a date'), source(date_added='2026-09-13'),
    source(owner_id='alice'), source(title={'unexpected': 'object'}),
])
def test_invalid_source_rejected_without_writing(client, record):
    response = upload(client, record)
    assert response.status_code == 400
    assert response.json()['error_code'] == 'INVALID_EDU_JSON'
    assert client.app.state.store.list_documents('corpus:education') == []


def test_invalid_encoding_and_cleaned_empty_text(client):
    response = upload(client, b'{"text":["\xff"]}')
    assert response.status_code == 400
    assert response.json()['error_code'] == 'INVALID_TEXT_ENCODING'
    response = upload(client, {'text': ['\u200b']})
    assert response.status_code == 400
    assert response.json()['error_code'] == 'EMPTY_EXTRACTED_TEXT'


def test_json_is_education_only_and_requires_developer_key(client):
    assert upload(client, source(), path='/uploadmedical').status_code == 400
    assert upload(client, source(), headers={}).status_code == 403
    assert upload(client, source(), headers={'X-Developer-Key': 'wrong'}).status_code == 403
    assert client.app.state.store.list_documents('corpus:medical') == []


def test_mixed_batch_and_invalid_json_batch_are_atomic(client):
    files = [('files', ('notes.txt', b'Ordinary education notes.', 'text/plain')),
             ('files', ('source.json', json.dumps(source()).encode(), 'application/json'))]
    headers = {'X-Developer-Key': 'test-upload-key'}
    bad = files + [('files', ('bad.json', b'{}', 'application/json'))]
    assert client.post('/uploadedu', headers=headers, files=bad).status_code == 400
    assert client.app.state.store.list_documents('corpus:education') == []
    response = client.post('/uploadedu', headers=headers, files=files)
    assert response.status_code == 200
    assert len(response.json()) == 2
    assert all(item['chunk_count'] > 0 for item in response.json())


def test_json_uses_existing_file_size_limit(client, monkeypatch):
    monkeypatch.setattr('app.services.document_service.DocumentService.MAX_FILE_SIZE_BYTES', 10)
    response = upload(client, source())
    assert response.status_code == 413
    assert response.json()['error_code'] == 'FILE_TOO_LARGE'


def test_openapi_describes_education_json_support(client):
    paths = client.app.openapi()['paths']
    assert '.json' in paths['/uploadedu']['post']['description']
    assert '.json' not in paths['/uploadmedical']['post']['description']
