import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.postgres_repository import PostgresRepository
from app.store import InMemoryStore
from pdf_samples import make_pdf
from test_postgres_integration import database_url  # noqa: F401


@pytest.fixture(params=['memory', 'postgres'])
def store(request):
    if request.param == 'postgres':
        return PostgresRepository(request.getfixturevalue('database_url'))
    return InMemoryStore()


@pytest.mark.parametrize('domain', ['medical', 'education'])
def test_three_pdf_files_are_returned_stored_and_listed_individually(store, domain):
    samples = [
        ('first.pdf', make_pdf('Saratoga registry code MAPLE-188.')),
        ('second.pdf', make_pdf('Yorktown registry code BIRCH-189.')),
        ('third.pdf', make_pdf('Boston registry code OAK-190.')),
    ]
    headers = {'X-User-ID': 'alice'}
    with TestClient(create_app(store=store)) as client:
        response = client.post('/upload', headers=headers, data={'domain': domain},
                               files=[('files', (name, raw, 'application/pdf')) for name, raw in samples])
        assert response.status_code == 201
        uploaded = response.json()
        assert [item['filename'] for item in uploaded] == [name for name, _ in samples]
        assert len({item['document_id'] for item in uploaded}) == 3
        assert all(item['success'] and item['chunk_count'] > 0 for item in uploaded)
        listed = client.get('/upload', headers=headers, params={'domain': domain}).json()
        assert {item['id'] for item in listed} == {item['document_id'] for item in uploaded}
        for item, (_, raw) in zip(uploaded, samples, strict=True):
            assert store.get_document_content('alice', item['document_id']) == raw
        assert {chunk['document_id'] for chunk in store.list_chunks('alice')} == {
            item['document_id'] for item in uploaded}
        other_domain = 'medical' if domain == 'education' else 'education'
        assert client.get('/upload', headers=headers, params={'domain': other_domain}).json() == []
        assert client.get('/upload', headers={'X-User-ID': 'bob'}, params={'domain': domain}).json() == []
        assert client.get('/uploadedu', headers=headers).json() == []
        assert client.get('/uploadmedical', headers=headers).json() == []
    # PostgreSQL data must remain available in a fresh repository/application.
    if isinstance(store, PostgresRepository):
        with TestClient(create_app(store=PostgresRepository(store._database_url))) as client:
            listed = client.get('/upload', headers=headers, params={'domain': domain}).json()
            assert {item['id'] for item in listed} == {item['document_id'] for item in uploaded}


def test_three_pdfs_with_one_invalid_file_leave_no_partial_batch():
    store = InMemoryStore()
    with TestClient(create_app(store=store)) as client:
        response = client.post('/upload', headers={'X-User-ID': 'alice'}, data={'domain': 'education'}, files=[
            ('files', ('one.pdf', make_pdf('First source.'), 'application/pdf')),
            ('files', ('two.pdf', b'not a PDF', 'application/pdf')),
            ('files', ('three.pdf', make_pdf('Third source.'), 'application/pdf')),
        ])
        assert response.status_code == 400
        assert store.list_documents('alice') == []
