"""Parse Education and Medical JSON into text and source metadata."""

import json

from app.edu_json import InvalidEducationJson, parse_education_json


class InvalidSourceJson(ValueError):
    pass


def section_text(section):
    """Read Medical paragraphs and nested subsections."""
    if not isinstance(section, dict):
        raise InvalidSourceJson("Each body section must be an object.")

    heading = section.get("heading", "")
    paragraphs = section.get("paragraphs", [])
    subsections = section.get("subsections", [])

    if not isinstance(heading, str):
        raise InvalidSourceJson("Section headings must be strings.")

    if not isinstance(paragraphs, list):
        raise InvalidSourceJson("Section paragraphs must be a list.")

    for paragraph in paragraphs:
        if not isinstance(paragraph, str):
            raise InvalidSourceJson("Each paragraph must be a string.")

    if not isinstance(subsections, list):
        raise InvalidSourceJson("Section subsections must be a list.")

    parts = []
    has_text = any(paragraph.strip() for paragraph in paragraphs)

    if heading.strip():
        parts.append(heading.strip())

    parts.extend(paragraph.strip() for paragraph in paragraphs if paragraph.strip())

    for subsection in subsections:
        child_parts, child_has_text = section_text(subsection)
        parts.extend(child_parts)
        has_text = has_text or child_has_text

    return parts, has_text


def parse_medical_json(record):
    if record.get("is_stub_body"):
        raise InvalidSourceJson("Medical source contains only a stub body.")

    body = record.get("body")
    abstract = record.get("abstract", "")

    if not isinstance(body, list) or not body:
        raise InvalidSourceJson("Medical source requires body sections.")

    if not isinstance(abstract, str):
        raise InvalidSourceJson("Medical abstract must be a string.")

    parts = []
    if abstract.strip():
        parts.extend(["Abstract", abstract.strip()])

    has_body_text = False
    for section in body:
        section_parts, has_text = section_text(section)
        parts.extend(section_parts)
        has_body_text = has_body_text or has_text

    if not has_body_text:
        raise InvalidSourceJson("Medical body contains no readable paragraphs.")

    authors = record.get("authors", [])
    if not isinstance(authors, list):
        raise InvalidSourceJson("Medical authors must be a list.")

    author_names = []
    for author in authors:
        if not isinstance(author, dict):
            raise InvalidSourceJson("Each Medical author must be an object.")

        name = author.get("name")
        if not isinstance(name, str) or not name.strip():
            raise InvalidSourceJson("Each Medical author needs a nonblank name.")

        author_names.append(name.strip())

    metadata = {
        key: value
        for key, value in record.items()
        if key not in {"abstract", "body", "pdf_text", "authors"}
    }
    metadata["authors"] = author_names
    metadata["author_details"] = authors

    return [(None, "\n\n".join(parts))], metadata


def parse_source_json(raw, domain):
    if domain not in {"education", "medical"}:
        raise InvalidSourceJson("Expected education or medical domain.")

    decoded = raw.decode("utf-8-sig")

    try:
        record = json.loads(decoded)
    except json.JSONDecodeError as exc:
        raise InvalidSourceJson("The file is not valid JSON.") from exc

    if not isinstance(record, dict):
        raise InvalidSourceJson("Expected one JSON object per file.")

    if domain == "medical" and "body" in record:
        return parse_medical_json(record)

    try:
        return parse_education_json(raw)
    except InvalidEducationJson as exc:
        raise InvalidSourceJson(
            "Expected a valid Education text-array or Medical body record."
        ) from exc