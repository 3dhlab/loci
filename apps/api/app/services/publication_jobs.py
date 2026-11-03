"""Durable publication admission/outbox, fenced workers and read-only recovery.

Only the transport module knows deployment credentials or generation files.
Acceptance commits before queue delivery. Redis is a delivery hint; PostgreSQL
is the authority for claims, lease/effect admission and terminal outcomes.
"""
from datetime import timedelta
import logging
from multiprocessing import get_context
from threading import Event, Thread
from uuid import UUID, uuid4

from redis import Redis
from rq import Queue
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import load_only

from app.core.config import settings
from app.db.session import SessionLocal, engine
from app.models.entities import User
from app.models.publication_job import PublicationJob as Job
from app.services.audit_events import append_audit_event
from app.services.publication_job_contract import (
    ACTIVE, CAPABILITIES, DESTINATION, InvalidPublicationRequest, PublicationAdminRequired, PublicationAuthorityRevoked,
    PublicationLaneBusy, PublicationNotFound, PublicationRequestConflict,
    StalePublicationAttempt, public_status, request_identity, validate_cancellation_receipt, validate_receipt, validate_selection,
    validate_superseded_receipt,
)

logger=logging.getLogger(__name__)
LEASE_SECONDS=300
DISPATCH_SECONDS=60
ADMISSION_LOCK=7063179410083121


def capabilities():
    """Advertise durable requests only when code and migrated storage exist."""
    try:
        from app.services import publication_transport
        if not all(callable(getattr(publication_transport,name,None)) for name in
                   ("capture_selection","execute_selected","reconcile_selected","cancel_selected")):
            raise RuntimeError()
        with SessionLocal() as db:
            db.execute(select(Job).limit(0))
        return {"publication_capabilities":dict(CAPABILITIES),"destination":DESTINATION,"protocol_version":1}
    except Exception:
        return {"publication_capabilities":{name:False for name in CAPABILITIES},
                "destination":DESTINATION,"protocol_version":1}


def _now(db):
    return db.scalar(select(func.clock_timestamp()))


def _actor(db, actor_id, *, lock=False):
    query=select(User.is_active).where(User.id==actor_id)
    active=db.scalar(query.with_for_update() if lock else query)
    if active is not True:
        raise PublicationAuthorityRevoked("Publication actor is no longer active.")
    return active


def _admin(db,actor_id):
    _actor(db,actor_id)
    if db.scalar(select(User.is_platform_admin).where(User.id==actor_id)) is not True:
        raise PublicationAdminRequired("Platform administrator access is required.")


def _access(db,job,actor_id,*,admin_recovery=False):
    _actor(db,actor_id)
    if job.actor_id==actor_id:return
    if not admin_recovery:raise PublicationNotFound()
    try:_admin(db,actor_id)
    except PublicationAdminRequired:raise PublicationNotFound() from None


def _execution(job):
    return {"operation_id":str(job.id),"actor_id":str(job.actor_id),"selection_ref":job.selection_ref,
            "selection_digest":job.selection_digest,"metadata":job.selection_metadata_json,
            "attempt_token":str(job.attempt_token) if job.attempt_token else None}


