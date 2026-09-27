import hashlib
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_current_user_stream
from app.core.config import settings
from app.db.session import get_db
from app.models.entities import (
    AnnotationReviewStatus,
    Clip,
    Object,
    ObjectModel,
    ObjectModelAnnotation,
    Project,
    Segment,
    Transcript,
    User,
    Video,
)
from app.schemas.object_model import (
    ObjectModelAnnotationCreate,
    ObjectModelAnnotationPlaylistEntry,
    ObjectModelAnnotationResponse,
    ObjectModelAnnotationUpdate,
    ObjectModelResponse,
    ObjectModelSettingsUpdate,
)
from app.services.audit_events import append_metadata_change_event, append_publication_state_event, build_user_actor_payload
from app.services.upload_validation import (
    UploadTooLargeError,
    UploadValidationError,
    persist_bounded_upload,
    validate_glb_v2,
)

router = APIRouter(prefix="/objects", tags=["object-models"])


def _owned_object(db: Session, object_id: UUID, owner_id: UUID) -> Object | None:
    return db.execute(
        select(Object).join(Project, Project.id == Object.project_id).where(Object.id == object_id, Project.owner_id == owner_id)
    ).scalar_one_or_none()


def _current_object_model(db: Session, object_id: UUID) -> ObjectModel | None:
    return db.execute(select(ObjectModel).where(ObjectModel.object_id == object_id)).scalar_one_or_none()


def _object_model_directory(object_id: UUID) -> Path:
    root = Path(settings.media_root).resolve() / settings.object_model_output_subdir / str(object_id)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _commit_model_upload(db: Session, model: ObjectModel, destination: Path) -> None:
    """Commit a model identity atomically with its newly persisted file.

    A failed database commit cannot leave an unreferenced multi-gigabyte upload
    behind. Successful replacements retain the immutable previous revision on
    disk for operator rollback and later retention-policy cleanup.
    """

    try:
        db.commit()
    except Exception:
        try:
            db.rollback()
        finally:
            destination.unlink(missing_ok=True)
        raise

    # The commit is durable at this point. A later refresh failure must retain
    # the file referenced by the committed row so a transient readback error
    # cannot turn a successful upload into a broken canonical identity.
    db.refresh(model)


def _validate_annotation_links(
    db: Session,
    owner_id: UUID,
    obj: Object,
    video_id: UUID,
    clip_id: UUID | None,
    transcript_segment_id: UUID | None,
) -> None:
    video = db.execute(
        select(Video).join(Project, Project.id == Video.project_id).where(Video.id == video_id, Project.owner_id == owner_id)
    ).scalar_one_or_none()
    if video is None or video.project_id != obj.project_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Video must belong to the same project as the object")

    if clip_id is not None:
        clip = db.execute(select(Clip).where(Clip.id == clip_id)).scalar_one_or_none()
        if clip is None or clip.video_id != video_id or clip.project_id != obj.project_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Clip must belong to the same video and project")

    if transcript_segment_id is not None:
        segment = db.execute(
            select(Segment).join(Transcript, Transcript.id == Segment.transcript_id).where(Segment.id == transcript_segment_id)
        ).scalar_one_or_none()
        if segment is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Transcript segment not found")
        transcript = db.execute(select(Transcript).where(Transcript.id == segment.transcript_id)).scalar_one()
        if transcript.video_id != video_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Transcript segment must belong to the selected video")


def _normalized_annotation_playlist(
    *,
    video_id: UUID,
    clip_id: UUID | None,
    transcript_segment_id: UUID | None,
    start_ms: int,
    end_ms: int,
    playlist: list[ObjectModelAnnotationPlaylistEntry] | None,
) -> list[dict]:
    entries = playlist or [
        ObjectModelAnnotationPlaylistEntry(
            video_id=video_id,
            clip_id=clip_id,
            transcript_segment_id=transcript_segment_id,
            start_ms=start_ms,
            end_ms=end_ms,
        )
    ]
    normalized = [entry.model_dump(mode="json") for entry in entries]
    for entry in normalized:
        if int(entry["end_ms"]) <= int(entry["start_ms"]):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Each playlist clip must end after it starts")
    return normalized


