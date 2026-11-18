"""Serve RAWBerry with local PostgreSQL and deterministic model substitutes."""

import os

import uvicorn
from psycopg.conninfo import conninfo_to_dict

from app.api.routes import get_chat_service
from app.main import create_app
from app.postgres_repository import PostgresRepository
from app.retrieval.embeddings import EMBEDDING_DIMENSIONS, HashEmbeddingProvider
from app.services.chat_service import ChatService
from app.services.gemini_service import MockChatClient


if __name__ == '__main__':
    database_url = os.environ['TEST_DATABASE_URL']
    connection = conninfo_to_dict(database_url)
    if connection.get('host') != '127.0.0.1' or connection.get('dbname') != 'rawberry_test':
        raise SystemExit('Use the disposable local rawberry_test database.')

    store = PostgresRepository(database_url, embeddings=HashEmbeddingProvider(EMBEDDING_DIMENSIONS))
    app = create_app(store=store)
    app.dependency_overrides[get_chat_service] = lambda: ChatService(store, llm_client=MockChatClient())
    uvicorn.run(app, host='127.0.0.1', port=8000)