def admit_request(*, actor_id, request_key, body, actor, capture=None):
    key_hash,fingerprint=request_identity(request_key,body)
    actor_id=UUID(str(actor_id))
    if capture is None:
        from app.services.publication_transport import capture_selection
        capture=capture_selection
    with SessionLocal() as db:
        # Shared across API instances. The unique partial lane index remains the
        # final database constraint; no process-global status controls admission.
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"),{"key":ADMISSION_LOCK})
        _actor(db,actor_id,lock=True)
        existing=db.scalar(select(Job).where(Job.actor_id==actor_id,Job.destination==DESTINATION,
                                             Job.request_key_hash==key_hash))
        if existing is not None:
            if existing.body_fingerprint!=fingerprint:
                raise PublicationRequestConflict("This request key belongs to a different publication request.")
            result=public_status(existing)
            db.commit()
            return result
        if db.scalar(select(Job.id).where(Job.status.in_(ACTIVE)).limit(1)) is not None:
            raise PublicationLaneBusy("Another publication is active or requires reconciliation.")
        operation_id=uuid4()
        selected=validate_selection(operation_id,capture(str(operation_id),actor))
        now=_now(db)
        job=Job(id=operation_id,actor_id=actor_id,destination=DESTINATION,lane="global",
                request_key_hash=key_hash,body_fingerprint=fingerprint,selection_ref=selected["selection_ref"],
                selection_digest=selected["selection_digest"],selection_metadata_json=selected["metadata"],
                status="QUEUED",attempt_number=0,progress_percent=0,stage_key="queued",
                detail="Selected generation is safely queued for publication.",created_at=now,updated_at=now)
        db.add(job)
        db.flush()
        result=public_status(job)
        db.commit()
    # Deliberately outside the admission transaction. Failure leaves the durable
    # QUEUED row available to the worker's recurring dispatcher after restart.
    dispatch_one(operation_id)
    return result


def get_status(*, actor_id, operation_id=None, request_key=None):
    if operation_id is not None and request_key is not None:
        raise InvalidPublicationRequest("Choose an operation identity or a request key.")
    with SessionLocal() as db:
        actor_id=UUID(str(actor_id));_actor(db,actor_id)
        query=select(Job).where(Job.actor_id==actor_id,Job.destination==DESTINATION)
        if operation_id is not None:
            query=query.where(Job.id==UUID(str(operation_id)))
        elif request_key is not None:
            query=query.where(Job.request_key_hash==request_identity(request_key,{})[0])
        else:
            query=query.order_by(Job.created_at.desc(),Job.id.desc()).limit(1)
        job=db.scalar(query)
        if job is None:
            if operation_id is not None or request_key is not None:
                raise PublicationNotFound("Publication request was not found.")
            return None
        return public_status(job)


def blocked_status(*,actor_id):
    """Deliberate operator recovery surface, with no actor/selection payload."""
    with SessionLocal() as db:
        _admin(db,UUID(str(actor_id)))
        job=db.scalar(select(Job).where(Job.status.in_(ACTIVE)).limit(1))
        return public_status(job) if job is not None else None


def _recovery_status(operation_id,actor_id):
    with SessionLocal() as db:
        _admin(db,UUID(str(actor_id)));job=db.get(Job,operation_id)
        if job is None:raise PublicationNotFound()
        return public_status(job)


def dispatch_one(operation_id):
    """At-least-once delivery, never an authority to repeat a public effect."""
    try:
        queue=Queue("publication",connection=Redis.from_url(settings.redis_url,
                    socket_connect_timeout=3,socket_timeout=5,retry_on_timeout=False))
        queue.enqueue("app.services.publication_jobs.run_publication_job",str(operation_id),
                      job_id=f"publication-{operation_id}-{uuid4().hex}",job_timeout=3600,
                      ttl=180,result_ttl=86400,failure_ttl=604800)
        with SessionLocal() as db:
            db.execute(update(Job).where(Job.id==operation_id,Job.status.in_(ACTIVE))
                       .values(last_dispatched_at=_now(db)))
            db.commit()
        return True
    except Exception:
        # No exception text: Redis/DB URLs may contain credentials. The row is
        # the outbox and remains eligible; a queue outage cannot erase acceptance.
        logger.warning("Publication delivery is pending; durable outbox retained.")
        return False


