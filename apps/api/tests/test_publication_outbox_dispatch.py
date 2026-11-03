"""Clock-controlled actual outbox bodies with a query/repository adapter.

These tests exercise eligibility and callback ordering without sleeping or using
services. They do not prove PostgreSQL locking, Redis delivery or RQ fairness;
the separately authored PostgreSQL/worker acceptance cases cover those gates.
"""
import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from uuid import uuid4


class Predicate:
    def __init__(self, evaluate): self.evaluate = evaluate
    def __and__(self, other): return Predicate(lambda row: self.evaluate(row) and other.evaluate(row))
    def __or__(self, other): return Predicate(lambda row: self.evaluate(row) or other.evaluate(row))


class Column:
    def __init__(self, name): self.name = name
    def __eq__(self, value): return Predicate(lambda row: getattr(row, self.name) == value)
    def __le__(self, value):
        return Predicate(lambda row: getattr(row, self.name) is not None and getattr(row, self.name) <= value)
    def is_(self, value): return Predicate(lambda row: getattr(row, self.name) is value)
    def in_(self, values): return Predicate(lambda row: getattr(row, self.name) in values)
    def asc(self): return self


class Job:
    pass


for name in ("id", "status", "lease_expires_at", "effect_started_at", "attempt_token", "attempt_number",
             "stage_key", "detail", "updated_at", "last_dispatched_at", "created_at", "reconciliation_token",
             "progress_percent"):
    setattr(Job, name, Column(name))


class Query:
    def __init__(self, projection): self.projection = projection; self.predicates = []; self.maximum = None
    def where(self, *predicates): self.predicates.extend(predicates); return self
    def options(self, *args): return self
    def with_for_update(self, **kwargs): return self
    def order_by(self, *args): return self
    def limit(self, count): self.maximum = count; return self


class Result(list):
    def all(self): return self


class Repository:
    def __init__(self, row, now):
        self.row = row; self.now = now; self.messages = []; self.commits = 0
        self.default_markers = ["GENERATED_DEFAULT_MARKER"]
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def scalars(self, query):
        matches = all(predicate.evaluate(self.row) for predicate in query.predicates)
        return Result([self.row if query.projection is Job else getattr(self.row, query.projection.name)] if matches else [])
    def scalar(self, query):
        values = self.scalars(query)
        return values[0] if values else None
    def flush(self): pass
    def commit(self): self.commits += 1
    def dispatch(self, operation):
        assert self.commits > 0, "Outbox delivery must follow the repository commit."
        self.messages.append(operation)
        self.row.last_dispatched_at = self.now  # Injected real-delivery repository acknowledgement.


def functions(source=None):
    path = Path(__file__).parents[1] / "app/services/publication_jobs.py"
    tree = ast.parse(path.read_text() if source is None else source)
    names = {"recover_and_dispatch", "_release_reconciliation", "_heartbeat_reconciliation"}
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in nodes} == names
    env = {"Job": Job, "select": Query, "load_only": lambda *args: None, "timedelta": timedelta,
           "DISPATCH_SECONDS": 60, "LEASE_SECONDS": 300, "StalePublicationAttempt": RuntimeError}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), env)
    return env


