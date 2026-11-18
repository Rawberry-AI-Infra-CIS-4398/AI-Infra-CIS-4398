"""Shared education/medical source validation without indexing metadata as text.

Education-named symbols are retained for compatibility; both corpora use this schema.
"""
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, field_validator


class EducationSource(BaseModel):
    model_config = ConfigDict(extra='forbid')

    text: list[str] = Field(min_length=1)
    authors: list[str] = Field(default_factory=list)
    institution: str | None = None
    citation: str = ''
    title: str = ''
    header: str = ''
    date_published: AwareDatetime | None = None
    date_added: AwareDatetime | None = None
    start_page: Annotated[int, Field(ge=1)] | None = None
    source_id: str | None = None

    @field_validator('text')
    @classmethod
    def readable_entries(cls, entries: list[str]) -> list[str]:
        if any(not entry.strip() for entry in entries):
            raise ValueError('Each text entry must contain nonblank text.')
        return entries


class InvalidEducationJson(ValueError):
    pass


def parse_education_json(raw: bytes) -> tuple[list[tuple[int | None, str]], dict]:
    # Explicit decoding rejects invalid UTF-8 and accepts a UTF-8 BOM.
    decoded = raw.decode('utf-8-sig')
    try:
        record = EducationSource.model_validate_json(decoded, strict=True)
    except ValidationError as exc:
        raise InvalidEducationJson('Invalid education source record.') from exc
    metadata = record.model_dump(mode='json', exclude={'text'}, exclude_unset=True)
    if metadata.get('institution') == '':
        metadata['institution'] = None
    # A starting page alone does not establish the page of each text fragment.
    # Preserve it in metadata instead of inventing per-chunk page numbers.
    return [(None, '\n\n'.join(record.text))], metadata