def recover_and_dispatch():
    with SessionLocal() as db:
        now=_now(db)
        expired=db.scalars(select(Job).where(Job.status=="RUNNING",Job.lease_expires_at<=now)
                           .options(load_only(Job.id,Job.status,Job.lease_expires_at,Job.effect_started_at,
                               Job.attempt_token,Job.attempt_number,Job.stage_key,Job.detail,Job.updated_at,Job.last_dispatched_at))
                           .with_for_update(skip_locked=True).limit(10)).all()
        for job in expired:
            if job.effect_started_at is None:
                job.status="QUEUED";job.attempt_token=None;job.stage_key="queued"
                job.progress_percent=0
                job.detail="Worker restarted before effect admission; the same selected generation is queued."
            else:
                job.status="NEEDS_RECONCILIATION";job.stage_key="reconciliation"
                job.detail="Worker stopped after effect admission; destination confirmation is required."
            job.lease_expires_at=None;job.updated_at=now;job.last_dispatched_at=None
        db.flush()
        due=list(db.scalars(select(Job.id).where(Job.status.in_(("QUEUED","NEEDS_RECONCILIATION")),
            (Job.last_dispatched_at.is_(None)) | (Job.last_dispatched_at<=now-timedelta(seconds=DISPATCH_SECONDS)))
            .order_by(Job.created_at.asc()).limit(5)))
        db.commit()
    for operation_id in due:
        dispatch_one(operation_id)


def _claim(operation_id):
    with SessionLocal() as db:
        job=db.scalar(select(Job).where(Job.id==operation_id).with_for_update());now=_now(db)
        if job is None or job.status!="QUEUED":return None
        try:_actor(db,job.actor_id,lock=True)
        except PublicationAuthorityRevoked:
            job.status="FAILED";job.error_code="authority_revoked";job.finished_at=now;job.updated_at=now
            job.stage_key="failed"
            job.detail="Publication was not started because its actor is no longer active."
            db.commit();return None
        now=_now(db)
        token=uuid4();job.status="RUNNING";job.attempt_token=token;job.attempt_number+=1
        job.started_at=now;job.updated_at=now;job.lease_expires_at=now+timedelta(seconds=LEASE_SECONDS)
        job.stage_key="preparing";job.detail="Publishing the selected generation."
        result=_execution(job);db.commit();return result


def heartbeat(operation_id,token,*,progress=None):
    with SessionLocal() as db:
        job=db.scalar(select(Job).where(Job.id==operation_id).options(load_only(Job.id,Job.status,
            Job.attempt_token,Job.lease_expires_at,Job.updated_at,Job.progress_percent,Job.stage_key)).with_for_update())
        now=_now(db)
        if job is None or job.status!="RUNNING" or job.attempt_token!=token or job.lease_expires_at<=now:
            raise StalePublicationAttempt("Publication attempt no longer owns its lease.")
        job.updated_at=now;job.lease_expires_at=now+timedelta(seconds=LEASE_SECONDS)
        if progress is not None:
            value=progress.get("progress_percent",0)
            if type(value) in (int,float) and 0<=value<=99:job.progress_percent=int(value)
            stage=progress.get("stage_key")
            if isinstance(stage,str) and stage.replace("_","").isalnum():job.stage_key=stage[:64]
        db.commit()


def admit_effect(operation_id,token):
    with SessionLocal() as db:
        job=db.scalar(select(Job).where(Job.id==operation_id).options(load_only(Job.id,Job.actor_id,Job.status,
            Job.attempt_token,Job.lease_expires_at,Job.effect_started_at,Job.updated_at)).with_for_update());now=_now(db)
        if job is None or job.status!="RUNNING" or job.attempt_token!=token or job.lease_expires_at<=now:
            raise StalePublicationAttempt("Publication attempt no longer owns effect admission.")
        _actor(db,job.actor_id,lock=True)
        now=_now(db)
        if job.lease_expires_at<=now:raise StalePublicationAttempt("Publication lease expired while checking current authority.")
        if job.effect_started_at is not None:raise StalePublicationAttempt("Publication effect admission is single-use.")
        job.effect_started_at=now
        job.updated_at=now;job.lease_expires_at=now+timedelta(seconds=LEASE_SECONDS)
        db.commit()


