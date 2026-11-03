"""Authenticated durable publication admission and actor-scoped recovery."""
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Header, HTTPException, status

from app.api.deps import get_current_user
from app.models.entities import User
from app.schemas.ops import LiveDatabaseSyncStatusResponse
from app.services.audit_events import build_user_actor_payload
from app.services import publication_jobs
from app.services.publication_job_contract import (
    InvalidPublicationRequest, PublicationAdminRequired, PublicationAuthorityRevoked, PublicationLaneBusy,
    PublicationNotFound, PublicationRequestConflict,
)

router = APIRouter(prefix="/ops", tags=["ops"])


def _error(exc):
    if isinstance(exc,InvalidPublicationRequest):return HTTPException(status_code=400,detail=str(exc))
    if isinstance(exc,(PublicationLaneBusy,PublicationRequestConflict)):return HTTPException(status_code=409,detail=str(exc))
    if isinstance(exc,PublicationNotFound):return HTTPException(status_code=404,detail="Publication request was not found.")
    if isinstance(exc,PublicationAuthorityRevoked):return HTTPException(status_code=403,detail="Publication actor is no longer active.")
    if isinstance(exc,PublicationAdminRequired):return HTTPException(status_code=403,detail="Platform administrator access is required.")
    return HTTPException(status_code=503,detail="Publication is unavailable. Retain your request key and check its status before retrying.")


def _admit(current_user,request_key,body,trigger):
    try:
        if body:
            raise InvalidPublicationRequest("Publication currently selects all approved published records; omit request selectors.")
        payload=publication_jobs.admit_request(actor_id=current_user.id,request_key=request_key,body=body or {},
            actor=build_user_actor_payload(current_user,trigger=trigger))
        return LiveDatabaseSyncStatusResponse(**payload)
    except Exception as exc:
        raise _error(exc) from exc


@router.get("/live-database/sync/capabilities")
def publication_capabilities(current_user: User=Depends(get_current_user)):
    del current_user
    return publication_jobs.capabilities()


@router.post("/live-database/sync",response_model=LiveDatabaseSyncStatusResponse,status_code=status.HTTP_202_ACCEPTED,
             deprecated=True)
def sync_live_database(current_user: User=Depends(get_current_user),
                       request_key: str | None=Header(default=None,alias="Idempotency-Key"),
                       body: dict | None=Body(default=None)):
    """Compatibility URL: accepted means queued, never synchronous success.

    Both publication URLs use the same durable ledger and request key. There is
    no direct transport route that can bypass worker claims or current authority.
    """
    return _admit(current_user,request_key,body,"ops.live_database.sync")


@router.post("/live-database/sync/start",response_model=LiveDatabaseSyncStatusResponse,status_code=status.HTTP_202_ACCEPTED)
def start_live_database_sync(current_user: User=Depends(get_current_user),
                             request_key: str | None=Header(default=None,alias="Idempotency-Key"),
                             body: dict | None=Body(default=None)):
    return _admit(current_user,request_key,body,"ops.live_database.sync.start")


@router.get("/live-database/sync/status",response_model=LiveDatabaseSyncStatusResponse)
def live_database_sync_status(current_user: User=Depends(get_current_user),operation_id: UUID | None=None,
                              request_key: str | None=Header(default=None,alias="Idempotency-Key")):
    try:
        payload=publication_jobs.get_status(actor_id=current_user.id,operation_id=operation_id,request_key=request_key)
        if payload is None:
            payload={"operation_id":None,"state":"IDLE","detail":"No publication request for this account.",
                     "progress_percent":0,**publication_jobs.capabilities()}
        return LiveDatabaseSyncStatusResponse(**payload)
    except Exception as exc:
        raise _error(exc) from exc


@router.get("/live-database/sync/operations/{operation_id}",response_model=LiveDatabaseSyncStatusResponse)
def publication_operation_status(operation_id: UUID,current_user: User=Depends(get_current_user)):
    return live_database_sync_status(current_user=current_user,operation_id=operation_id,request_key=None)


@router.get("/live-database/sync/blocked",response_model=LiveDatabaseSyncStatusResponse)
def blocked_publication(current_user: User=Depends(get_current_user)):
    try:
        payload=publication_jobs.blocked_status(actor_id=current_user.id)
        if payload is None:
            payload={"operation_id":None,"state":"IDLE","detail":"No publication is blocking the lane.",
                     "progress_percent":0,**publication_jobs.capabilities()}
        return LiveDatabaseSyncStatusResponse(**payload)
    except Exception as exc:
        raise _error(exc) from exc


@router.post("/live-database/sync/{operation_id}/reconcile",response_model=LiveDatabaseSyncStatusResponse)
def reconcile_publication(operation_id: UUID,current_user: User=Depends(get_current_user)):
    """Read the existing destination commitment; never repeat a promotion."""
    try:
        return LiveDatabaseSyncStatusResponse(**publication_jobs.reconcile_operation(operation_id,actor_id=current_user.id))
    except Exception as exc:
        raise _error(exc) from exc


@router.post("/live-database/sync/{operation_id}/cancel",response_model=LiveDatabaseSyncStatusResponse)
def cancel_publication(operation_id: UUID,current_user: User=Depends(get_current_user)):
    try:
        return LiveDatabaseSyncStatusResponse(**publication_jobs.cancel_operation(operation_id,actor_id=current_user.id))
    except Exception as exc:
        raise _error(exc) from exc
