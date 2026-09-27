"""RBAC for the private authoring API.

Roles are project-scoped grants in ``project_role``. The scope key is
``project_key`` = the batch's ``project_slug`` when present, else its
``stable_video_id`` (objects can have a null project_slug).

The authorization decision is factored into pure functions
(``is_authorized``, ``auditor_exclusivity_violation``) so the full RBAC matrix
is unit-testable without HTTP or a database. ``require_role`` wires those into
a FastAPI dependency on top of the existing ``get_current_user``.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterable

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.authoring_lane import OcrBatch, ProjectRole
from app.models.entities import User

# Canonical role vocabulary (mirrors the migration CHECK constraint).
ROLES: frozenset[str] = frozenset({"uploader", "reviewer", "approver", "pm", "operator", "auditor"})
# Any project member may read batch-scoped data.
MEMBER_ROLES: frozenset[str] = ROLES
# Audit read is restricted to oversight roles.
AUDIT_READ_ROLES: frozenset[str] = frozenset({"approver", "pm", "operator", "auditor"})
# Roles that can mutate state; auditor must never co-hold any of these.
MUTATING_ROLES: frozenset[str] = frozenset({"uploader", "reviewer", "approver", "pm", "operator"})


def batch_project_key(batch: OcrBatch) -> str:
    """Project scope key for a batch (non-null even when project_slug is null)."""
    return batch.project_slug or batch.stable_video_id


# --------------------------------------------------------------------------- #
# Pure authorization logic (unit-testable, no DB / no HTTP)
# --------------------------------------------------------------------------- #
def is_authorized(granted_roles: Iterable[str], allowed_roles: Iterable[str]) -> bool:
    """True when the caller holds at least one allowed role."""
    return bool(set(granted_roles) & set(allowed_roles))


def auditor_exclusivity_violation(existing_roles: Iterable[str], new_role: str) -> bool:
    """True when granting ``new_role`` would violate auditor read-only exclusivity.

    Auditor cannot co-hold any mutating role on the same project, in either
    direction (adding auditor to a mutating holder, or adding a mutating role
    to an auditor).
    """
    existing = set(existing_roles)
    if new_role == "auditor":
        return bool(existing & MUTATING_ROLES)
    if new_role in MUTATING_ROLES:
        return "auditor" in existing
    return False


def granted_roles_for_project(db: Session, user_id: uuid.UUID, project_key: str) -> set[str]:
    rows = db.execute(
        select(ProjectRole.role).where(
            ProjectRole.user_id == user_id,
            ProjectRole.project_key == project_key,
        )
    ).scalars().all()
    return set(rows)


def granted_roles_any_project(db: Session, user_id: uuid.UUID) -> set[str]:
    rows = db.execute(select(ProjectRole.role).where(ProjectRole.user_id == user_id)).scalars().all()
    return set(rows)


def granted_project_keys(
    db: Session, user_id: uuid.UUID, roles: Iterable[str] | None = None
) -> set[str]:
    """Project keys on which ``user_id`` holds a role (optionally role-filtered).

    Used to project-scope the cross-batch list views (``list_batches`` /
    ``list_audit``) so a member of project A never sees project B's batches or
    audit trail.
    """
    stmt = select(ProjectRole.project_key).where(ProjectRole.user_id == user_id)
    if roles is not None:
        stmt = stmt.where(ProjectRole.role.in_(list(roles)))
    return set(db.execute(stmt).scalars().all())


def role_scope_lock_key(user_id: uuid.UUID, project_key: str) -> str:
    """Stable key naming the serialization scope for role mutations."""
    return f"project_role:{user_id}:{project_key}"


def lock_role_scope(db: Session, user_id: uuid.UUID, project_key: str) -> None:
    """Serialize concurrent role mutations for one (user_id, project_key).

    Auditor exclusivity is a read-check-then-write invariant: two admins acting
    on the same (user, project) at once could each pass the exclusivity check
    and then insert conflicting rows (e.g. auditor + reviewer), because there is
    no row to row-lock when the scope is empty. A transaction-scoped Postgres
    advisory lock keyed by the scope closes that gap — it serializes the whole
    read-check-insert section and is released automatically on commit/rollback.
    No-op on non-Postgres backends (the app runs on Postgres in real use; this
    only keeps unit/static contexts dialect-agnostic).
    """
    if db.bind is None or db.bind.dialect.name != "postgresql":
        return
    # hashtext -> int4; advisory locks take bigint/int4 keys. Keying by the scope
    # string means only the SAME (user, project) serializes; unrelated scopes are
    # independent. Occasional hash collisions only over-serialize, never under.
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
        {"k": role_scope_lock_key(user_id, project_key)},
    )


def grant_project_role(
    db: Session,
    *,
    user_id: uuid.UUID,
    project_key: str,
    role: str,
    created_by: uuid.UUID | None = None,
) -> ProjectRole:
    """Create a role grant, enforcing auditor read-only exclusivity.

    This is the seam a future admin endpoint uses; B1 exposes no HTTP grant
    route, but the exclusivity rule is enforced and tested here.
    """
    if role not in ROLES:
        raise ValueError(f"unknown role: {role}")
    existing = granted_roles_for_project(db, user_id, project_key)
    if auditor_exclusivity_violation(existing, role):
        raise ValueError(
            f"auditor exclusivity: cannot grant '{role}' on '{project_key}' while holding {sorted(existing)}"
        )
    grant = ProjectRole(user_id=user_id, project_key=project_key, role=role, created_by=created_by)
    db.add(grant)
    db.flush()
    return grant


def find_project_role(db: Session, *, user_id: uuid.UUID, project_key: str, role: str) -> ProjectRole | None:
    """The exact (user, project, role) grant row, or None."""
    return db.execute(
        select(ProjectRole).where(
            ProjectRole.user_id == user_id,
            ProjectRole.project_key == project_key,
            ProjectRole.role == role,
        )
    ).scalar_one_or_none()


def revoke_project_role(db: Session, *, user_id: uuid.UUID, project_key: str, role: str) -> bool:
    """Delete one exact (user, project, role) grant. True if a row was removed."""
    grant = find_project_role(db, user_id=user_id, project_key=project_key, role=role)
    if grant is None:
        return False
    db.delete(grant)
    db.flush()
    return True


# --------------------------------------------------------------------------- #
# FastAPI dependency
# --------------------------------------------------------------------------- #
class AuthorizationContext:
    """Resolved authorization context handed to a route."""

    def __init__(self, user: User, roles: set[str], project_key: str | None):
        self.user = user
        self.roles = roles
        self.project_key = project_key


def require_role(*allowed_roles: str):
    """Dependency factory: require one of ``allowed_roles``.

    If the request path carries ``batch_id``, roles are resolved against that
    batch's project scope; otherwise the caller need only hold an allowed role
    on any project (sufficient for the B1 read endpoints and audit read).
    """
    allowed = set(allowed_roles)
    if not allowed <= ROLES:
        raise ValueError(f"unknown roles in require_role: {allowed - ROLES}")

    def dependency(
        request: Request,
        current_user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> AuthorizationContext:
        batch_id_raw = request.path_params.get("batch_id")
        project_key: str | None = None
        if batch_id_raw is not None:
            try:
                batch_uuid = uuid.UUID(str(batch_id_raw))
            except (ValueError, TypeError) as exc:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="batch not found") from exc
            batch = db.get(OcrBatch, batch_uuid)
            if batch is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="batch not found")
            project_key = batch_project_key(batch)
            roles = granted_roles_for_project(db, current_user.id, project_key)
        else:
            roles = granted_roles_any_project(db, current_user.id)

        if not is_authorized(roles, allowed):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="insufficient role for this authoring action",
            )
        return AuthorizationContext(current_user, roles, project_key)

    return dependency


def require_platform_admin(
    current_user: User = Depends(get_current_user),
) -> User:
    """Require a platform-admin account.

    Platform admin is orthogonal to project-scoped roles: ordinary members,
    reviewers, approvers, PMs, operators, and auditors are all denied unless
    they also carry ``is_platform_admin``. Unauthenticated callers are rejected
    earlier by ``get_current_user`` (401).
    """
    if not getattr(current_user, "is_platform_admin", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="platform admin required for role administration",
        )
    return current_user