def _succeed(operation_id,token,receipt,*,reconciliation=False):
    """Resolve a validated publication or an immutable destination no-effect fence."""
    with SessionLocal() as db:
        job=db.scalar(select(Job).where(Job.id==operation_id).with_for_update());now=_now(db)
        if job is None:raise PublicationNotFound()
        cancelled=isinstance(receipt,dict) and receipt.get("outcome")=="not_applied"
        superseded=isinstance(receipt,dict) and receipt.get("outcome")=="superseded"
        if superseded:validate_superseded_receipt(job.id,job.selection_digest,receipt,job.selection_metadata_json)
        elif cancelled:validate_cancellation_receipt(job.id,job.selection_digest,receipt)
        else:validate_receipt(job.id,job.selection_digest,receipt,job.selection_metadata_json)
        now=_now(db)
        if job.status in {"SUCCEEDED","CANCELLED","SUPERSEDED"} and job.receipt_json==receipt:return public_status(job)
        live_lease=job.lease_expires_at is not None and job.lease_expires_at>now
        allowed=(job.status=="NEEDS_RECONCILIATION" and job.reconciliation_token==token and live_lease if reconciliation else
                 job.status=="RUNNING" and job.attempt_token==token and live_lease)
        if not allowed or job.effect_started_at is None:raise StalePublicationAttempt("Publication outcome has no current effect admission.")
        job.status="SUPERSEDED" if superseded else "CANCELLED" if cancelled else "SUCCEEDED"
        job.receipt_json=receipt;job.finished_at=now;job.updated_at=now
        job.lease_expires_at=None;job.reconciliation_token=None
        job.stage_key="superseded" if superseded else "cancelled" if cancelled else "complete"
        job.progress_percent=100;job.error_code=None
        job.detail=("Generation was committed earlier; the destination now selects a different generation. No promotion was repeated." if superseded else
                    "Destination confirmed this operation cannot be applied; the publication lane is released."
                    if cancelled else "Selected generation and destination media, annotations and citations were verified.")
        result=public_status(job);db.commit();return result


def _fail_attempt(operation_id,token):
    with SessionLocal() as db:
        job=db.scalar(select(Job).where(Job.id==operation_id).with_for_update());now=_now(db)
        if job is None or job.status!="RUNNING" or job.attempt_token!=token:return
        uncertain=job.effect_started_at is not None
        job.status="NEEDS_RECONCILIATION" if uncertain else "FAILED"
        job.stage_key="reconciliation" if uncertain else "failed"
        job.error_code="destination_unconfirmed" if uncertain else "publication_not_started"
        job.detail=("Destination may have changed; receipt readback is required before another publication."
                    if uncertain else "The selected generation could not be published before any effect was admitted.")
        job.lease_expires_at=None;job.finished_at=None if uncertain else now;job.updated_at=now
        db.commit()


def _claim_reconciliation(operation_id,actor_id,*,admin_recovery=False):
    with SessionLocal() as db:
        job=db.scalar(select(Job).where(Job.id==UUID(str(operation_id))).with_for_update())
        if job is None:raise PublicationNotFound()
        if actor_id is not None:_access(db,job,UUID(str(actor_id)),admin_recovery=admin_recovery)
        now=_now(db)
        if (job.status!="NEEDS_RECONCILIATION" or
                (job.lease_expires_at is not None and job.lease_expires_at>now)):
            return None,None,public_status(job)
        if job.effect_started_at is None:raise StalePublicationAttempt("Reconciliation has no admitted effect.")
        token=uuid4();job.reconciliation_token=token;job.lease_expires_at=now+timedelta(seconds=LEASE_SECONDS)
        job.updated_at=now;job.last_dispatched_at=now
        selected=_execution(job)
        db.commit()
        return selected,token,None