def _validate_annotation_playlist(db: Session, owner_id: UUID, obj: Object, playlist: list[dict]) -> None:
    for entry in playlist:
        _validate_annotation_links(
            db,
            owner_id,
            obj,
            UUID(str(entry["video_id"])),
            UUID(str(entry["clip_id"])) if entry.get("clip_id") else None,
            UUID(str(entry["transcript_segment_id"])) if entry.get("transcript_segment_id") else None,
        )


def _serialize_annotation(annotation: ObjectModelAnnotation) -> ObjectModelAnnotationResponse:
    playlist = annotation.playlist_json or [
        {
            "video_id": annotation.video_id,
            "clip_id": annotation.clip_id,
            "transcript_segment_id": annotation.transcript_segment_id,
            "label": "Clip 1",
            "start_ms": annotation.start_ms,
            "end_ms": annotation.end_ms,
        }
    ]

    return ObjectModelAnnotationResponse.model_validate(
        {
            "id": annotation.id,
            "object_model_id": annotation.object_model_id,
            "object_id": annotation.object_id,
            "video_id": annotation.video_id,
            "clip_id": annotation.clip_id,
            "transcript_segment_id": annotation.transcript_segment_id,
            "title": annotation.title,
            "description": annotation.description,
            "point_x": annotation.point_x,
            "point_y": annotation.point_y,
            "point_z": annotation.point_z,
            "normal_x": annotation.normal_x,
            "normal_y": annotation.normal_y,
            "normal_z": annotation.normal_z,
            "camera_json": annotation.camera_json,
            "playlist": playlist,
            "start_ms": annotation.start_ms,
            "end_ms": annotation.end_ms,
            "review_status": annotation.review_status,
            "is_published": annotation.is_published,
            "model_revision_created_against": annotation.model_revision_created_against,
            "created_by": annotation.created_by,
            "created_at": annotation.created_at,
            "updated_at": annotation.updated_at,
        }
    )


@router.get("/{object_id}/model", response_model=ObjectModelResponse)
def get_object_model(
    object_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ObjectModel:
    obj = _owned_object(db, object_id, current_user.id)
    if obj is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object not found")

    model = _current_object_model(db, object_id)
    if model is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object model not found")
    return model


@router.post("/{object_id}/model", response_model=ObjectModelResponse, status_code=status.HTTP_201_CREATED)
def upload_object_model(
    object_id: UUID,
    model_file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ObjectModel:
    obj = _owned_object(db, object_id, current_user.id)
    if obj is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object not found")

    filename = Path(model_file.filename or "").name
    if not filename or Path(filename).suffix.lower() != ".glb":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only .glb model uploads are supported")

    existing = _current_object_model(db, object_id)
    next_revision = (existing.revision_number + 1) if existing is not None else 1
    model_directory = _object_model_directory(object_id)
    destination = model_directory / f"rev-{next_revision}-{uuid4().hex[:8]}.glb"
    for _attempt in range(10):
        try:
            uploaded_bytes = persist_bounded_upload(
                model_file.file,
                destination,
                max_bytes=settings.object_model_upload_max_bytes,
            )
            break
        except FileExistsError:
            destination = model_directory / f"rev-{next_revision}-{uuid4().hex[:8]}.glb"
            continue
        except UploadTooLargeError as exc:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Model upload exceeds the configured size limit.",
            ) from exc
        except UploadValidationError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Model upload is empty.") from exc
    else:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A unique model destination could not be allocated.",
        )

    try:
        validate_glb_v2(destination, expected_size_bytes=uploaded_bytes)
    except UploadValidationError as exc:
        destination.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded model is not a valid binary glTF 2 container.",
        ) from exc

    stat = destination.stat()
    previous_path = Path(existing.storage_path) if existing is not None else None
    mime_type = model_file.content_type or "model/gltf-binary"

    if existing is None:
        model = ObjectModel(
            object_id=object_id,
            storage_path=str(destination),
            original_filename=filename,
            mime_type=mime_type,
            sha256_checksum=_sha256_file(destination),
            file_size_bytes=stat.st_size,
            revision_number=next_revision,
            position_x=0,
            position_y=0,
            position_z=0,
            rotation_x=0,
            rotation_y=0,
            rotation_z=0,
            default_camera_json=None,
            uploaded_by=current_user.id,
        )
        db.add(model)
    else:
        existing.storage_path = str(destination)
        existing.original_filename = filename
        existing.mime_type = mime_type
        existing.sha256_checksum = _sha256_file(destination)
        existing.file_size_bytes = stat.st_size
        existing.revision_number = next_revision
        existing.is_published = False
        existing.uploaded_by = current_user.id
        db.add(existing)
        model = existing

        annotations = db.execute(
            select(ObjectModelAnnotation).where(ObjectModelAnnotation.object_id == object_id)
        ).scalars().all()
        for annotation in annotations:
            annotation.review_status = AnnotationReviewStatus.REVIEW_REQUIRED
            annotation.is_published = False
            db.add(annotation)

    _commit_model_upload(db, model, destination)

    # Keep the preceding immutable revision available through the release
    # rollback window. Its stable rev-N filename allows deliberate operator
    # recovery even after the row advances to the new canonical identity.
    _ = previous_path

    return model


