"""Ingestion output must match the contract the upload endpoint persists."""

from pathlib import Path

import pytest

import ingestion_pipeline
from app.ingestion import split_pages
from app.store import InMemoryStore
from app.user_models import DocumentRecord

TESTDOCS = Path(__file__).resolve().parents[1] / "testdocs"


class RecordingStore(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.batches = []

    def save_documents(self, user_id, documents):
        self.batches.append(documents)
        return super().save_documents(user_id, documents)


def test_load_documents_reads_supported_files_with_original_bytes():
    documents = ingestion_pipeline.load_documents(str(TESTDOCS))
    paths = [Path(path).name for path, _, _ in documents]
    assert "sample-plain-1kb.txt" in paths
    for path, raw_bytes, pages in documents:
        assert raw_bytes == Path(path).read_bytes()
        assert pages == [(None, raw_bytes.decode("utf-8"))]


def test_load_documents_rejects_missing_or_empty_directory(tmp_path):
    with pytest.raises(FileNotFoundError):
        ingestion_pipeline.load_documents(str(tmp_path / "missing"))
    (tmp_path / "notes.md").write_text("unsupported")
    with pytest.raises(FileNotFoundError):
        ingestion_pipeline.load_documents(str(tmp_path))


def test_load_documents_reads_docx_and_skips_files_upload_rejects(tmp_path, monkeypatch):
    from docx import Document

    word = Document()
    word.add_paragraph("alpha from word")
    word.save(tmp_path / "notes.docx")
    (tmp_path / "fake.pdf").write_bytes(b"not a pdf")
    (tmp_path / "damaged.docx").write_bytes(b"PK\x03\x04 damaged")
    (tmp_path / "large.txt").write_text("x" * 1_000_001)
    monkeypatch.setattr(ingestion_pipeline, "MAX_FILE_SIZE_BYTES", 1_000_000)

    documents = ingestion_pipeline.load_documents(str(tmp_path))

    assert [(Path(path).name, pages) for path, _, pages in documents] == [
        ("notes.docx", [(None, "alpha from word")])]


def test_split_documents_keeps_contiguous_indexes_and_page_numbers():
    pages = [(1, "alpha " * 400), (2, "   "), (3, "beta " * 50)]
    chunks = split_pages(pages, "doc-1")
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    assert all(0 < len(c.text) <= 900 for c in chunks)
    assert {c.page_number for c in chunks} == {1, 3}
    assert all(c.document_id == "doc-1" and c.status == "pending" for c in chunks)


def test_ingest_document_saves_one_upload_shaped_batch():
    path = str(TESTDOCS / "google.txt")
    raw_bytes = Path(path).read_bytes()
    store = RecordingStore()

    document_id = ingestion_pipeline.ingest_document(
        store, "alice", path, raw_bytes, ingestion_pipeline.extract_pages(".txt", raw_bytes))

    (document, content, chunks), = store.batches[0]
    assert len(store.batches) == 1
    DocumentRecord(**document)
    assert document["id"] == document_id and document["file_type"] == ".txt"
    assert document["chunk_count"] == len(chunks) > 1
    assert content == raw_bytes
    assert set(chunks[0]) == {"document_id", "chunk_index", "page_number", "text", "status"}
    assert all(c["page_number"] is None for c in chunks)
    assert store.list_documents("bob") == []
    assert store.search_chunks(
        "alice",
        Path(path).read_text(encoding="utf-8").split()[0]
    )



def test_ingest_document_without_text_writes_nothing():
    store = RecordingStore()
    assert ingestion_pipeline.ingest_document(store, "alice", "blank.pdf", b"%PDF-", [(1, "")]) is None
    assert store.batches == []


def test_seed_corpus_tags_domain_and_skips_already_seeded_files():
    store = RecordingStore()
    documents = [("sources/a.txt", b"alpha source", [(None, "alpha source")]),
                 ("sources/b.txt", b"beta source", [(None, "beta source")])]

    assert ingestion_pipeline.seed_corpus(store, "medical", documents) == (["a.txt", "b.txt"], [], [])
    saved = store.list_documents("corpus:medical")
    assert [d["filename"] for d in saved] == ["a.txt", "b.txt"]
    assert all(d["metadata"]["domain"] == "medical" for d in saved)
    assert store.list_documents("corpus:education") == []

    # a re-run, or a changed file reusing a seeded name, adds nothing
    renamed = [("other/b.txt", b"beta changed", [(None, "beta changed")])]
    assert ingestion_pipeline.seed_corpus(store, "medical", documents + renamed) == ([], ["a.txt", "b.txt", "b.txt"], [])
    assert len(store.list_documents("corpus:medical")) == 2


def test_seed_corpus_continues_after_a_failed_file():
    class FlakyStore(RecordingStore):
        def save_documents(self, user_id, documents):
            if documents[0][0]["filename"] == "a.txt":
                raise RuntimeError("embedding quota exceeded")
            return super().save_documents(user_id, documents)

    store = FlakyStore()
    documents = [("sources/a.txt", b"alpha source", [(None, "alpha source")]),
                 ("sources/b.txt", b"beta source", [(None, "beta source")])]

    assert ingestion_pipeline.seed_corpus(store, "education", documents) == (["b.txt"], [], ["a.txt"])
    assert [d["filename"] for d in store.list_documents("corpus:education")] == ["b.txt"]
