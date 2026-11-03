"""Real PostgreSQL ledger/trigger/API tests with generated transport boundaries.

Run only against the explicit disposable CI database. These tests do not operate
the SSH destination or claim real HTTP media/readback/worker-process acceptance.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import hashlib
import importlib.util
from multiprocessing import get_context
import os
from pathlib import Path
from threading import Lock
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

os.environ.setdefault("RATE_LIMIT_ENABLED","false")


def _commit_effect_then_exit(url,schema,operation_id,pipe):
    """A real separate synthetic worker process; never a real destination effect."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.services import publication_jobs as jobs
    jobs.engine=create_engine(url,connect_args={"options":f"-csearch_path={schema},public"})
    jobs.SessionLocal=sessionmaker(bind=jobs.engine,autoflush=False)
    selected=jobs._claim(UUID(operation_id))
    jobs.admit_effect(UUID(operation_id),UUID(selected["attempt_token"]))
    pipe.send(selected["attempt_token"]);pipe.close()
    # Simulate a workhorse crash after the independently committed source fence.
    # No target script, application server, media or SSH command is executed.
    os._exit(23)


class DurablePublicationPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sqlalchemy import create_engine
        from sqlalchemy.engine import make_url
        url=os.environ.get("CI_DATABASE_URL")
        if not url:raise RuntimeError("Explicit disposable CI_DATABASE_URL is required; no database test is skipped.")
        parsed=make_url(url)
        if (parsed.get_backend_name()!="postgresql" or parsed.host not in {"127.0.0.1","localhost","::1","postgres"}
                or not parsed.database or not parsed.database.startswith("semantic_ci")):
            raise RuntimeError("Publication tests require the named local disposable semantic_ci target.")
        cls.url=parsed
        cls.admin=create_engine(parsed,pool_pre_ping=True)

    @classmethod
    def tearDownClass(cls):
        cls.admin.dispose()

    def setUp(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from sqlalchemy import create_engine,text
        from sqlalchemy.orm import sessionmaker
        from app.models.entities import AuditEvent,User
        from app.services import publication_jobs as jobs
        self.jobs=jobs;self.schema="loci_publication_test_"+uuid4().hex
        with self.admin.begin() as connection:connection.execute(text(f'CREATE SCHEMA "{self.schema}"'))
        self.engine=create_engine(self.url,connect_args={"options":f"-csearch_path={self.schema},public"},pool_pre_ping=True)
        self.addCleanup(self.cleanup_schema)
        User.__table__.create(self.engine)
        AuditEvent.__table__.create(self.engine)
        # This suite tests the additive revision, not the full application
        # migration graph. Video's new deferred FK needs its pre-existing table.
        with self.engine.begin() as connection:connection.execute(text("CREATE TABLE videos (id uuid PRIMARY KEY)"))
        path=Path(__file__).parents[1]/"alembic/versions/20261103_0037_add_durable_publication.py"
        spec=importlib.util.spec_from_file_location("_durable_publication_test_migration",path)
        self.migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.migration)
        with self.engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):self.migration.upgrade()
        self.Session=sessionmaker(bind=self.engine,autoflush=False)
        self.patches=[patch.object(jobs,"SessionLocal",self.Session),patch.object(jobs,"engine",self.engine),
                      patch.object(jobs,"dispatch_one",return_value=False)]
        for item in self.patches:item.start();self.addCleanup(item.stop)
        self.user_id=uuid4();self.other_id=uuid4();self.capture_count=0;self.capture_lock=Lock()
        with self.Session() as db:
            db.add_all([User(id=self.user_id,email=f"generated-{self.user_id}@example.invalid",password_hash="unused-generated-hash",is_active=True,is_platform_admin=False),
                        User(id=self.other_id,email=f"generated-{self.other_id}@example.invalid",password_hash="unused-generated-hash",is_active=True,is_platform_admin=False)])
            db.commit()

    def cleanup_schema(self):
        from sqlalchemy import text
        self.engine.dispose()
        with self.admin.begin() as connection:connection.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))

    def capture(self,operation_id,actor):
        from app.services.publication_job_contract import canonical
        with self.capture_lock:self.capture_count+=1
        metadata={"manifest":{"media_paths":[],"objects":[{"id":"generated","name":"GENERATED_SELECTED_CONTENT"}]},
            "media_inventory":{},"bundle_sha256":hashlib.sha256(b"").hexdigest(),"bundle_bytes":0,"destination_fingerprint":"b"*64,
            "expected_head":{"generation_id":None,"version":0}}
        return {"version":1,"generation_id":operation_id,"selection_ref":operation_id,
                "selection_digest":hashlib.sha256(canonical(metadata)).hexdigest(),"metadata":metadata}

    def admit(self,key="generated-request",actor_id=None,body=None):
        actor_id=actor_id or self.user_id
        return self.jobs.admit_request(actor_id=actor_id,request_key=key,body={} if body is None else body,
            actor={"type":"user","user_id":str(actor_id)},capture=self.capture)

    def receipt(self,operation_id):
        from app.models.publication_job import PublicationJob
        from app.services.publication_readback import expected_receipt
        with self.Session() as db:
            job=db.get(PublicationJob,UUID(str(operation_id)))
            return {**expected_receipt(job.selection_metadata_json["manifest"],{}),
                "operation_id":str(job.id),"generation_id":str(job.id),"selection_digest":job.selection_digest,
                "committed_at":"2026-01-01T00:00:00Z",
                "http_readback":{"status":"verified","resource_count":1,"verified_at":"2026-01-01T00:00:01Z"}}

    def cancellation(self,operation_id,reason="explicit_cancel"):
        receipt=self.receipt(operation_id)
        return {name:receipt[name] for name in ("version","operation_id","generation_id","selection_digest","committed_at")} | {
            "outcome":"not_applied","reason":reason}

    def superseded(self,operation_id):
        receipt=self.receipt(operation_id)
        return {name:receipt[name] for name in ("version","operation_id","generation_id","selection_digest","committed_at")} | {
            "outcome":"superseded","current_head":{"generation_id":str(uuid4()),"version":1},
            "published_receipt":{name:value for name,value in receipt.items() if name!="http_readback"}}

    def expire(self,operation_id):
        from app.models.publication_job import PublicationJob
        with self.Session() as db:
            job=db.get(PublicationJob,UUID(str(operation_id)))
            job.lease_expires_at=self.jobs._now(db)-timedelta(seconds=1);db.commit()

    def test_acceptance_commits_before_any_delivery_and_replay_never_recaptures(self):
        observed=[]
        def delivery(operation_id):
            observed.append(self.jobs.get_status(actor_id=self.user_id,operation_id=operation_id)["state"])
        with patch.object(self.jobs,"dispatch_one",side_effect=delivery):first=self.admit()
        self.assertEqual(observed,["QUEUED"])
        second=self.admit();self.assertEqual(first["operation_id"],second["operation_id"])
        self.assertEqual(self.capture_count,1)

    def test_request_body_conflict_and_global_lane_are_database_backed(self):
        from app.services.publication_job_contract import PublicationLaneBusy,PublicationRequestConflict
        self.admit()
        with self.assertRaises(PublicationRequestConflict):self.admit(body={"internal-test-identity":"different"})
        with self.assertRaises(PublicationLaneBusy):self.admit("other-key",self.other_id)
        self.assertEqual(self.capture_count,1)

    def test_two_independent_sessions_replay_one_admission(self):
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:self.admit(),range(2)))
        self.assertEqual(len({result["operation_id"] for result in results}),1)
        self.assertEqual(self.capture_count,1)

    def test_status_by_id_and_request_key_is_actor_scoped(self):
        from app.services.publication_job_contract import PublicationNotFound
        result=self.admit()
        self.assertEqual(self.jobs.get_status(actor_id=self.user_id,request_key="generated-request")["operation_id"],result["operation_id"])
        for kwargs in ({"operation_id":result["operation_id"]},{"request_key":"generated-request"}):
            with self.assertRaises(PublicationNotFound):self.jobs.get_status(actor_id=self.other_id,**kwargs)
        self.assertIsNone(self.jobs.get_status(actor_id=self.other_id))
        self.assertNotIn("GENERATED_SELECTED_CONTENT",str(result))

    def test_concurrent_worker_claim_has_one_winner(self):
        operation=UUID(self.admit()["operation_id"])
        with ThreadPoolExecutor(max_workers=2) as pool:claims=list(pool.map(lambda _:self.jobs._claim(operation),range(2)))
        self.assertEqual(sum(value is not None for value in claims),1)

    def test_revoked_actor_is_rechecked_before_claim_and_effect(self):
        from app.models.entities import User
        from app.services.publication_job_contract import PublicationAuthorityRevoked
        operation=UUID(self.admit()["operation_id"]);claim=self.jobs._claim(operation)
        with self.Session() as db:user=db.get(User,self.user_id);user.is_active=False;db.commit()
        with self.assertRaises(PublicationAuthorityRevoked):self.jobs.admit_effect(operation,UUID(claim["attempt_token"]))
        self.jobs._fail_attempt(operation,UUID(claim["attempt_token"]))
        with self.Session() as db:
            from app.models.publication_job import PublicationJob
            self.assertEqual(db.get(PublicationJob,operation).status,"FAILED")

    def test_revoked_before_claim_does_not_execute_transport(self):
        from app.models.entities import User
        operation=UUID(self.admit()["operation_id"])
        with self.Session() as db:user=db.get(User,self.user_id);user.is_active=False;db.commit()
        self.assertIsNone(self.jobs._claim(operation))
        with self.Session() as db:
            from app.models.publication_job import PublicationJob
            self.assertEqual(db.get(PublicationJob,operation).status,"FAILED")

    def test_effect_admission_is_single_use_and_success_cannot_bypass_it(self):
        from app.services.publication_job_contract import StalePublicationAttempt
        operation=UUID(self.admit()["operation_id"]);claim=self.jobs._claim(operation);token=UUID(claim["attempt_token"])
        with self.assertRaises(StalePublicationAttempt):self.jobs._succeed(operation,token,self.receipt(operation))
        self.jobs.admit_effect(operation,token)
        with self.assertRaises(StalePublicationAttempt):self.jobs.admit_effect(operation,token)

    def test_insert_policy_rejects_fabricated_running_or_terminal_history(self):
        from sqlalchemy.exc import DBAPIError
        from app.models.publication_job import PublicationJob
        for state in ("RUNNING","NEEDS_RECONCILIATION","SUCCEEDED","FAILED","CANCELLED"):
            operation=uuid4();selected=self.capture(str(operation),{})
            with self.Session() as db:
                db.add(PublicationJob(id=operation,actor_id=self.user_id,destination="configured-public",lane="global",
                    request_key_hash=uuid4().hex*2,body_fingerprint="a"*64,selection_ref=str(operation),
                    selection_digest=selected["selection_digest"],selection_metadata_json=selected["metadata"],
                    status=state,attempt_number=0,progress_percent=0,detail="Generated invalid insert"))
                with self.assertRaisesRegex(DBAPIError,"must begin queued"):db.flush()
                db.rollback()

    def test_restart_before_effect_requeues_frozen_selection_and_fences_old_token(self):
        from app.services.publication_job_contract import StalePublicationAttempt
        operation=UUID(self.admit()["operation_id"]);old=self.jobs._claim(operation)
        self.expire(operation);self.jobs.recover_and_dispatch();new=self.jobs._claim(operation)
        self.assertIsNotNone(new);self.assertNotEqual(old["attempt_token"],new["attempt_token"])
        self.assertEqual(old["selection_digest"],new["selection_digest"])
        with self.assertRaises(StalePublicationAttempt):self.jobs.admit_effect(operation,UUID(old["attempt_token"]))
        self.jobs.admit_effect(operation,UUID(new["attempt_token"]))
        self.assertEqual(self.capture_count,1)

    def test_restart_after_effect_never_requeues_promotion(self):
        operation=UUID(self.admit()["operation_id"]);claim=self.jobs._claim(operation)
        self.jobs.admit_effect(operation,UUID(claim["attempt_token"]));self.expire(operation)
        self.jobs.recover_and_dispatch()
        self.assertEqual(self.jobs.get_status(actor_id=self.user_id,operation_id=operation)["state"],"NEEDS_RECONCILIATION")
        with patch.object(self.jobs,"reconcile_operation",return_value={"state":"NEEDS_RECONCILIATION"}) as reconcile:
            self.jobs.run_publication_job(str(operation),transport=lambda *args: self.fail("Promotion must not be repeated."))
        reconcile.assert_called_once_with(operation)

    def test_actual_separate_process_crash_retains_effect_and_fences_restart(self):
        operation=UUID(self.admit()["operation_id"])
        context=get_context("spawn");read,write=context.Pipe(duplex=False)
        process=context.Process(target=_commit_effect_then_exit,args=(
            self.url.render_as_string(hide_password=False),self.schema,str(operation),write))
        process.start();write.close()
        try:
            self.assertTrue(read.poll(30),"Synthetic worker did not commit its source fence.")
            old_token=read.recv();process.join(timeout=10)
            self.assertEqual(process.exitcode,23)
        finally:
            read.close()
            if process.is_alive():process.terminate();process.join(timeout=5)
        self.expire(operation);self.jobs.recover_and_dispatch()
        self.assertEqual(self.jobs.get_status(actor_id=self.user_id,operation_id=operation)["state"],"NEEDS_RECONCILIATION")
        from app.services.publication_job_contract import StalePublicationAttempt
        with self.assertRaises(StalePublicationAttempt):self.jobs.admit_effect(operation,UUID(old_token))
        self.assertEqual(self.capture_count,1)

    def test_worker_effect_admission_is_visible_before_transport_effect_and_success_is_durable(self):
        operation=UUID(self.admit()["operation_id"]);observed=[]
        def transport(op,token,ref,digest,metadata,actor,before_effect,progress):
            before_effect()
            from app.models.publication_job import PublicationJob
            with self.Session() as db:observed.append(db.get(PublicationJob,operation).effect_started_at is not None)
            progress(stage_key="verify_destination",progress_percent=90)
            return self.receipt(op)
        self.jobs.run_publication_job(str(operation),transport=transport)
        result=self.jobs.get_status(actor_id=self.user_id,operation_id=operation)
        self.assertEqual(observed,[True]);self.assertEqual(result["state"],"SUCCEEDED")
        self.assertEqual(result["receipt"]["operation_id"],str(operation))

    def test_lost_acknowledgement_remains_uncertain_until_read_only_reconciliation(self):
        operation=UUID(self.admit()["operation_id"]);effects=[]
        def lost(*args):args[-2]();effects.append("one-generated-effect");raise RuntimeError("generated lost acknowledgement")
        self.jobs.run_publication_job(str(operation),transport=lost)
        result=self.jobs.get_status(actor_id=self.user_id,operation_id=operation)
        self.assertEqual(result["state"],"NEEDS_RECONCILIATION");self.assertIsNone(result["receipt"])
        result=self.jobs.reconcile_operation(operation,actor_id=self.user_id,transport=lambda *args:self.receipt(operation))
        self.assertEqual(result["state"],"SUCCEEDED");self.assertEqual(effects,["one-generated-effect"])

    def test_bad_receipt_cannot_turn_public_effect_into_success_or_ordinary_retry(self):
        operation=UUID(self.admit()["operation_id"])
        def transport(*args):
            args[-2]();receipt=self.receipt(operation);receipt["media_sha256"]="c"*64;return receipt
        self.jobs.run_publication_job(str(operation),transport=transport)
        self.assertEqual(self.jobs.get_status(actor_id=self.user_id,operation_id=operation)["state"],"NEEDS_RECONCILIATION")

    def test_reconciliation_claim_serializes_readback_and_fences_stale_completion(self):
        from app.services.publication_job_contract import StalePublicationAttempt
        operation=UUID(self.admit()["operation_id"]);claim=self.jobs._claim(operation)
        self.jobs.admit_effect(operation,UUID(claim["attempt_token"]));self.jobs._fail_attempt(operation,UUID(claim["attempt_token"]))
        first,token,_=self.jobs._claim_reconciliation(operation,self.user_id)
        second,other,status=self.jobs._claim_reconciliation(operation,self.user_id)
        self.assertIsNone(second);self.assertIsNone(other);self.assertEqual(status["state"],"NEEDS_RECONCILIATION")
        self.expire(operation);third,new_token,_=self.jobs._claim_reconciliation(operation,self.user_id)
        self.assertNotEqual(token,new_token)
        self.assertEqual(first["attempt_token"],third["attempt_token"])
        with self.assertRaises(StalePublicationAttempt):self.jobs._heartbeat_reconciliation(operation,token)
        with self.assertRaises(StalePublicationAttempt):self.jobs._succeed(operation,token,self.receipt(operation),reconciliation=True)
        self.jobs._release_reconciliation(operation,token)
        self.jobs._heartbeat_reconciliation(operation,new_token)

    def test_queued_cancel_is_actor_scoped_and_makes_late_queue_delivery_harmless(self):
        from app.services.publication_job_contract import PublicationNotFound
        operation=UUID(self.admit()["operation_id"])
        with self.assertRaises(PublicationNotFound):self.jobs.cancel_operation(operation,actor_id=self.other_id)
        result=self.jobs.cancel_operation(operation,actor_id=self.user_id,
            transport=lambda *args:self.fail("An unclaimed request needs no destination command."))
        self.assertEqual(result["state"],"CANCELLED");self.assertIsNone(result["receipt"])
        self.assertIsNone(self.jobs._claim(operation))
        self.assertEqual(self.admit("new-after-cancel")["state"],"QUEUED")

    def test_running_cancel_is_refused_and_uncertain_cancel_requires_target_fence(self):
        from app.services.publication_job_contract import PublicationLaneBusy
        operation=UUID(self.admit()["operation_id"]);claim=self.jobs._claim(operation)
        with self.assertRaises(PublicationLaneBusy):self.jobs.cancel_operation(operation,actor_id=self.user_id)
        self.jobs.admit_effect(operation,UUID(claim["attempt_token"]));self.jobs._fail_attempt(operation,UUID(claim["attempt_token"]))
        result=self.jobs.cancel_operation(operation,actor_id=self.user_id,transport=lambda *args:self.cancellation(operation))
        self.assertEqual(result["state"],"CANCELLED");self.assertIsNone(result["receipt"])
        self.assertEqual(result["cancellation_receipt"]["outcome"],"not_applied")
        self.assertEqual(self.admit("after-target-fence")["state"],"QUEUED")

    def test_destination_cas_rejection_never_becomes_published(self):
        operation=UUID(self.admit()["operation_id"])
        def transport(*args):args[-2]();return self.cancellation(operation,"destination_changed")
        self.jobs.run_publication_job(str(operation),transport=transport)
        result=self.jobs.get_status(actor_id=self.user_id,operation_id=operation)
        self.assertEqual(result["state"],"CANCELLED");self.assertIsNone(result["receipt"])
        self.assertEqual(result["cancellation_receipt"]["reason"],"destination_changed")

    def test_superseded_committed_generation_is_historical_not_current_success(self):
        operation=UUID(self.admit()["operation_id"]);claim=self.jobs._claim(operation)
        self.jobs.admit_effect(operation,UUID(claim["attempt_token"]));self.jobs._fail_attempt(operation,UUID(claim["attempt_token"]))
        result=self.jobs.reconcile_operation(operation,actor_id=self.user_id,transport=lambda *args:self.superseded(operation))
        self.assertEqual(result["state"],"SUPERSEDED");self.assertIsNone(result["receipt"])
        self.assertEqual(result["superseded_receipt"]["outcome"],"superseded")
        self.assertEqual(self.admit("after-superseded")["state"],"QUEUED")

    def test_invalid_cancellation_fence_keeps_lane_uncertain(self):
        operation=UUID(self.admit()["operation_id"]);claim=self.jobs._claim(operation)
        self.jobs.admit_effect(operation,UUID(claim["attempt_token"]));self.jobs._fail_attempt(operation,UUID(claim["attempt_token"]))
        receipt=self.cancellation(operation);receipt["operation_id"]=str(uuid4())
        result=self.jobs.cancel_operation(operation,actor_id=self.user_id,transport=lambda *args:receipt)
        self.assertEqual(result["state"],"NEEDS_RECONCILIATION")
        self.assertIsNone(result["cancellation_receipt"])

    def test_active_admin_can_recover_revoked_actor_without_ordinary_cross_actor_access(self):
        from sqlalchemy import select
        from app.models.entities import AuditEvent,User
        from app.services.publication_job_contract import PublicationAdminRequired,PublicationNotFound
        operation=UUID(self.admit()["operation_id"]);claim=self.jobs._claim(operation)
        self.jobs.admit_effect(operation,UUID(claim["attempt_token"]));self.jobs._fail_attempt(operation,UUID(claim["attempt_token"]))
        admin=uuid4()
        with self.Session() as db:
            owner=db.get(User,self.user_id);owner.is_active=False
            db.add(User(id=admin,email=f"generated-admin-{admin}@example.invalid",password_hash="unused-generated-hash",is_active=True,is_platform_admin=True));db.commit()
        with self.assertRaises(PublicationAdminRequired):self.jobs.blocked_status(actor_id=self.other_id)
        with self.assertRaises(PublicationNotFound):self.jobs.cancel_operation(operation,actor_id=self.other_id)
        blocked=self.jobs.blocked_status(actor_id=admin)
        self.assertEqual(blocked["operation_id"],str(operation));self.assertNotIn("GENERATED_SELECTED_CONTENT",str(blocked))
        observed=[]
        def fence(*args):
            with self.Session() as db:
                event=db.scalar(select(AuditEvent).where(AuditEvent.event_type=="publication.admin_recovery_requested"))
                self.assertIsNotNone(event);observed.append(event.actor_json["user_id"])
            self.assertEqual(args[-1]["user_id"],str(admin))
            return self.cancellation(operation)
        result=self.jobs.cancel_operation(operation,actor_id=admin,transport=fence)
        self.assertEqual(result["state"],"CANCELLED");self.assertEqual(observed,[str(admin)])
        self.assertIsNone(self.jobs.blocked_status(actor_id=admin))
        with self.assertRaises(PublicationNotFound):self.jobs.get_status(actor_id=admin,operation_id=operation)
        with self.assertRaises(PublicationNotFound):self.jobs.cancel_operation(operation,actor_id=admin)

    def test_selection_and_destination_receipts_are_immutable_and_rollback_refuses_history(self):
        from sqlalchemy import text
        from sqlalchemy.exc import DBAPIError
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from app.models.publication_job import PublicationGeneration
        operation=UUID(self.admit()["operation_id"])
        with self.engine.connect() as connection:
            transaction=connection.begin()
            try:
                with self.assertRaises(DBAPIError):connection.execute(text("UPDATE publication_jobs SET selection_digest=:digest"),{"digest":"c"*64})
            finally:transaction.rollback()
        with self.Session() as db:
            db.add(PublicationGeneration(operation_id=operation,generation_id=operation,selection_digest="a"*64,receipt_json={"generated":True}));db.commit()
        with self.engine.connect() as connection:
            transaction=connection.begin()
            try:
                with self.assertRaises(DBAPIError):connection.execute(text("DELETE FROM publication_generations"))
            finally:transaction.rollback()
        with self.engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                with self.assertRaisesRegex(RuntimeError,"Publication history exists"):self.migration.downgrade()

    def test_empty_migration_downgrade_and_reupgrade_preserve_user_table(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        with self.engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                self.migration.downgrade();self.migration.upgrade()
        self.assertEqual(self.admit()["state"],"QUEUED")

    def test_destination_head_advances_once_and_legacy_clear_cannot_replay_old_version(self):
        from sqlalchemy import text
        from sqlalchemy.exc import DBAPIError
        from app.models.publication_job import PublicationDestinationHead,PublicationGeneration
        operation=uuid4()
        with self.Session() as db:
            db.add(PublicationGeneration(operation_id=operation,generation_id=operation,selection_digest="a"*64,receipt_json={"generated":True}))
            db.flush();db.add(PublicationDestinationHead(lane="global",generation_id=operation,version=1));db.commit()
        with self.engine.connect() as connection:
            transaction=connection.begin()
            try:
                with self.assertRaises(DBAPIError):connection.execute(text("UPDATE publication_destination_head SET version=1"))
            finally:transaction.rollback()
        with self.Session() as db:
            head=db.get(PublicationDestinationHead,"global");head.generation_id=None;head.version+=1;db.commit()
        with self.Session() as db:
            head=db.get(PublicationDestinationHead,"global")
            self.assertEqual(head.version,2);self.assertIsNone(head.generation_id)

    def test_target_only_legacy_head_prevents_counter_reset_by_schema_rollback(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from app.models.publication_job import PublicationDestinationHead
        with self.Session() as db:
            db.add(PublicationDestinationHead(lane="global",generation_id=None,version=1));db.commit()
        with self.engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                with self.assertRaisesRegex(RuntimeError,"Publication history exists"):self.migration.downgrade()

    def test_real_api_lost_ack_lookup_is_authorized_and_never_runs_direct_transport(self):
        from fastapi.testclient import TestClient
        from app.core.security import create_access_token
        from app.db.session import get_db
        from app.models.entities import User
        from app.main import app
        from app.services import publication_transport
        def database():
            with self.Session() as db:yield db
        app.dependency_overrides[get_db]=database
        self.addCleanup(app.dependency_overrides.pop,get_db,None)
        headers={"Authorization":"Bearer "+create_access_token(str(self.user_id)),"Idempotency-Key":"generated-api-request"}
        other={"Authorization":"Bearer "+create_access_token(str(self.other_id)),"Idempotency-Key":"generated-api-request"}
        admin=uuid4()
        with self.Session() as db:
            db.add(User(id=admin,email=f"generated-api-admin-{admin}@example.invalid",password_hash="unused-generated-hash",is_active=True,is_platform_admin=True));db.commit()
        admin_headers={"Authorization":"Bearer "+create_access_token(str(admin))}
        with TestClient(app) as client,patch.object(publication_transport,"capture_selection",side_effect=self.capture):
            self.assertEqual(client.post("/api/v1/ops/live-database/sync/start",headers={"Idempotency-Key":"generated-api-request"}).status_code,401)
            first=client.post("/api/v1/ops/live-database/sync/start",headers=headers)
            self.assertEqual(first.status_code,202,first.text)
            # Pretend its response never arrived: the original key recovers the
            # committed operation through an authenticated GET after reload.
            lookup=client.get("/api/v1/ops/live-database/sync/status",headers=headers)
            self.assertEqual(lookup.status_code,200,lookup.text)
            self.assertEqual(lookup.json()["operation_id"],first.json()["operation_id"])
            self.assertEqual(client.get("/api/v1/ops/live-database/sync/status",headers=other).status_code,404)
            self.assertEqual(client.get("/api/v1/ops/live-database/sync/operations/"+first.json()["operation_id"],headers=other).status_code,404)
            replay=client.post("/api/v1/ops/live-database/sync",headers=headers)
            self.assertEqual(replay.status_code,202,replay.text)
            self.assertEqual(replay.json()["operation_id"],first.json()["operation_id"])
            self.assertEqual(self.capture_count,1)
            self.assertNotIn("GENERATED_SELECTED_CONTENT",lookup.text)
            bad=client.post("/api/v1/ops/live-database/sync/start",headers={**headers,"Idempotency-Key":"different"},json={"unsupported_selector":True})
            self.assertEqual(bad.status_code,400,bad.text)
            self.assertEqual(client.get("/api/v1/ops/live-database/sync/blocked",headers=headers).status_code,403)
            blocked=client.get("/api/v1/ops/live-database/sync/blocked",headers=admin_headers)
            self.assertEqual(blocked.status_code,200,blocked.text)
            self.assertEqual(blocked.json()["operation_id"],first.json()["operation_id"])
            self.assertNotIn("GENERATED_SELECTED_CONTENT",blocked.text)
            private=client.get("/api/v1/ops/live-database/sync/operations/"+first.json()["operation_id"],headers=admin_headers)
            self.assertEqual(private.status_code,404,private.text)
            cancelled=client.post("/api/v1/ops/live-database/sync/"+first.json()["operation_id"]+"/cancel",headers=admin_headers)
            self.assertEqual(cancelled.status_code,200,cancelled.text)
            self.assertEqual(cancelled.json()["state"],"CANCELLED")


if __name__=="__main__":unittest.main()