@router.patch("/{object_id}/model", response_model=ObjectModelResponse)
def update_object_model_settings(
    object_id: UUID,
    payload: ObjectModelSettingsUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ObjectModel:
    obj = _owned_object(db, object_id, current_user.id)
    if obj is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object not found")

    model = _current_object_model(db, object_id)
    if model is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object model not found")

    previous_is_published = bool(model.is_published)
    previous_values = {
        "position_x": model.position_x,
        "position_y": model.position_y,
        "position_z": model.position_z,
        "rotation_x": model.rotation_x,
        "rotation_y": model.rotation_y,
        "rotation_z": model.rotation_z,
        "default_camera_json": model.default_camera_json,
    }

    if payload.position_x is not None:
        model.position_x = payload.position_x
    if payload.position_y is not None:
        model.position_y = payload.position_y
    if payload.position_z is not None:
        model.position_z = payload.position_z
    if payload.rotation_x is not None:
        model.rotation_x = payload.rotation_x
    if payload.rotation_y is not None:
        model.rotation_y = payload.rotation_y
    if payload.rotation_z is not None:
        model.rotation_z = payload.rotation_z
    if "default_camera_json" in payload.model_fields_set:
        model.default_camera_json = payload.default_camera_json.model_dump() if payload.default_camera_json is not None else None
    if "is_published" in payload.model_fields_set:
        model.is_published = bool(payload.is_published)
    if "notify_followers_on_publish" in payload.model_fields_set:
        # opt-in toggle for the post-publish notify-me digest.
        # No event log entry — this is a UX preference, not a publication
        # state change. The publish handler reads this flag at fire time
        # to decide whether to enqueue the digest.
        model.notify_followers_on_publish = bool(payload.notify_followers_on_publish)

    actor_json = build_user_actor_payload(current_user, trigger="api.object_models.update")
    append_publication_state_event(
        db,
        subject_type="object_model",
        subject_id=model.id,
        actor_json=actor_json,
        root_public_id=obj.website_object_id,
        previous_value=previous_is_published,
        next_value=bool(model.is_published),
    )
    append_metadata_change_event(
        db,
        subject_type="object_model",
        subject_id=model.id,
        actor_json=actor_json,
        root_public_id=obj.website_object_id,
        changes={
            key: {"before": previous_values[key], "after": getattr(model, key)}
            for key in previous_values
            if key in payload.model_fields_set and previous_values[key] != getattr(model, key)
        },
    )
    db.add(model)
    db.commit()
    db.refresh(model)
    return model


@router.delete("/{object_id}/model", status_code=status.HTTP_204_NO_CONTENT)
def delete_object_model(
    object_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    obj = _owned_object(db, object_id, current_user.id)
    if obj is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object not found")

    model = _current_object_model(db, object_id)
    if model is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object model not found")

    storage_path = Path(model.storage_path)
    db.delete(model)
    db.commit()

    if storage_path.exists():
        storage_path.unlink(missing_ok=True)


@router.get("/{object_id}/model/file")
def get_object_model_file(
    object_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_stream),
) -> FileResponse:
    obj = _owned_object(db, object_id, current_user.id)
    if obj is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object not found")

    model = _current_object_model(db, object_id)
    if model is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object model not found")

    path = Path(model.storage_path)
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Stored object model file is missing")

    return FileResponse(path=path, media_type=model.mime_type, filename=model.original_filename)


@router.get("/{object_id}/model/annotations", response_model=list[ObjectModelAnnotationResponse])
def list_object_model_annotations(
    object_id: UUID,
    published_only: bool = Query(default=False),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[ObjectModelAnnotationResponse]:
    obj = _owned_object(db, object_id, current_user.id)
    if obj is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object not found")

    stmt = select(ObjectModelAnnotation).where(ObjectModelAnnotation.object_id == object_id)
    if published_only:
        stmt = stmt.where(ObjectModelAnnotation.is_published.is_(True))

    annotations = db.execute(
        stmt.order_by(ObjectModelAnnotation.created_at.desc())
    ).scalars().all()
    return [_serialize_annotation(annotation) for annotation in annotations]


@router.post("/{object_id}/model/annotations", response_model=ObjectModelAnnotationResponse, status_code=status.HTTP_201_CREATED)
def create_object_model_annotation(
    object_id: UUID,
    payload: ObjectModelAnnotationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ObjectModelAnnotationResponse:
    obj = _owned_object(db, object_id, current_user.id)
    if obj is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object not found")

    model = _current_object_model(db, object_id)
    if model is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Upload a .glb model before creating annotations")

    if payload.end_ms <= payload.start_ms:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Annotation end_ms must be greater than start_ms")

    playlist = _normalized_annotation_playlist(
        video_id=payload.video_id,
        clip_id=payload.clip_id,
        transcript_segment_id=payload.transcript_segment_id,
        start_ms=payload.start_ms,
        end_ms=payload.end_ms,
        playlist=payload.playlist,
    )

    _validate_annotation_links(
        db,
        current_user.id,
        obj,
        payload.video_id,
        payload.clip_id,
        payload.transcript_segment_id,
    )
    _validate_annotation_playlist(db, current_user.id, obj, playlist)

    primary = playlist[0]

    annotation = ObjectModelAnnotation(
        website_annotation_id=f"annotation-{uuid4().hex}",
        object_model_id=model.id,
        object_id=object_id,
        video_id=UUID(str(primary["video_id"])),
        clip_id=UUID(str(primary["clip_id"])) if primary.get("clip_id") else None,
        transcript_segment_id=UUID(str(primary["transcript_segment_id"])) if primary.get("transcript_segment_id") else None,
        title=payload.title,
        description=payload.description,
        point_x=payload.point_x,
        point_y=payload.point_y,
        point_z=payload.point_z,
        normal_x=payload.normal_x,
        normal_y=payload.normal_y,
        normal_z=payload.normal_z,
        camera_json=payload.camera_json,
        playlist_json=playlist,
        start_ms=int(primary["start_ms"]),
        end_ms=int(primary["end_ms"]),
        review_status=AnnotationReviewStatus.ACTIVE,
        model_revision_created_against=model.revision_number,
        created_by=current_user.id,
    )
    db.add(annotation)
    db.commit()
    db.refresh(annotation)
    return _serialize_annotation(annotation)


def _owned_annotation(db: Session, annotation_id: UUID, owner_id: UUID) -> ObjectModelAnnotation | None:
    return db.execute(
        select(ObjectModelAnnotation)
        .join(Object, Object.id == ObjectModelAnnotation.object_id)
        .join(Project, Project.id == Object.project_id)
        .where(ObjectModelAnnotation.id == annotation_id, Project.owner_id == owner_id)
    ).scalar_one_or_none()


@router.patch("/model-annotations/{annotation_id}", response_model=ObjectModelAnnotationResponse)
def update_object_model_annotation(
    annotation_id: UUID,
    payload: ObjectModelAnnotationUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ObjectModelAnnotationResponse:
    annotation = _owned_annotation(db, annotation_id, current_user.id)
    if annotation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Annotation not found")

    updates = payload.model_dump(exclude_unset=True)
    next_video_id = updates.get("video_id", annotation.video_id)
    next_clip_id = updates.get("clip_id", annotation.clip_id)
    next_segment_id = updates.get("transcript_segment_id", annotation.transcript_segment_id)
    next_start_ms = updates.get("start_ms", annotation.start_ms)
    next_end_ms = updates.get("end_ms", annotation.end_ms)

    if next_end_ms <= next_start_ms:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Annotation end_ms must be greater than start_ms")

    obj = db.execute(select(Object).where(Object.id == annotation.object_id)).scalar_one()
    _validate_annotation_links(db, current_user.id, obj, next_video_id, next_clip_id, next_segment_id)

    previous_is_published = bool(annotation.is_published)
    previous_values = {
        "video_id": annotation.video_id,
        "clip_id": annotation.clip_id,
        "transcript_segment_id": annotation.transcript_segment_id,
        "title": annotation.title,
        "description": annotation.description,
        "point_x": annotation.point_x,
        "point_y": annotation.point_y,
        "point_z": annotation.point_z,
        "normal_x": annotation.normal_x,
        "normal_y": annotation.normal_y,
        "normal_z": annotation.normal_z,
        "camera_json": annotation.camera_json,
        "start_ms": annotation.start_ms,
        "end_ms": annotation.end_ms,
        "playlist_json": annotation.playlist_json,
        "review_status": annotation.review_status,
    }

    playlist = None
    if "playlist" in updates:
        playlist = _normalized_annotation_playlist(
            video_id=next_video_id,
            clip_id=next_clip_id,
            transcript_segment_id=next_segment_id,
            start_ms=next_start_ms,
            end_ms=next_end_ms,
            playlist=payload.playlist,
        )
        _validate_annotation_playlist(db, current_user.id, obj, playlist)

    for key, value in updates.items():
        if key == "playlist":
            continue
        setattr(annotation, key, value)

    if playlist is not None:
        primary = playlist[0]
        annotation.playlist_json = playlist
        annotation.video_id = UUID(str(primary["video_id"]))
        annotation.clip_id = UUID(str(primary["clip_id"])) if primary.get("clip_id") else None
        annotation.transcript_segment_id = UUID(str(primary["transcript_segment_id"])) if primary.get("transcript_segment_id") else None
        annotation.start_ms = int(primary["start_ms"])
        annotation.end_ms = int(primary["end_ms"])

    actor_json = build_user_actor_payload(current_user, trigger="api.object_model_annotations.update")
    append_publication_state_event(
        db,
        subject_type="annotation",
        subject_id=annotation.id,
        actor_json=actor_json,
        root_public_id=obj.website_object_id,
        previous_value=previous_is_published,
        next_value=bool(annotation.is_published),
    )
    annotation_changes = {
        key: {"before": previous_values[key], "after": getattr(annotation, key if key != "playlist_json" else "playlist_json")}
        for key in previous_values
        if key != "playlist_json" and key in updates and previous_values[key] != getattr(annotation, key)
    }
    if playlist is not None and previous_values["playlist_json"] != annotation.playlist_json:
        annotation_changes["playlist_json"] = {
            "before": previous_values["playlist_json"],
            "after": annotation.playlist_json,
        }
    append_metadata_change_event(
        db,
        subject_type="annotation",
        subject_id=annotation.id,
        actor_json=actor_json,
        root_public_id=obj.website_object_id,
        changes=annotation_changes,
    )

    db.add(annotation)
    db.commit()
    db.refresh(annotation)
    return _serialize_annotation(annotation)


@router.delete("/model-annotations/{annotation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_object_model_annotation(
    annotation_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    annotation = _owned_annotation(db, annotation_id, current_user.id)
    if annotation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Annotation not found")

    db.delete(annotation)
    db.commit()


@router.post("/model-annotations/{annotation_id}/mark-reviewed", response_model=ObjectModelAnnotationResponse)
def mark_object_model_annotation_reviewed(
    annotation_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ObjectModelAnnotationResponse:
    annotation = _owned_annotation(db, annotation_id, current_user.id)
    if annotation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Annotation not found")

    annotation.review_status = AnnotationReviewStatus.ACTIVE
    db.add(annotation)
    db.commit()
    db.refresh(annotation)
    return _serialize_annotation(annotation)
