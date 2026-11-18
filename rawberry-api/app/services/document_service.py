from __future__ import annotations

import os
from uuid import uuid4

from fastapi import HTTPException, UploadFile, status

from app import ingestion
from app.corpora import corpus_owner
from app.edu_json import InvalidEducationJson, parse_education_json

from app.ingestion import (
    InvalidFileContent,
    UnreadableDocument,
    extract_pages,
    prepare_document,
)
from app.services.document_cleaner import EmptyExtractedText

from app.models import UploadResponse
from app.user_models import DocumentRecord
from app.repository import Repository

# Extracted text is chunked here; PostgreSQL embeds chunks on save.
class DocumentService:
    MAX_FILE_SIZE_BYTES = ingestion.MAX_FILE_SIZE_BYTES
    ALLOWED_EXTENSIONS = ingestion.ALLOWED_EXTENSIONS

    def __init__(self, store: Repository) -> None:
        # Store uploaded files and their extracted chunks through the repository.
        self.store = store


    def _extract_pages(self, ext: str, raw_bytes: bytes) -> list[tuple[int | None, str]]:
        try:
            return extract_pages(ext, raw_bytes)
        except InvalidFileContent as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "success": False,
                    "error_code": "INVALID_FILE_CONTENT",
                    "message": "File content does not match its filename extension.",
                },
            ) from exc
        except UnicodeDecodeError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "success": False,
                    "error_code": "INVALID_TEXT_ENCODING",
                    "message": "Text files must use UTF-8 encoding.",
                },
            ) from exc
        except UnreadableDocument as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "success": False,
                    "error_code": "UNREADABLE_FILE",
                    "message": "The file could not be read. It may be damaged or password-protected.",
                },
            ) from exc

    def upload_document(self, user_id: str, files: list[UploadFile]) -> list[UploadResponse]:
        if not files:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"success": False, "error_code": "NO_FILES", "message": "At least one file is required."},
            )

        # Validate and extract every file before saving any, so one bad file rejects the batch.
        allowed_extensions = set(self.ALLOWED_EXTENSIONS)
        if user_id == corpus_owner('education'):
            allowed_extensions.add('.json')
        validated_files: list[tuple[str, str, bytes, list[tuple[int | None, str]], dict]] = []
        for uploaded_file in files:
            filename = uploaded_file.filename or "upload.bin"
            ext = os.path.splitext(filename)[1].lower()
            if ext not in allowed_extensions:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "success": False,
                        "error_code": "INVALID_FILE_TYPE",
                        "message": ("Supported files: PDF, DOCX, TXT, and education JSON."
                                    if '.json' in allowed_extensions else "Only PDF, DOCX, and TXT files are supported."),
                    },
                )

            raw_bytes = uploaded_file.file.read(self.MAX_FILE_SIZE_BYTES + 1)
            if not raw_bytes:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"success": False, "error_code": "EMPTY_FILE", "message": "Uploaded file is empty."},
                )
            if len(raw_bytes) > self.MAX_FILE_SIZE_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail={"success": False, "error_code": "FILE_TOO_LARGE", "message": "File exceeds 25MB limit."},
                )

            source_metadata = {}
            if ext == '.json':
                try:
                    pages, education = parse_education_json(raw_bytes)
                    source_metadata = {'education': education}
                except UnicodeDecodeError as exc:
                    raise HTTPException(400, detail={
                        'success': False, 'error_code': 'INVALID_TEXT_ENCODING',
                        'message': 'JSON files must use UTF-8 encoding.',
                    }) from exc
                except InvalidEducationJson as exc:
                    raise HTTPException(400, detail={
                        'success': False, 'error_code': 'INVALID_EDU_JSON',
                        'message': ('Expected one education JSON object with a nonempty text array of nonblank strings '
                                    'and valid source metadata. See the upload documentation for supported fields.'),
                    }) from exc
            else:
                pages = self._extract_pages(ext, raw_bytes)
            validated_files.append((filename, ext, raw_bytes, pages, source_metadata))

        responses: list[UploadResponse] = []
        documents: list[tuple[dict, bytes, list[dict]]] = []
        
        for filename, ext, raw_bytes, pages, source_metadata in validated_files:
            document_id = str(uuid4())

            try:
                chunks, cleaning_metadata = prepare_document(
                    pages, document_id
                )
            except EmptyExtractedText as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "success": False,
                        "error_code": "EMPTY_EXTRACTED_TEXT",
                        "message": (
                            "No readable text could be extracted "
                            "from the document."
                        ),
                    },
                ) from exc

            document = DocumentRecord(
                id=document_id,
                filename=filename,
                file_type=ext,
                size_bytes=len(raw_bytes),
                user_id=user_id,
                chunk_count=len(chunks),
                embedding_status="pending",
                
                metadata={
                    **source_metadata,
                    "source": "upload_endpoint",
                    "cleaning": cleaning_metadata,
                },

            )

            documents.append((document.model_dump(), raw_bytes, [chunk.model_dump() for chunk in chunks]))

            responses.append(
                UploadResponse(
                    success=True,
                    document_id=document.id,
                    filename=document.filename,
                    status="uploaded",
                    chunk_count=len(chunks),
                    message="Document uploaded successfully; processing is pending.",
                    embedding_status="pending",
                )
            )

        statuses = self.store.save_documents(user_id, documents)
        # Report what storage actually indexed; PDFs without a text layer and empty files have no chunks.
        for response, embedding_status in zip(responses, statuses, strict=True):
            response.embedding_status = embedding_status
            if embedding_status == "completed":
                response.message = "Document uploaded and indexed successfully."
        return responses
