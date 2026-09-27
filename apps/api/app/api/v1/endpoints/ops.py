from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_current_user
from app.models.entities import User
from app.schemas.ops import LiveDatabaseSyncResponse, LiveDatabaseSyncStatusResponse
from app.services.audit_events import build_user_actor_payload
from app.services.public_sync import begin_live_database_sync, get_live_database_sync_status, sync_public_projection

router = APIRouter(prefix="/ops", tags=["ops"])

def _serialize_live_database_sync_status(status_payload) -> LiveDatabaseSyncStatusResponse:
    return LiveDatabaseSyncStatusResponse(
        operation_id=getattr(status_payload, "operation_id", None),
        state=getattr(status_payload, "state", "IDLE"),
        stage_key=getattr(status_payload, "stage_key", None),
        stage_label=getattr(status_payload, "stage_label", None),
        detail=getattr(status_payload, "detail", ""),
        progress_percent=getattr(status_payload, "progress_percent", 0),
        started_at=getattr(status_payload, "started_at", None),
        finished_at=getattr(status_payload, "finished_at", None),
        generated_at=getattr(status_payload, "generated_at", None),
        project_count=getattr(status_payload, "project_count", 0),
        object_count=getattr(status_payload, "object_count", 0),
        video_count=getattr(status_payload, "video_count", 0),
        transcript_count=getattr(status_payload, "transcript_count", 0),
        annotation_count=getattr(status_payload, "annotation_count", 0),
        media_file_count=getattr(status_payload, "media_file_count", 0),
        error=getattr(status_payload, "error", None),
    )


@router.post("/live-database/sync", response_model=LiveDatabaseSyncResponse)
def sync_live_database(current_user: User = Depends(get_current_user)) -> LiveDatabaseSyncResponse:
    current_status = get_live_database_sync_status()
    if current_status.state == "RUNNING":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A Live Database sync is already running.",
        )
    try:
        result = sync_public_projection(actor=build_user_actor_payload(current_user, trigger="ops.live_database.sync"))
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc

    changed_media_file_count = getattr(result, "changed_media_file_count", None)
    manifest_changed = getattr(result, "manifest_changed", True)
    if not manifest_changed and changed_media_file_count == 0:
        detail = "Live Database already up to date. No published changes were detected since the last successful sync."
    elif not manifest_changed and isinstance(changed_media_file_count, int):
        detail = (
            "Live Database sync complete. "
            f"Published records were unchanged and {changed_media_file_count} media file"
            f"{'' if changed_media_file_count == 1 else 's'} refreshed."
        )
    else:
        detail = (
            "Live Database sync complete. "
            f"{result.object_count} objects, {result.video_count} videos, "
            f"{result.annotation_count} annotations, and {result.media_file_count} media files promoted."
        )
    return LiveDatabaseSyncResponse(
        detail=detail,
        generated_at=result.generated_at,
        project_count=result.project_count,
        object_count=result.object_count,
        video_count=result.video_count,
        transcript_count=result.transcript_count,
        annotation_count=result.annotation_count,
        media_file_count=result.media_file_count,
    )


@router.post(
    "/live-database/sync/start",
    response_model=LiveDatabaseSyncStatusResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_live_database_sync(current_user: User = Depends(get_current_user)) -> LiveDatabaseSyncStatusResponse:
    return _serialize_live_database_sync_status(
        begin_live_database_sync(actor=build_user_actor_payload(current_user, trigger="ops.live_database.sync.start"))
    )


@router.get("/live-database/sync/status", response_model=LiveDatabaseSyncStatusResponse)
def live_database_sync_status(current_user: User = Depends(get_current_user)) -> LiveDatabaseSyncStatusResponse:
    del current_user
    return _serialize_live_database_sync_status(get_live_database_sync_status())
