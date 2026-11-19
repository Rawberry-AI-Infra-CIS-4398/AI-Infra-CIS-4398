"""Load a directory of source JSON files into a shared corpus."""

import argparse
from pathlib import Path

from fastapi import HTTPException, UploadFile

from app.config import get_settings
from app.corpora import corpus_owner
from app.postgres_repository import PostgresRepository
from app.repository import StorageError
from app.services.document_service import DocumentService


def main():
    parser = argparse.ArgumentParser(
        description="Ingest Education or Medical source JSON files."
    )
    parser.add_argument("--directory", required=True)
    parser.add_argument(
        "--domain",
        required=True,
        choices=["education", "medical"],
    )
    args = parser.parse_args()

    directory = Path(args.directory)
    if not directory.is_dir():
        parser.error("The directory does not exist.")

    files = []
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path.suffix.lower() == ".json":
            files.append(path)

    if not files:
        parser.error("No JSON files found in the directory.")

    settings = get_settings()
    if not settings.database_url:
        parser.error("DATABASE_URL must be configured.")
    if not settings.google_cloud_project:
        parser.error("GOOGLE_CLOUD_PROJECT must be configured.")

    repository = PostgresRepository(settings.database_url)
    repository.check_ready()
    service = DocumentService(repository)
    owner = corpus_owner(args.domain)

    completed = 0
    failed = 0

    for path in files:
        source_path = path.relative_to(directory)

        try:
            with path.open("rb") as source:
                uploaded_file = UploadFile(
                    filename=path.name,
                    file=source,
                )
                responses = service.upload_document(owner, [uploaded_file])

            result = responses[0]
            if result.embedding_status != "completed":
                failed += 1
                print(f"NOT INDEXED {source_path}: {result.embedding_status}")
                continue

            completed += 1
            print(
                f"OK {source_path}: "
                f"{result.chunk_count} chunks, "
                f"document_id={result.document_id}"
            )

        except HTTPException as exc:
            failed += 1
            print(f"FAILED {source_path}: {exc.detail}")

        except (OSError, StorageError) as exc:
            failed += 1
            print(f"FAILED {source_path}: {exc}")

    print(f"Finished: {completed} indexed, {failed} failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())