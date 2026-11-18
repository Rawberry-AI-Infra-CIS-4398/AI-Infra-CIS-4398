"""Database probes must exercise PostgreSQL, never report memory as connected."""
from unittest.mock import MagicMock

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.postgres_repository import PostgresRepository
from app.store import InMemoryStore


def test_memory_is_not_a_database_connection():
    with TestClient(create_app(store=InMemoryStore())) as client:
        response = client.get('/health/db')
        assert response.status_code == 503
        assert response.json() == {'status': 'unhealthy', 'database': 'not_configured'}
        assert response.headers['cache-control'] == 'no-store'
        assert client.get('/health').json() == {'status': 'healthy'}


@pytest.fixture
def database_client(monkeypatch):
    store = PostgresRepository('postgresql://test:secret@database/test')
    # Startup schema readiness is separate from this per-request connectivity probe.
    monkeypatch.setattr(store, 'check_ready', lambda: None)
    with TestClient(create_app(store=store)) as client:
        yield client


def test_probe_opens_a_connection_on_every_request(database_client, monkeypatch):
    connect = MagicMock()
    connection = connect.return_value.__enter__.return_value
    connection.execute.return_value.fetchone.return_value = {'connected': 1}
    monkeypatch.setattr(psycopg, 'connect', connect)
    for _ in range(2):
        response = database_client.get('/health/db')
        assert response.status_code == 200
        assert response.json() == {'status': 'healthy', 'database': 'connected'}
        assert response.headers['cache-control'] == 'no-store'
    assert connect.call_count == 2
    assert connect.call_args.kwargs['connect_timeout'] == 5
    connection.execute.assert_any_call("SET LOCAL statement_timeout = '3s'")
    connection.execute.assert_any_call('SELECT 1 AS connected')


@pytest.mark.parametrize('failure', ['connect', 'query', 'unexpected_result'])
def test_database_failure_is_safe_and_liveness_stays_healthy(database_client, monkeypatch, failure):
    connect = MagicMock()
    connection = connect.return_value.__enter__.return_value
    if failure == 'connect':
        connect.side_effect = psycopg.OperationalError('password=secret socket=/cloudsql/private')
    elif failure == 'query':
        connection.execute.side_effect = psycopg.OperationalError('private database details')
    else:
        connection.execute.return_value.fetchone.return_value = None
    monkeypatch.setattr(psycopg, 'connect', connect)
    response = database_client.get('/health/db')
    assert response.status_code == 503
    assert response.json() == {'status': 'unhealthy', 'database': 'unavailable'}
    assert database_client.get('/health').status_code == 200
