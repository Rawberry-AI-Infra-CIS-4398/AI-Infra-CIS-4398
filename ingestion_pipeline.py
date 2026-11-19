import argparse
import hashlib
import os
import sys
from uuid import uuid4

# app resolves to rawberry-api/app through the repository-level app package
from app.config import get_settings
from app.corpora import corpus_owner
# the /upload endpoint uses the same file rules, extraction, cleaning and chunking
from app.ingestion import (ALLOWED_EXTENSIONS, MAX_FILE_SIZE_BYTES, InvalidFileContent,
                           UnreadableDocument, extract_pages, prepare_document)
from app.postgres_repository import PostgresRepository
from app.services.document_cleaner import EmptyExtractedText
from app.user_models import DocumentRecord

# developer sources are seeded into one of the shared corpora, never a user's uploads
DOMAINS = ("medical", "education")
# frontend-provided sources live in rawberry-db/sources/<domain>/{pdfs,json}; only pdfs is read here
SOURCES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rawberry-db", "sources")


#updated later to fit final format from front end team
def load_documents(doc_path="testdocs"):
    print(f"loading all documents from directory {doc_path}...")

    # check if directory exists
    if not os.path.exists(doc_path):
        raise FileNotFoundError(f"The directory {doc_path} does not exist")

    # each document is (path, raw bytes, [(page_number, text), ...])
    # raw bytes are kept because document_content stores the original file
    documents = []
    for filename in sorted(os.listdir(doc_path)):
        path = os.path.join(doc_path, filename)
        ext = os.path.splitext(filename)[1].lower()
        if not os.path.isfile(path) or ext not in ALLOWED_EXTENSIONS:
            continue
        # same files the /upload endpoint rejects, skipped so one bad file doesn't stop the batch
        if os.path.getsize(path) > MAX_FILE_SIZE_BYTES:
            print(f"Skipping {path}, larger than the {MAX_FILE_SIZE_BYTES // (1024 * 1024)}MB limit")
            continue
        with open(path, "rb") as file:
            raw_bytes = file.read()
        if not raw_bytes:
            print(f"Skipping empty file {path}")
            continue
        try:
            pages = extract_pages(ext, raw_bytes)
        except (InvalidFileContent, UnicodeDecodeError, UnreadableDocument):
            print(f"Skipping unreadable file {path}")
            continue
        documents.append((path, raw_bytes, pages))

    # ensure documents actually contains documents
    if len(documents) == 0:
        raise FileNotFoundError(f"No {', '.join(ALLOWED_EXTENSIONS)} files found in {doc_path} directory. Add files first.")

    # output checks, shows first 2 docs
    for i, (path, raw_bytes, pages) in enumerate(documents[:2]):
        print(f"Document{i+1}")
        print(f" Source {path} ({len(raw_bytes)} bytes, {len(pages)} page(s))")
    return documents


def ingest_document(repository, user_id, path, raw_bytes, pages, domain=None):

    document_id = str(uuid4())

    try:
        chunks, cleaning_metadata = prepare_document(
            pages, document_id
        )
    except EmptyExtractedText:
        print(f"No readable text extracted from {path}, skipping")
        return None


    document = DocumentRecord(
        id=document_id,
        user_id=user_id,
        filename=os.path.basename(path),
        file_type=os.path.splitext(path)[1].lower(),
        size_bytes=len(raw_bytes),
        chunk_count=len(chunks),
        embedding_status="pending",

        metadata={
            "source": "ingestion_pipeline",
            "path": path,
            # lets a re-run recognise files that were already seeded
            "sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "cleaning": cleaning_metadata,
            **({"domain": domain} if domain else {}),
        },

    )
    # same (document, bytes, chunks) shape the /upload endpoint saves
    # the repository embeds every chunk with the pinned Vertex model before writing,
    # so a failed embedding leaves no partial rows
    statuses = repository.save_documents(user_id, [(document.model_dump(), raw_bytes, [c.model_dump() for c in chunks])])
    print(f"Ingested {document.filename}: {len(chunks)} chunks, embedding {statuses[0]}, document id {document_id}")
    return document_id


def seed_corpus(repository, domain, documents):
    """Ingest each document into the domain's corpus; return (ingested, skipped, failed) filenames."""
    owner = corpus_owner(domain)
    # each file commits on its own, so after a crash a re-run only adds what is missing
    existing = repository.list_documents(owner)
    seen_hashes = {(d.get("metadata") or {}).get("sha256") for d in existing}
    seen_names = {d["filename"] for d in existing}

    ingested, skipped, failed = [], [], []
    for path, raw_bytes, pages in documents:
        filename = os.path.basename(path)
        digest = hashlib.sha256(raw_bytes).hexdigest()
        if digest in seen_hashes:
            print(f"Skipping {filename}, already seeded in {owner}")
            skipped.append(filename)
            continue
        if filename in seen_names:
            print(f"Skipping {filename}, a different file with this name is already in {owner}; rename it to seed both")
            skipped.append(filename)
            continue
        try:
            document_id = ingest_document(repository, owner, path, raw_bytes, pages, domain)
        except Exception as exc:
            # one Vertex or database error shouldn't stop the remaining files
            print(f"Failed to ingest {filename}: {type(exc).__name__}: {exc}")
            failed.append(filename)
            continue
        if document_id is None:
            skipped.append(filename)
            continue
        seen_hashes.add(digest)
        seen_names.add(filename)
        ingested.append(filename)
    return ingested, skipped, failed


def main():
    from dotenv import load_dotenv
    load_dotenv()

    parser = argparse.ArgumentParser(description="Load, chunk, and embed developer sources into a RAWBerry corpus.")
    parser.add_argument("--domain", choices=DOMAINS, default=os.getenv("INGEST_DOMAIN"),
                        help="corpus to seed; chats with the same domain search these sources")
    parser.add_argument("--doc-path", help="folder to read; defaults to rawberry-db/sources/<domain>/pdfs")
    args = parser.parse_args()

    if args.domain not in DOMAINS:
        parser.error(f"--domain (or INGEST_DOMAIN) must be one of: {', '.join(DOMAINS)}")
    doc_path = args.doc_path or os.path.join(SOURCES_DIR, args.domain, "pdfs")
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        parser.error("DATABASE_URL must be set")
    # embeddings go through Vertex AI with Application Default Credentials
    if not get_settings().google_cloud_project:
        parser.error("GOOGLE_CLOUD_PROJECT must be set for Vertex AI embeddings")

    repository = PostgresRepository(database_url)
    repository.check_ready()

    #loading documents
    documents = load_documents(doc_path=doc_path)
    ingested, skipped, failed = seed_corpus(repository, args.domain, documents)

    print(f"\nSeeded {corpus_owner(args.domain)}: {len(ingested)} ingested, {len(skipped)} skipped, {len(failed)} failed")
    if failed:
        print("Failed files (safe to re-run; already seeded files are skipped): " + ", ".join(failed))
        sys.exit(1)


# ensures that this file only runs when it is ran directly not when imported by another file
if __name__ == "__main__":
    main()
