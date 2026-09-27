from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.entities import Object, Project, User
from app.schemas.object import ObjectCreate, ObjectResponse, ObjectUpdate
from app.services.audit_events import append_metadata_change_event, append_publication_state_event, build_user_actor_payload

router = APIRouter(prefix="/objects", tags=["objects"])


def _owned_project(db: Session, project_id: UUID, owner_id: UUID) -> Project | None:
    return db.execute(select(Project).where(Project.id == project_id, Project.owner_id == owner_id)).scalar_one_or_none()


@router.post("", response_model=ObjectResponse, status_code=status.HTTP_201_CREATED)
def create_object(
    payload: ObjectCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Object:
    if _owned_project(db, payload.project_id, current_user.id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    obj = Object(
        project_id=payload.project_id,
        name=payload.name,
        description=payload.description,
        external_url=payload.external_url,
        metadata_json=payload.metadata_json,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@router.get("", response_model=list[ObjectResponse])
def list_objects(
    project_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[Object]:
    query = select(Object).join(Project, Project.id == Object.project_id).where(Project.owner_id == current_user.id)
    if project_id is not None:
        query = query.where(Object.project_id == project_id)
    return db.execute(query.order_by(Object.created_at.desc())).scalars().all()


@router.get("/{object_id}", response_model=ObjectResponse)
def get_object(object_id: UUID, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)) -> Object:
    obj = db.execute(
        select(Object)
        .join(Project, Project.id == Object.project_id)
        .where(Object.id == object_id, Project.owner_id == current_user.id)
    ).scalar_one_or_none()
    if obj is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object not found")
    return obj


@router.patch("/{object_id}", response_model=ObjectResponse)
def update_object(
    object_id: UUID,
    payload: ObjectUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Object:
    obj = db.execute(
        select(Object)
        .join(Project, Project.id == Object.project_id)
        .where(Object.id == object_id, Project.owner_id == current_user.id)
    ).scalar_one_or_none()
    if obj is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object not found")

    updates = payload.model_dump(exclude_unset=True)
    previous_is_published = bool(obj.is_published)
    previous_values = {
        "name": obj.name,
        "description": obj.description,
        "external_url": obj.external_url,
        "metadata_json": obj.metadata_json,
    }
    for key, value in updates.items():
        setattr(obj, key, value)

    actor_json = build_user_actor_payload(current_user, trigger="api.objects.update")
    append_publication_state_event(
        db,
        subject_type="object",
        subject_id=obj.id,
        actor_json=actor_json,
        root_public_id=obj.website_object_id,
        previous_value=previous_is_published,
        next_value=bool(obj.is_published),
    )
    append_metadata_change_event(
        db,
        subject_type="object",
        subject_id=obj.id,
        actor_json=actor_json,
        root_public_id=obj.website_object_id,
        changes={
            key: {"before": previous_values[key], "after": getattr(obj, key)}
            for key in previous_values
            if key in updates and previous_values[key] != getattr(obj, key)
        },
    )

    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@router.delete("/{object_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_object(object_id: UUID, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)) -> None:
    obj = db.execute(
        select(Object)
        .join(Project, Project.id == Object.project_id)
        .where(Object.id == object_id, Project.owner_id == current_user.id)
    ).scalar_one_or_none()
    if obj is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object not found")

    db.delete(obj)
    db.commit()
