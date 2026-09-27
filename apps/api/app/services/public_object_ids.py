"""Normalize stable public object identifiers."""
from __future__ import annotations

LEGACY_WEBSITE_OBJECT_ID_ALIASES: dict[str, str] = {}


def resolve_website_object_id_alias(website_object_id: str) -> str:
    return website_object_id.strip()