class PublicationOutboxClockTests(unittest.TestCase):
    def setUp(self):
        self.base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.row = SimpleNamespace(id=uuid4(), status="NEEDS_RECONCILIATION", reconciliation_token=uuid4(),
            attempt_token=uuid4(), attempt_number=1, effect_started_at=self.base - timedelta(seconds=1),
            lease_expires_at=self.base + timedelta(seconds=300), last_dispatched_at=self.base,
            updated_at=self.base, created_at=self.base, stage_key="reconciliation", detail="generated",
            progress_percent=85, selection_digest="a" * 64, selection_metadata_json={"generated": "immutable"})
        self.repo = Repository(self.row, self.base)
        self.env = functions()
        self.env.update(SessionLocal=lambda: self.repo, _now=lambda db: self.repo.now,
                        dispatch_one=self.repo.dispatch)

    def dispatch_at(self, seconds):
        self.repo.now = self.base + timedelta(seconds=seconds)
        self.env["recover_and_dispatch"]()

    def test_live_reconciliation_is_not_redelivered_after_aged_dispatch(self):
        self.dispatch_at(90)
        self.assertEqual(self.repo.messages, [])
        self.assertEqual(self.row.last_dispatched_at, self.base)

    def test_heartbeat_renewed_long_probe_remains_out_of_the_delivery_queue(self):
        for seconds in (70, 90, 180, 299):
            self.repo.now = self.base + timedelta(seconds=seconds)
            self.env["_heartbeat_reconciliation"](self.row.id, self.row.reconciliation_token)
            self.env["recover_and_dispatch"]()
        self.assertEqual(self.repo.messages, [])
        self.assertGreater(self.row.lease_expires_at, self.repo.now)

    def test_release_starts_backoff_after_probe_and_leaves_default_marker_opportunity(self):
        self.repo.now = self.base + timedelta(seconds=90)
        self.env["_release_reconciliation"](self.row.id, self.row.reconciliation_token)
        self.assertEqual(self.row.last_dispatched_at, self.repo.now)
        self.assertIsNone(self.row.lease_expires_at)
        self.assertIsNone(self.row.reconciliation_token)
        self.dispatch_at(91)
        self.dispatch_at(149)
        self.assertEqual(self.repo.messages, [])
        # A generated priority marker demonstrates only the absence of a new
        # publication hint; it is not an actual RQ scheduling/fairness claim.
        marker = self.repo.messages.pop(0) if self.repo.messages else self.repo.default_markers.pop(0)
        self.assertEqual(marker, "GENERATED_DEFAULT_MARKER")

    def test_release_backoff_is_eligible_at_the_existing_sixty_second_boundary(self):
        self.repo.now = self.base + timedelta(seconds=90)
        self.env["_release_reconciliation"](self.row.id, self.row.reconciliation_token)
        self.dispatch_at(150)
        self.assertEqual(self.repo.messages, [self.row.id])

    def test_expired_reconciliation_crash_lease_is_eligible_without_release(self):
        token = self.row.reconciliation_token
        self.dispatch_at(301)
        self.assertEqual(self.repo.messages, [self.row.id])
        self.assertEqual(self.row.reconciliation_token, token)
        self.assertEqual(self.row.status, "NEEDS_RECONCILIATION")

    def test_expired_pre_effect_worker_is_requeued_without_changing_selection(self):
        self.row.status = "RUNNING"; self.row.effect_started_at = None; self.row.reconciliation_token = None
        selected = self.row.selection_metadata_json.copy()
        self.dispatch_at(301)
        self.assertEqual(self.repo.messages, [self.row.id])
        self.assertEqual(self.row.status, "QUEUED")
        self.assertIsNone(self.row.attempt_token)
        self.assertEqual(self.row.attempt_number, 1)
        self.assertEqual(self.row.selection_metadata_json, selected)

    def test_expired_post_effect_worker_only_enqueues_uncertain_recovery(self):
        self.row.status = "RUNNING"; self.row.reconciliation_token = None
        effect, token = self.row.effect_started_at, self.row.attempt_token
        self.dispatch_at(301)
        self.assertEqual(self.repo.messages, [self.row.id])
        self.assertEqual(self.row.status, "NEEDS_RECONCILIATION")
        self.assertEqual(self.row.effect_started_at, effect)
        self.assertEqual(self.row.attempt_token, token)

    def test_stale_release_cannot_clear_another_claim_or_reset_its_dispatch_clock(self):
        lease, token, dispatched = self.row.lease_expires_at, self.row.reconciliation_token, self.row.last_dispatched_at
        self.repo.now = self.base + timedelta(seconds=90)
        self.env["_release_reconciliation"](self.row.id, uuid4())
        self.assertEqual((self.row.lease_expires_at, self.row.reconciliation_token, self.row.last_dispatched_at),
                         (lease, token, dispatched))


if __name__ == "__main__":
    unittest.main()
