from __future__ import annotations

import enum
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.entities import AuditEvent, User


def build_user_actor_payload(current_user: User, *, trigger: str) -> dict[str, str]:
    return {
        "type": "user",
        "user_id": str(current_user.id),
        "email": current_user.email,
        "trigger": trigger,
    }


def normalize_actor_payload(actor_json: dict | None, *, fallback_trigger: str | None = None) -> dict:
    if isinstance(actor_json, dict):
        normalized = {str(key): _jsonable(value) for key, value in actor_json.items() if value is not None}
    else:
        normalized = {}
    normalized.setdefault("type", "system")
    if fallback_trigger is not None:
        normalized.setdefault("trigger", fallback_trigger)
    return normalized


def append_audit_event(
    db: Session,
    *,
    event_type: str,
    subject_type: str,
    actor_json: dict | None,
    subject_id: UUID | None = None,
    publication_manifest_id: UUID | None = None,
    root_public_id: str | None = None,
    payload_json: dict | None = None,
    created_at: datetime | None = None,
) -> AuditEvent:
    event = AuditEvent(
        event_type=event_type,
        subject_type=subject_type,
        subject_id=subject_id,
        publication_manifest_id=publication_manifest_id,
        root_public_id=root_public_id,
        actor_json=normalize_actor_payload(actor_json),
        payload_json=_jsonable(payload_json or {}),
        created_at=_normalize_timestamp(created_at),
    )
    db.add(event)
    return event


def append_publication_state_event(
    db: Session,
    *,
    subject_type: str,
    subject_id: UUID,
    actor_json: dict | None,
    root_public_id: str | None,
    previous_value: bool,
    next_value: bool,
    extra_payload: dict | None = None,
) -> AuditEvent | None:
    if previous_value == next_value:
        return None
    event_type = f"{subject_type}.published" if next_value else f"{subject_type}.unpublished"
    payload = {"previous_is_published": previous_value, "next_is_published": next_value}
    if extra_payload:
        payload.update(extra_payload)
    return append_audit_event(
        db,
        event_type=event_type,
        subject_type=subject_type,
        subject_id=subject_id,
        actor_json=actor_json,
        root_public_id=root_public_id,
        payload_json=payload,
    )


def append_metadata_change_event(
    db: Session,
    *,
    subject_type: str,
    subject_id: UUID,
    actor_json: dict | None,
    root_public_id: str | None,
    changes: dict,
) -> AuditEvent | None:
    if not changes:
        return None
    return append_audit_event(
        db,
        event_type=f"{subject_type}.metadata_changed",
        subject_type=subject_type,
        subject_id=subject_id,
        actor_json=actor_json,
        root_public_id=root_public_id,
        payload_json={"changes": _jsonable(changes)},
    )


def _normalize_timestamp(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _jsonable(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return _normalize_timestamp(value).isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump(mode="json"))
    return value