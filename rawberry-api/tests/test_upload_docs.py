from fastapi.testclient import TestClient

from app.main import create_app
from app.store import InMemoryStore


def test_swagger_uses_size_aware_upload_transport():
    with TestClient(create_app(store=InMemoryStore())) as client:
        docs = client.get('/docs')
        assert docs.status_code == 200
        assert docs.text.index('/static/upload-client.js') < docs.text.index('swagger-ui-bundle.js')
        assert 'requestInterceptor: RAWBerryUploads.requestInterceptor' in docs.text
        assert 'earlier batches remain uploaded' in docs.text
        script = client.get('/static/upload-client.js')
        assert script.status_code == 200
        assert '30 * MiB' in script.text and '25 * MiB' in script.text
        assert client.get('/docs/oauth2-redirect').status_code == 200
        assert client.get('/redoc').status_code == 200
        assert '/static/upload-client.js' not in client.app.openapi()['paths']


def test_swagger_upload_assets_respect_proxy_root_path():
    with TestClient(create_app(store=InMemoryStore()), root_path='/api') as client:
        docs = client.get('/docs')
        assert '/api/static/upload-client.js' in docs.text
        assert "url: '/api/openapi.json'" in docs.text
        assert '/api/docs/oauth2-redirect' in docs.text
