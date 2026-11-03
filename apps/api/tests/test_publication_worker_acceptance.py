"""Real disposable PostgreSQL/Redis and a separately spawned RQ worker.

This acceptance slice exercises API admission, the actual outbox dispatcher,
Redis delivery, child-process claims/current authority and crash recovery. The
queue name alone is redirected to a unique owned queue. Selection capture and
the execute/reconcile transport boundary are generated callbacks: they write
only this test's event table, never SSH, media or a public destination. No test
here establishes destination/HTTP acceptance or production CLI/fork-workhorse
behavior. Missing explicit disposable services is an error, never a skip.
"""
from __future__ import annotations

from contextlib import ExitStack
from datetime import timedelta
import hashlib
import importlib.util
from multiprocessing import get_context
import os
from pathlib import Path
import re
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import UUID, uuid4

os.environ.setdefault("RATE_LIMIT_ENABLED", "false")


SCHEMA_PATTERN = re.compile(r"loci_pub_accept_[a-f0-9]{32}")
QUEUE_PATTERN = re.compile(r"loci-publication-acceptance-[a-f0-9]{32}")
WORKER_TIMEOUT_SECONDS = 25


def _require_disposable_services(environ=None):
    """Strict subset of scripts/ci/disposable_targets.py, API-copy compatible."""
    env = os.environ if environ is None else environ
    if env.get("SEMANTIC_ENV", "").lower() in {"prod", "production", "public"}:
        raise RuntimeError("Publication acceptance refuses production mode.")
    database = env.get("CI_DATABASE_URL", "")
    redis_url = env.get("CI_REDIS_URL", "")
    try:
        pg, redis = urlsplit(database), urlsplit(redis_url)
        local = {"127.0.0.1", "localhost", "::1"}
        pg_hosts = local | ({"postgres"} if env.get("CI_DISPOSABLE_SERVICES") == "true" else set())
        redis_hosts = local | ({"redis"} if env.get("CI_DISPOSABLE_SERVICES") == "true" else set())
        valid = (
            pg.scheme in {"postgresql", "postgresql+psycopg"}
            and pg.hostname in pg_hosts and bool(pg.username)
            and pg.port in {None, 5432} and pg.path == "/semantic_ci"
            and not pg.query and not pg.fragment
            and redis.scheme == "redis" and redis.hostname in redis_hosts
            and redis.port in {None, 6379} and redis.path == "/15"
            and not redis.query and not redis.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise RuntimeError("Explicit local disposable CI_DATABASE_URL semantic_ci and CI_REDIS_URL database15 are required.")
    if env.get("DATABASE_URL") != database or env.get("REDIS_URL") != redis_url:
        raise RuntimeError("Application DATABASE_URL/REDIS_URL must exactly match the guarded disposable CI targets.")
    if env.get("EMBEDDING_WARMUP_ENABLED", "").lower() != "false":
        raise RuntimeError("Publication acceptance requires embedding warmup disabled.")
    return database, redis_url


def _schema_engine(database_url, schema):
    from sqlalchemy import create_engine

    if SCHEMA_PATTERN.fullmatch(schema) is None:
        raise ValueError("Only the generated acceptance schema is permitted.")
    return create_engine(database_url, connect_args={
        "connect_timeout": 5,
        "options": f"-csearch_path={schema},public -cstatement_timeout=5000 -clock_timeout=5000",
    }, pool_pre_ping=True)


def _rq_worker_process(database_url, redis_url, schema, queue_name, worker_name, mode):
    """Run one real RQ delivery in this directly owned, bounded child process."""
    _require_disposable_services({**os.environ, "CI_DATABASE_URL": database_url, "CI_REDIS_URL": redis_url})
    if (QUEUE_PATTERN.fullmatch(queue_name) is None
            or re.fullmatch(re.escape(queue_name) + r"-worker-[a-f0-9]{32}", worker_name) is None):
        raise ValueError("The worker requires its generated owned queue/name.")
    if mode not in {"uncertain", "reconcile_only", "revoke_before_effect", "crash_before_effect", "crash_after_effect"}:
        raise ValueError("Unknown generated transport boundary.")

    from redis import Redis
    from rq import Queue, SimpleWorker
    from sqlalchemy import text
    from sqlalchemy.orm import sessionmaker
    from app.models.entities import User
    from app.models.publication_job import PublicationJob
    from app.services import publication_jobs as jobs, publication_transport

    # No inherited SQLAlchemy pool is used by the independently spawned worker.
    engine = _schema_engine(database_url, schema)
    jobs.engine = engine
    jobs.SessionLocal = sessionmaker(bind=engine, autoflush=False)
    connection = Redis.from_url(redis_url, socket_connect_timeout=3, socket_timeout=5, retry_on_timeout=False)

    def event(operation, kind, selected_digest, expected_effect):
        with jobs.SessionLocal() as db:
            row = db.get(PublicationJob, UUID(operation))
            if row is None or (row.effect_started_at is not None) is not expected_effect:
                raise AssertionError("The real source effect fence does not match the generated boundary.")
            if row.selection_digest != selected_digest:
                raise AssertionError("The child did not receive the admitted immutable selection.")
            db.execute(text("INSERT INTO worker_acceptance_events (operation_id,kind,worker_pid,effect_seen,selection_digest) "
                            "VALUES (:op,:kind,:pid,:effect,:digest)"),
                       {"op": UUID(operation), "kind": kind, "pid": os.getpid(),
                        "effect": expected_effect, "digest": selected_digest})
            db.commit()

    def execute(operation, token, reference, selected_digest, metadata, actor, before_effect, progress):
        if reference != operation or actor["trigger"] != "publication.worker":
            raise AssertionError("The RQ child did not receive the real selected operation contract.")
        if mode == "reconcile_only":
            raise AssertionError("An uncertain restart must never execute a new publication effect.")
        if mode == "revoke_before_effect":
            with jobs.SessionLocal() as db:
                user = db.get(User, UUID(actor["user_id"]))
                user.is_active = False
                db.commit()
            # This must raise from the real current-authority database check.
            before_effect()
            raise AssertionError("Revoked authority admitted an effect.")
        if mode == "crash_before_effect":
            event(operation, "crash_before_effect", selected_digest, False)
            os._exit(23)  # Exact generated child only; preserve the real running row.
        before_effect()
        event(operation, "generated_target_boundary", selected_digest, True)
        if mode == "crash_after_effect":
            os._exit(23)
        # No positive receipt is fabricated. The actual worker must retain an
        # uncertain outcome after its real source fence, not claim publication.
        raise RuntimeError("Generated target acknowledgement deliberately unavailable.")

    def reconcile(operation, token, reference, selected_digest, metadata, actor):
        event(operation, "readback_only", selected_digest, True)
        return None  # An injected read-only boundary, never an HTTP receipt.

    try:
        with patch.object(publication_transport, "execute_selected", execute), \
                patch.object(publication_transport, "reconcile_selected", reconcile):
            worker = SimpleWorker([Queue(queue_name, connection=connection)], connection=connection,
                                  name=worker_name, maintenance_interval=3600)
            worker.work(burst=True, max_jobs=1, with_scheduler=False, logging_level="ERROR")
    finally:
        engine.dispose()
        connection.close()
        connection.connection_pool.disconnect()


class PublicationWorkerAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database_url, cls.redis_url = _require_disposable_services()
        from sqlalchemy import create_engine
        cls.admin = create_engine(cls.database_url, connect_args={"connect_timeout": 5}, pool_pre_ping=True)

    @classmethod
    def tearDownClass(cls):
        cls.admin.dispose()

    def setUp(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from fastapi.testclient import TestClient
        from redis import Redis
        from rq import Queue
        from sqlalchemy import text
        from sqlalchemy.orm import sessionmaker
        from app.core.config import settings
        from app.core.security import create_access_token
        from app.db.session import get_db
        from app.main import app
        from app.models.entities import AuditEvent, User
        from app.services import publication_jobs as jobs, publication_transport

        self.schema = "loci_pub_accept_" + uuid4().hex
        self.queue_name = "loci-publication-acceptance-" + uuid4().hex
        self.operations = set()
        self.job_ids = set()
        self.workers = []
        self.delivery_connections = []
        self.stack = ExitStack()
        self.client = self.connection = self.engine = self.queue = None
        self.original_queue_members = set()
        self.queue_owned = False
        self.previous_dependency = app.dependency_overrides.get(get_db)
        self.app, self.get_db, self.jobs = app, get_db, jobs
        with self.admin.begin() as db:
            db.execute(text(f'CREATE SCHEMA "{self.schema}"'))
        self.addCleanup(self._cleanup_owned)
        self.engine = _schema_engine(self.database_url, self.schema)
        self.Session = sessionmaker(bind=self.engine, autoflush=False)
        User.__table__.create(self.engine)
        AuditEvent.__table__.create(self.engine)
        with self.engine.begin() as db:
            db.execute(text("CREATE TABLE videos (id uuid PRIMARY KEY)"))
            db.execute(text("CREATE TABLE worker_acceptance_events ("
                            "id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, operation_id uuid NOT NULL, "
                            "kind text NOT NULL, worker_pid integer NOT NULL, effect_seen boolean NOT NULL, "
                            "selection_digest varchar(64) NOT NULL)"))
        migration_path = Path(__file__).parents[1] / "alembic/versions/20261103_0037_add_durable_publication.py"
        spec = importlib.util.spec_from_file_location("_worker_acceptance_migration", migration_path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with self.engine.begin() as db:
            with Operations.context(MigrationContext.configure(db)):
                migration.upgrade()

        self.connection = Redis.from_url(self.redis_url, socket_connect_timeout=3, socket_timeout=5,
                                         retry_on_timeout=False)
        self.connection.ping()
        self.queue = Queue(self.queue_name, connection=self.connection)
        self.original_queue_members = self.connection.smembers("rq:queues")
        self.assertFalse(self.connection.exists(self.queue.key))
        self.assertNotIn(self.queue.key.encode(), self.original_queue_members)
        self.queue_owned = True
        self.actor_id, self.other_id = uuid4(), uuid4()
        with self.Session() as db:
            db.add_all([User(id=ident, email=f"generated-{ident}@example.invalid", password_hash="unused-generated-hash",
                             is_active=True, is_platform_admin=False) for ident in (self.actor_id, self.other_id)])
            db.commit()
        self.capture_count = 0

        def generated_selection(operation_id, actor):
            from app.services.publication_job_contract import canonical

            self.capture_count += 1
            self.operations.add(operation_id)
            metadata = {"manifest": {"media_paths": [], "objects": [
                {"id": str(uuid4()), "name": "GENERATED_IMMUTABLE_WORKER_SELECTION"}]},
                "media_inventory": {}, "bundle_sha256": hashlib.sha256(b"").hexdigest(), "bundle_bytes": 0,
                "destination_fingerprint": "b" * 64, "expected_head": {"generation_id": None, "version": 0}}
            return {"version": 1, "generation_id": operation_id, "selection_ref": operation_id,
                    "selection_digest": hashlib.sha256(canonical(metadata)).hexdigest(), "metadata": metadata}

        def isolated_real_queue(name, **kwargs):
            # The single routing injection keeps all RQ messages in this owned
            # queue. The dispatcher, real Redis calls and RQ enqueue are intact.
            self.assertEqual(name, "publication")
            self.delivery_connections.append(kwargs["connection"])
            return Queue(self.queue_name, **kwargs)

        patches = [patch.object(jobs, "SessionLocal", self.Session), patch.object(jobs, "engine", self.engine),
                        patch.object(jobs, "Queue", isolated_real_queue), patch.object(settings, "redis_url", self.redis_url),
                        patch.object(publication_transport, "capture_selection", generated_selection)]
        for item in patches:
            self.stack.enter_context(item)
        def database():
            with self.Session() as db:
                yield db
        def restore_dependency():
            if self.previous_dependency is None:
                app.dependency_overrides.pop(get_db, None)
            else:
                app.dependency_overrides[get_db] = self.previous_dependency
        self.stack.callback(restore_dependency)
        app.dependency_overrides[get_db] = database
        self.headers = {"Authorization": "Bearer " + create_access_token(str(self.actor_id)),
                        "Idempotency-Key": "generated-worker-request"}
        self.other_headers = {"Authorization": "Bearer " + create_access_token(str(self.other_id)),
                              "Idempotency-Key": "generated-worker-request"}
        self.client = self.stack.enter_context(TestClient(app))

    def _remember_deliveries(self, *, require_nonempty=True):
        from rq.job import Job

        ids = self.queue.job_ids
        if require_nonempty:
            self.assertTrue(ids, "The actual dispatcher did not deliver a Redis message.")
        for job_id in ids:
            delivered = Job.fetch(job_id, connection=self.connection)
            self.assertEqual(delivered.origin, self.queue_name)
            self.assertEqual(delivered.func_name, "app.services.publication_jobs.run_publication_job")
            self.assertEqual(len(delivered.args), 1)
            self.assertIn(delivered.args[0], self.operations)
            self.assertTrue(job_id.startswith("publication-" + delivered.args[0] + "-"))
            self.job_ids.add(job_id)
        return ids

    def _admit(self):
        response = self.client.post("/api/v1/ops/live-database/sync/start", headers=self.headers)
        self.assertEqual(response.status_code, 202, response.text)
        payload = response.json()
        self.assertEqual(payload["state"], "QUEUED")
        self._remember_deliveries()
        return UUID(payload["operation_id"])

    def _row(self, operation):
        from app.models.publication_job import PublicationJob

        with self.Session() as db:
            row = db.get(PublicationJob, operation)
            return {name: getattr(row, name) for name in ("status", "attempt_number", "attempt_token",
                "effect_started_at", "selection_digest", "selection_metadata_json", "receipt_json", "error_code")}

    def _rq_status(self, job_id):
        from rq.job import Job

        value = Job.fetch(job_id, connection=self.connection).get_status(refresh=True)
        return getattr(value, "value", value)

    def _events(self, operation):
        from sqlalchemy import text

        with self.engine.connect() as db:
            return [dict(row) for row in db.execute(text("SELECT kind,worker_pid,effect_seen,selection_digest "
                "FROM worker_acceptance_events WHERE operation_id=:op ORDER BY id"), {"op": operation}).mappings()]

    def _run_worker(self, mode, expected_exit=0):
        worker_name = self.queue_name + "-worker-" + uuid4().hex
        process = get_context("spawn").Process(target=_rq_worker_process,
            args=(self.database_url, self.redis_url, self.schema, self.queue_name, worker_name, mode))
        process.start()
        self.workers.append((process, worker_name))
        process.join(timeout=WORKER_TIMEOUT_SECONDS)
        self.assertFalse(process.is_alive(), "Owned RQ worker exceeded its bounded lifetime.")
        self.assertEqual(process.exitcode, expected_exit)
        return process.pid

    def _expire_and_dispatch(self, operation):
        from app.models.publication_job import PublicationJob

        with self.Session() as db:
            row = db.get(PublicationJob, operation)
            row.lease_expires_at = self.jobs._now(db) - timedelta(seconds=1)
            db.commit()
        self.jobs.recover_and_dispatch()  # Actual repository transition and Redis delivery.
        return self._remember_deliveries()

    def _cleanup_owned(self):
        # Direct returned handles only; SimpleWorker creates no fork-workhorse.
        for process, _name in self.workers:
            if process.is_alive():
                process.terminate()
                process.join(timeout=3)
            if process.is_alive():
                process.kill()
                process.join(timeout=3)
            self.assertFalse(process.is_alive(), "Owned worker cleanup did not complete.")
            process.close()
        try:
            self.stack.close()
        finally:
            self._cleanup_redis_and_schema()

    def _cleanup_redis_and_schema(self):
        from rq.exceptions import NoSuchJobError
        from rq.job import Job
        from rq.registry import CanceledJobRegistry, DeferredJobRegistry, FailedJobRegistry, FinishedJobRegistry, ScheduledJobRegistry, StartedJobRegistry
        from sqlalchemy import text

        try:
            if self.connection is not None and self.queue is not None and self.queue_owned:
                self._remember_deliveries(require_nonempty=False)
                # Every job ID was observed in the unique owned queue before
                # starting a child. Never flush DB15 or delete a shared registry.
                for job_id in self.job_ids:
                    try:
                        job = Job.fetch(job_id, connection=self.connection)
                    except NoSuchJobError:
                        continue
                    self.assertEqual(job.origin, self.queue_name)
                    job.delete(delete_dependents=False)
                    self.connection.delete("rq:results:" + job_id)
                for _process, name in self.workers:
                    key = "rq:worker:" + name
                    self.connection.srem("rq:workers", key)
                    self.connection.srem("rq:workers:" + self.queue_name, key)
                    self.connection.delete(key)
                keys = [registry(self.queue_name, connection=self.connection).key for registry in (
                    CanceledJobRegistry, DeferredJobRegistry, FailedJobRegistry, FinishedJobRegistry,
                    ScheduledJobRegistry, StartedJobRegistry)]
                self.connection.delete(*keys, "rq:workers:" + self.queue_name, self.queue.key + ":intermediate")
                self.queue.delete(delete_jobs=False)
                self.assertTrue(self.original_queue_members <= self.connection.smembers("rq:queues"))
        finally:
            for connection in [self.connection, *self.delivery_connections]:
                if connection is not None:
                    connection.close()
                    connection.connection_pool.disconnect()
            if self.engine is not None:
                self.engine.dispose()
            if SCHEMA_PATTERN.fullmatch(self.schema) is None:
                raise RuntimeError("Refusing cleanup of an unowned schema.")
            with self.admin.begin() as db:
                db.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))

    def test_api_admission_real_redis_child_claim_and_uncertain_read_only_restart(self):
        self.assertEqual(self.client.post("/api/v1/ops/live-database/sync/start",
            headers={"Idempotency-Key": "generated-worker-request"}).status_code, 401)
        operation = self._admit()
        selected = self._row(operation)
        queued = self._remember_deliveries()
        replay = self.client.post("/api/v1/ops/live-database/sync/start", headers=self.headers)
        self.assertEqual(replay.status_code, 202)
        self.assertEqual(replay.json()["operation_id"], str(operation))
        self.assertEqual(self.capture_count, 1)
        self.assertEqual(self.queue.job_ids, queued)
        self.assertEqual(self.client.get("/api/v1/ops/live-database/sync/status", headers=self.other_headers).status_code, 404)

        child_pid = self._run_worker("uncertain")
        self.assertEqual(self._rq_status(queued[0]), "finished")
        self.assertTrue(self.queue.is_empty())
        # Queue delivery finished; publication remains explicitly unconfirmed.
        uncertain = self._row(operation)
        self.assertEqual(uncertain["status"], "NEEDS_RECONCILIATION")
        self.assertIsNotNone(uncertain["effect_started_at"])
        self.assertIsNone(uncertain["receipt_json"])
        self.assertEqual(uncertain["attempt_number"], 1)
        self.assertEqual(uncertain["selection_metadata_json"], selected["selection_metadata_json"])
        events = self._events(operation)
        self.assertEqual([event["kind"] for event in events], ["generated_target_boundary"])
        self.assertEqual(events[0]["worker_pid"], child_pid)
        self.assertNotEqual(child_pid, os.getpid())
        with self.Session() as db:
            from app.models.publication_job import PublicationJob
            row = db.get(PublicationJob, operation)
            row.last_dispatched_at = None
            db.commit()
        self.jobs.recover_and_dispatch()
        self._remember_deliveries()
        restarted_pid = self._run_worker("reconcile_only")
        self.assertNotEqual(restarted_pid, child_pid)
        self.assertEqual(self._row(operation)["status"], "NEEDS_RECONCILIATION")
        self.assertEqual(self._row(operation)["attempt_token"], uncertain["attempt_token"])
        self.assertEqual([event["kind"] for event in self._events(operation)],
                         ["generated_target_boundary", "readback_only"])

    def test_current_authority_revoked_before_real_queue_claim_blocks_transport(self):
        from app.models.entities import User

        operation = self._admit()
        with self.Session() as db:
            db.get(User, self.actor_id).is_active = False
            db.commit()
        self._run_worker("uncertain")
        result = self._row(operation)
        self.assertEqual((result["status"], result["error_code"]), ("FAILED", "authority_revoked"))
        self.assertIsNone(result["effect_started_at"])
        self.assertEqual(self._events(operation), [])

    def test_current_authority_revoked_in_child_after_claim_blocks_effect_fence(self):
        operation = self._admit()
        self._run_worker("revoke_before_effect")
        result = self._row(operation)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["attempt_number"], 1)
        self.assertIsNone(result["effect_started_at"])
        self.assertEqual(self._events(operation), [])

    def test_actual_child_crash_before_effect_requeues_same_selection_via_redis(self):
        operation = self._admit()
        selected = self._row(operation)
        self._run_worker("crash_before_effect", expected_exit=23)
        running = self._row(operation)
        self.assertEqual(running["status"], "RUNNING")
        self.assertIsNone(running["effect_started_at"])
        self._expire_and_dispatch(operation)
        self.assertEqual(self._row(operation)["status"], "QUEUED")
        self._run_worker("uncertain")
        recovered = self._row(operation)
        self.assertEqual(recovered["status"], "NEEDS_RECONCILIATION")
        self.assertEqual(recovered["attempt_number"], 2)
        self.assertNotEqual(recovered["attempt_token"], running["attempt_token"])
        self.assertEqual(recovered["selection_digest"], selected["selection_digest"])
        self.assertEqual(recovered["selection_metadata_json"], selected["selection_metadata_json"])
        self.assertEqual([event["kind"] for event in self._events(operation)],
                         ["crash_before_effect", "generated_target_boundary"])

    def test_actual_child_crash_after_effect_only_dispatches_readback_after_restart(self):
        operation = self._admit()
        self._run_worker("crash_after_effect", expected_exit=23)
        running = self._row(operation)
        self.assertEqual(running["status"], "RUNNING")
        self.assertIsNotNone(running["effect_started_at"])
        self._expire_and_dispatch(operation)
        self.assertEqual(self._row(operation)["status"], "NEEDS_RECONCILIATION")
        self._run_worker("reconcile_only")
        recovered = self._row(operation)
        self.assertEqual(recovered["status"], "NEEDS_RECONCILIATION")
        self.assertEqual(recovered["attempt_number"], 1)
        self.assertEqual(recovered["attempt_token"], running["attempt_token"])
        self.assertEqual([event["kind"] for event in self._events(operation)],
                         ["generated_target_boundary", "readback_only"])


