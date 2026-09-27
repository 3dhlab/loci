from datetime import datetime
from urllib.parse import urlparse
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator


def _normalize_external_url(value: str | None) -> str | None:
    if value is None:
        return None

    trimmed = value.strip()
    if not trimmed:
        return None

    parsed = urlparse(trimmed)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("external_url must be a valid http:// or https:// URL")

    return trimmed


class ObjectCreate(BaseModel):
    project_id: UUID
    name: str
    description: str | None = None
    external_url: str | None = None
    metadata_json: dict | None = None

    _validate_external_url = field_validator("external_url")(_normalize_external_url)


class ObjectUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    external_url: str | None = None
    metadata_json: dict | None = None
    is_published: bool | None = None

    _validate_external_url = field_validator("external_url")(_normalize_external_url)


class ObjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    name: str
    description: str | None
    external_url: str | None
    metadata_json: dict | None
    is_published: bool
    created_at: datetime
    updated_at: datetime
