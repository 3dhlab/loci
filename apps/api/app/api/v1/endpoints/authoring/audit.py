"""Append-only audit + pre-write secret scanning for authoring mutations.

Every successful authoring mutation writes exactly one ``AuditEvent`` row
(reusing the existing ``audit_events`` table) inside the same transaction as the
mutation, so a rollback on validation/secret-scan failure leaves no audit row.

The audit payload (actor + before/after) is scanned for secrets before it is
persisted — a reviewer-supplied free-text field (e.g. ``suppression_reason``)
must never smuggle a credential into the durable record.
"""
from __future__ import annotations

import re
import uuid
from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from app.api.v1.endpoints.authoring.rbac import AuthorizationContext, MUTATING_ROLES
from app.models.entities import AuditEvent

# Pre-write secret patterns (names are safe to log; matched values never are).
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai_key", re.compile(r"sk-[A-Za-z0-9_\-]{16,}")),
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private_key_header", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("pg_url", re.compile(r"postgres(?:ql)?(?:\+\w+)?://[^\s\"']+")),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{20,}")),
    ("aws_secret", re.compile(r"aws_secret_access_key\s*[=:]\s*\S+", re.IGNORECASE)),
)

# Bound on a single bulk observation request.
MAX_BULK_TIMESTAMPS = 500


class SecretLeakError(ValueError):
    """Raised when a payload about to be persisted contains a secret-like value."""

    def __init__(self, matches: list[str]):
        self.matches = matches
        super().__init__(f"payload rejected by secret scan: {', '.join(matches)}")


def _iter_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _iter_strings(k)
            yield from _iter_strings(v)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_strings(item)


def scan_for_secrets(payload: Any) -> list[str]:
    """Return the names of any secret patterns found in ``payload`` (recursive)."""
    found: list[str] = []
    for text in _iter_strings(payload):
        for name, pattern in _SECRET_PATTERNS:
            if pattern.search(text):
                found.append(name)
    # Stable, de-duplicated.
    return sorted(set(found))


def assert_no_secrets(payload: Any) -> None:
    matches = scan_for_secrets(payload)
    if matches:
        raise SecretLeakError(matches)


def request_id_of(request: Request | None) -> str | None:
    if request is None:
        return None
    for header in ("x-request-id", "x-correlation-id"):
        value = request.headers.get(header)
        if value:
            return value[:128]
    return None


def authorizing_roles(ctx: AuthorizationContext) -> list[str]:
    """The caller's roles that grant mutation authority (for the audit record)."""
    return sorted(set(ctx.roles) & MUTATING_ROLES)


def write_audit_event(
    db: Session,
    *,
    event_type: str,
    subject_type: str,
    subject_id: uuid.UUID | None,
    ctx: AuthorizationContext,
    before: Any,
    after: Any,
    request: Request | None = None,
) -> AuditEvent:
    """Append one audit event. Scans actor + before/after for secrets first.

    Does NOT commit — the caller commits the mutation and this event together so
    the pair is atomic.
    """
    actor = {
        "user_id": str(ctx.user.id),
        "email": ctx.user.email,
        "roles": authorizing_roles(ctx),
        "project_key": ctx.project_key,
        "request_id": request_id_of(request),
    }
    payload = {"before": before, "after": after}
    # Secret scan everything we are about to persist.
    assert_no_secrets(actor)
    assert_no_secrets(payload)

    event = AuditEvent(
        event_type=event_type,
        subject_type=subject_type,
        subject_id=subject_id,
        actor_json=actor,
        payload_json=payload,
    )
    db.add(event)
    db.flush()
    return event