class QueueCleanupOwnershipTests(unittest.TestCase):
    def test_unadmitted_queue_is_never_deleted_after_preflight_collision(self):
        from contextlib import contextmanager
        from types import SimpleNamespace

        calls = []

        class Connection:
            connection_pool = SimpleNamespace(disconnect=lambda: calls.append("disconnect"))

            def close(self):
                calls.append("close")

            def __getattr__(self, name):
                raise AssertionError("Unowned Redis metadata must remain untouched: " + name)

        class Admin:
            @contextmanager
            def begin(self):
                yield SimpleNamespace(execute=lambda statement: calls.append(str(statement)))

        # Reproduce a handle assigned before absence/membership checks failed.
        # No TestCase setup, application, worker or service is started here.
        case = PublicationWorkerAcceptanceTests(methodName="test_current_authority_revoked_before_real_queue_claim_blocks_transport")
        case.queue_owned = False
        case.schema = "loci_pub_accept_" + "a" * 32
        case.queue_name = "loci-publication-acceptance-" + "b" * 32
        case.queue = SimpleNamespace(key="rq:queue:" + case.queue_name, job_ids=[])
        case.connection = Connection()
        case.delivery_connections = []
        case.job_ids = set()
        case.workers = []
        case.operations = set()
        case.original_queue_members = set()
        case.engine = SimpleNamespace(dispose=lambda: calls.append("dispose"))
        case.admin = Admin()
        case._cleanup_redis_and_schema()
        self.assertEqual(calls, ["close", "disconnect", "dispose", f'DROP SCHEMA "{case.schema}" CASCADE'])
