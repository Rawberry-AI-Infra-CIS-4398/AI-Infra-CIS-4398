"""Bridge owner-scoped API chunks to the Stage 1 lexical-vector retrieval API.

These deterministic hash vectors are generated locally, not model embeddings or
persisted pgvector indexes. Both storage adapters use the same ranking path.
"""
from typing import Any

from .embeddings import HashEmbeddingProvider
from .repository import InMemoryRetrievalRepository
from .service import RetrievalService


def search_owned_chunks(
    user_id: str, chunks: list[dict[str, Any]], query: str, limit: int = 3,
) -> list[dict[str, Any]]:
    if not query.strip() or limit <= 0:
        return []
    embeddings = HashEmbeddingProvider()
    repository = InMemoryRetrievalRepository()
    for index, chunk in enumerate(chunks):
        repository.upsert(index, chunk['document_id'], chunk['text'],
                          embeddings.embed_document(chunk['text']), owner_id=user_id)
    results = RetrievalService(embeddings, repository).retrieve(query, limit, owner_id=user_id)
    return [{**chunks[result.chunk_id], 'similarity': result.similarity} for result in results]


def source_line_range(chunk: dict[str, Any], metadata: dict[str, Any]) -> dict[str, int | None]:
    """Use the saved extraction map; never guess locations for legacy documents."""
    for location in metadata.get('cleaning', {}).get('chunk_source_map', []):
        if (location['chunk_index'] == chunk['chunk_index']
                and location.get('page_number') == chunk.get('page_number')):
            return {key: location.get(key) for key in ('source_start_line', 'source_end_line')}
    return {'source_start_line': None, 'source_end_line': None}