def _heartbeat_reconciliation(operation_id,token):
    with SessionLocal() as db:
        job=db.scalar(select(Job).where(Job.id==operation_id).options(load_only(Job.id,Job.status,
            Job.reconciliation_token,Job.lease_expires_at,Job.updated_at)).with_for_update());now=_now(db)
        if (job is None or job.status!="NEEDS_RECONCILIATION" or job.reconciliation_token!=token
                or job.lease_expires_at is None or job.lease_expires_at<=now):
            raise StalePublicationAttempt("Reconciliation no longer owns its lease.")
        job.updated_at=now;job.lease_expires_at=now+timedelta(seconds=LEASE_SECONDS);db.commit()


def _release_reconciliation(operation_id,token):
    with SessionLocal() as db:
        job=db.scalar(select(Job).where(Job.id==operation_id).options(load_only(Job.id,Job.status,
            Job.reconciliation_token,Job.lease_expires_at,Job.updated_at)).with_for_update())
        if job is not None and job.status=="NEEDS_RECONCILIATION" and job.reconciliation_token==token:
            job.reconciliation_token=None;job.lease_expires_at=None;job.updated_at=_now(db);db.commit()


def reconcile_operation(operation_id,*,actor_id=None,transport=None,admin_recovery=False):
    operation_id=UUID(str(operation_id))
    selected,token,status=_claim_reconciliation(operation_id,actor_id,admin_recovery=admin_recovery)
    if selected is None:return status
    # Read-only confirmation can complete an already admitted commitment even
    # after its original actor was revoked. It grants no new effect authority.
    stopped=Event();lost=Event();keeper=None
    def keep_lease():
        while not stopped.wait(30):
            try:_heartbeat_reconciliation(operation_id,token)
            except Exception:lost.set();return
    try:
        keeper=Thread(target=keep_lease,name="publication-reconciliation-lease",daemon=True);keeper.start()
        if transport is None:
            from app.services.publication_transport import reconcile_selected
            transport=reconcile_selected
        receipt=transport(selected["operation_id"],selected["attempt_token"],selected["selection_ref"],
            selected["selection_digest"],selected["metadata"],
            {"type":"user","user_id":str(actor_id) if actor_id is not None else selected["actor_id"],
             "trigger":"publication.admin_recovery" if admin_recovery else "publication.reconciliation"})
        if lost.is_set():raise StalePublicationAttempt("Reconciliation lease supervision was interrupted.")
        if receipt is not None:_succeed(operation_id,token,receipt,reconciliation=True)
    except Exception:
        logger.warning("Publication reconciliation remains unconfirmed; no promotion was retried.")
    finally:
        stopped.set()
        if keeper is not None and keeper.ident is not None:keeper.join(timeout=5)
        _release_reconciliation(operation_id,token)
    if actor_id is not None:
        if admin_recovery:return _recovery_status(operation_id,actor_id)
        return get_status(actor_id=actor_id,operation_id=operation_id)
    with SessionLocal() as db:
        return public_status(db.get(Job,operation_id))


