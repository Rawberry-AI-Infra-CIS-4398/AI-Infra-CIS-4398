"""User-scoped contracts; identity is supplied by the trusted upstream service."""
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DocumentRecord(BaseModel):
    id: str
    user_id: str
    filename: str
    file_type: str
    size_bytes: int
    chunk_count: int = 0
    embedding_status: str = "pending"
    metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentChunk(BaseModel):
    document_id: str
    chunk_index: int
    page_number: int | None = None
    text: str
    status: str = "pending"


class SystemPromptRequest(RequestModel):
    prompt: str = Field(default="", max_length=2000)


class SystemPromptResponse(BaseModel):
    user_id: str
    prompt: str


ChatDomain = Literal["medical", "education"]


class ConversationRequest(RequestModel):
    domain: ChatDomain
    title: str = Field(default="New conversation", min_length=1, max_length=200)


class ConversationRecord(BaseModel):
    domain: ChatDomain
    id: str
    user_id: str
    title: str
    description: str | None = Field(default=None, max_length=300)
    created_at: datetime
    updated_at: datetime


class ChatRequest(RequestModel):
    conversation_id: UUID
    message: str = Field(min_length=1, max_length=20000)


class SourceCitation(BaseModel):
    """A retrieved passage supplied as context, not proof of answer correctness."""

    document_id: str
    filename: str = Field(description="Filename exactly as uploaded, from stored document metadata.")
    page_number: int | None = Field(
        default=None, ge=1,
        description="1-based page position in the uploaded PDF, not a printed page label. Null for TXT/DOCX.",
    )
    chunk_index: int = Field(ge=0)
    excerpt: str
    source_start_line: int | None = Field(
        default=None, ge=1,
        description="First extracted text line covered by the passage (1-based, inclusive). PDF numbering restarts on each page; TXT/DOCX numbering spans the extracted document. Null when no source map is available.",
    )
    source_end_line: int | None = Field(
        default=None, ge=1,
        description="Last extracted text line covered by the passage (1-based, inclusive). These are extraction line numbers, not printed PDF line labels or proof of which claims the model used.",
    )


class RetrievalMetadata(BaseModel):
    retrieval_match_percent: int | None = Field(
        default=None, ge=0, le=100,
        description="Highest current-query passage similarity as a whole percentage; not answer confidence. Null when no passages were retrieved or for older messages.",
    )
    sources: list[SourceCitation] = Field(
        default_factory=list,
        description="All retrieved passages supplied for this question, in retrieval order. They are not verified supporting evidence.",
    )


class ChatResponse(RetrievalMetadata):
    conversation_id: str
    turn_id: str
    reply: str


class ChatStatusEvent(BaseModel):
    request_id: str
    status: Literal["received", "generating", "completed", "failed"]
    message: str
    response: ChatResponse | None = None


class MessageRecord(RetrievalMetadata):
    id: str
    turn_id: str
    role: Literal["user", "assistant"]
    content: str
    status: Literal["pending", "completed", "failed"]
    created_at: datetime

UserId = Annotated[str, StringConstraints(pattern=r'^[!-~]{1,128}$')]

class ShareRequest(RequestModel):
    user_ids: list[UserId] = Field(max_length=50)

class ShareResponse(BaseModel):
    user_ids: list[str]

class SharedConversation(BaseModel):
    conversation: ConversationRecord
    messages: list[MessageRecord]
