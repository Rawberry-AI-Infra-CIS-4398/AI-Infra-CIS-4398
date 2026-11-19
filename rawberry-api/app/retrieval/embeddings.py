"""Vertex AI semantic embeddings and deterministic local embeddings for development."""

from hashlib import blake2b
import math
import re
from typing import Protocol
from app.config import Settings, get_settings
from concurrent.futures import ThreadPoolExecutor

# Keep this pair pinned: changing either value requires re-embedding stored chunks.
EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIMENSIONS = 768


class VertexEmbeddingProvider:
    """Use the same model and dimensions for documents and queries."""
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text, "RETRIEVAL_QUERY")

    def embed_document(self, text: str) -> list[float]:
        return self._embed(text, "RETRIEVAL_DOCUMENT")

    def _new_client(self):
        from google import genai

        if not self.settings.google_cloud_project:
            raise ValueError(
                "Set GOOGLE_CLOUD_PROJECT before using Vertex AI embeddings"
            )

        return genai.Client(
            vertexai=True,
            project=self.settings.google_cloud_project,
            location=self.settings.google_cloud_location,
        )

    def _embed_with_client(self, client, text, task_type):
        if not text.strip():
            raise ValueError("Cannot embed empty text")

        response = client.models.embed_content(
            model=EMBEDDING_MODEL,
            contents=text,
            config={
                "task_type": task_type,
                "output_dimensionality": EMBEDDING_DIMENSIONS,
                "auto_truncate": False,
            },
        )

        if not response.embeddings or len(response.embeddings) != 1:
            raise ValueError("Vertex AI must return one embedding")

        vector = response.embeddings[0].values
        validate_embedding(vector)
        return list(vector)

    def _embed(self, text: str, task_type: str) -> list[float]:
        if not text.strip():
            raise ValueError("Cannot embed empty text")

        with self._new_client() as client:
            return self._embed_with_client(client, text, task_type)

    def embed_documents(self, texts):
        """Embed documents using one client per worker."""
        if not texts:
            return []

        workers = min(8, len(texts))
        indexed_texts = list(enumerate(texts))
        groups = [
            indexed_texts[index::workers]
            for index in range(workers)
        ]

        def process_group(group):
            results = []
            with self._new_client() as client:
                for index, text in group:
                    vector = self._embed_with_client(
                        client, text, "RETRIEVAL_DOCUMENT"
                    )
                    results.append((index, vector))
            return results

        with ThreadPoolExecutor(max_workers=workers) as executor:
            groups_of_results = list(executor.map(process_group, groups))

        results = []
        for group in groups_of_results:
            results.extend(group)

        # Restore original chunk order after processing the groups.
        results.sort(key=lambda item: item[0])
        return [vector for index, vector in results]

def validate_embedding(vector: list[float] | None) -> None:
    """Reject incompatible or unusable vectors before sending them to pgvector."""
    if (
        not vector
        or len(vector) != EMBEDDING_DIMENSIONS
        or not all(math.isfinite(value) for value in vector)
        or not any(vector)
    ):
        raise ValueError("Expected a finite, nonzero 768-dimensional embedding")


class EmbeddingProvider(Protocol):
    def embed_query(self, text: str) -> list[float]: ...

    def embed_document(self, text: str) -> list[float]: ...


class HashEmbeddingProvider:
    """Create stable lexical vectors without network calls or model weights."""

    def __init__(self, dimensions: int = 256) -> None:
        if dimensions < 16:
            raise ValueError("dimensions must be at least 16")
        self.dimensions = dimensions

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    def embed_document(self, text: str) -> list[float]:
        return self._embed(text)

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        words = re.findall(r"[a-z0-9]+", text.lower())
        features = words + [f"{left}_{right}" for left, right in zip(words, words[1:])]
        for feature in features:
            digest = blake2b(feature.encode("utf-8"), digest_size=8).digest()
            value = int.from_bytes(digest, "big")
            index = value % self.dimensions
            vector[index] += 1.0 if value & (1 << 63) else -1.0
        magnitude = math.sqrt(sum(value * value for value in vector))
        return [value / magnitude for value in vector] if magnitude else vector