def cancel_operation(operation_id,*,actor_id,transport=None):
    """Cancel before claim, or obtain a permanent target no-effect fence.

    A running attempt cannot be cancelled through this route. Once its lease
    expires, ordinary recovery first classifies whether any effect was admitted.
    No cancellation undoes an already committed public generation.
    """
    operation_id=UUID(str(operation_id));actor_id=UUID(str(actor_id))
    cross_actor=False
    with SessionLocal() as db:
        job=db.scalar(select(Job).where(Job.id==operation_id).with_for_update())
        if job is None:raise PublicationNotFound()
        _actor(db,actor_id,lock=True);_access(db,job,actor_id,admin_recovery=True)
        cross_actor=job.actor_id!=actor_id
        if cross_actor and job.status not in ACTIVE:raise PublicationNotFound()
        if job.status=="RUNNING":raise PublicationLaneBusy("Publication is still running; check its outcome before cancelling.")
        if cross_actor and job.status in {"QUEUED","NEEDS_RECONCILIATION"}:
            append_audit_event(db,event_type="publication.admin_recovery_requested",subject_type="publication_job",
                subject_id=operation_id,actor_json={"type":"user","user_id":str(actor_id),"trigger":"publication.admin_recovery"},
                payload_json={"operation_id":str(operation_id),"action":"cancel"})
        if job.status=="QUEUED":
            now=_now(db);job.status="CANCELLED";job.stage_key="cancelled";job.updated_at=now;job.finished_at=now
            job.detail="Queued publication was cancelled before effect admission."
            result=public_status(job);db.commit();return result
        if job.status!="NEEDS_RECONCILIATION":return public_status(job)
        if cross_actor:db.commit()
    if transport is None:
        from app.services.publication_transport import cancel_selected
        transport=cancel_selected
    # The same reconciliation claim serializes readback and explicit fencing.
    return reconcile_operation(operation_id,actor_id=actor_id,transport=transport,admin_recovery=cross_actor)


def run_publication_job(operation_id,*,transport=None):
    # RQ workhorses fork. Never reuse a pool inherited from the RQ parent;
    # the separately spawned dispatcher has its own independent engine.
    engine.dispose(close=False)
    operation_id=UUID(str(operation_id))
    with SessionLocal() as db:
        job=db.get(Job,operation_id);needs=job is not None and job.status=="NEEDS_RECONCILIATION"
    if needs:return reconcile_operation(operation_id)
    selected=_claim(operation_id)
    if selected is None:return None
    token=UUID(selected["attempt_token"]);stopped=Event();lease_lost=Event()
    def keep_lease():
        while not stopped.wait(30):
            try:heartbeat(operation_id,token)
            except Exception:lease_lost.set();return
    keeper=None
    def before_effect():
        if lease_lost.is_set():raise StalePublicationAttempt("Publication lease supervision was interrupted.")
        admit_effect(operation_id,token)
    def progress(**updates):
        if lease_lost.is_set():raise StalePublicationAttempt("Publication lease supervision was interrupted.")
        heartbeat(operation_id,token,progress=updates)
    try:
        keeper=Thread(target=keep_lease,name="publication-lease",daemon=True);keeper.start()
        if transport is None:
            from app.services.publication_transport import execute_selected
            transport=execute_selected
        receipt=transport(selected["operation_id"],selected["attempt_token"],selected["selection_ref"],
            selected["selection_digest"],selected["metadata"],
            {"type":"user","user_id":selected["actor_id"],"trigger":"publication.worker"},before_effect,progress)
        return _succeed(operation_id,token,receipt)
    except Exception:
        _fail_attempt(operation_id,token)
        logger.warning("Publication worker stopped; durable outcome retained without exposing transport diagnostics.")
        return None
    finally:
        stopped.set()
        if keeper is not None and keeper.ident is not None:keeper.join(timeout=5)


def _dispatch_loop(stop_event):
    """A separately spawned process: no DB thread is present when RQ forks."""
    while not stop_event.is_set():
        try:recover_and_dispatch()
        except Exception:logger.warning("Publication outbox unavailable; retrying without discarding requests.")
        stop_event.wait(10)


def start_dispatcher(stop_event,*,context=None):
    context=context or get_context("spawn")
    if context.get_start_method()!="spawn":
        raise ValueError("Publication dispatcher requires a separate spawn process.")
    process=context.Process(target=_dispatch_loop,args=(stop_event,),name="publication-outbox",daemon=True)
    process.start()
    return process


def stop_dispatcher(process,stop_event):
    """Stop only the directly returned owned process; never an ambient worker."""
    stop_event.set();process.join(timeout=5)
    if process.is_alive():
        process.terminate();process.join(timeout=5)
    if process.is_alive():
        raise RuntimeError("Owned publication dispatcher did not stop.")
