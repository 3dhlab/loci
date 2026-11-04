"""Fresh-process FK closure for standalone scripts and worker imports.

Requires the installed API dependencies, but no database, Redis, encoder,
daemon or application server. Fresh interpreters prevent another test's
canonical registry import from hiding missing declaration dependencies.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


API = Path(__file__).resolve().parents[1]
CHECK = r'''
import importlib
import json
import socket
import subprocess
import sys
from unittest.mock import patch

from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Engine
from sqlalchemy.schema import CreateTable

def forbidden(*args, **kwargs):
    raise AssertionError("Standalone metadata import attempted a service or external process.")

modules = json.loads(sys.argv[1])
with patch.object(Engine, "connect", forbidden), patch.object(Engine, "raw_connection", forbidden), \
     patch.object(socket.socket, "connect", forbidden), patch.object(socket, "create_connection", forbidden), \
     patch.object(subprocess, "Popen", forbidden):
    for name in modules:
        importlib.import_module(name)
    # Importing these after the entrypoint does not register the missing target:
    # entities is already loaded, and session only defines the Base/engine.
    from app.db.session import Base
    from app.models.entities import Video
    if modules == ["app.scripts.seed_demo"]:
        assert "app.db.base" not in sys.modules, "Seed import was masked by a prior canonical-registry import."
    tables = Base.metadata.tables
    assert "publication_generations" in tables
    target = tables["publication_generations"].c.generation_id
    foreign_keys = list(Video.__table__.c.publication_generation_id.foreign_keys)
    assert len(foreign_keys) == 1 and foreign_keys[0].column is target
    assert foreign_keys[0].deferrable is True and foreign_keys[0].initially == "DEFERRED"
    # Resolve EVERY registered FK, including the publication Job's reverse
    # dependency on users, and exercise the metadata ordering used by flush/DDL.
    for table in tables.values():
        for foreign_key in table.foreign_keys:
            assert foreign_key.column.table.metadata is Base.metadata
    ordered = [table.name for table in Base.metadata.sorted_tables]
    assert ordered.index("publication_generations") < ordered.index("videos")
    assert ordered.index("users") < ordered.index("publication_jobs")
    sql = str(CreateTable(Video.__table__).compile(dialect=postgresql.dialect()))
    assert "REFERENCES publication_generations (generation_id)" in sql
    assert "DEFERRABLE INITIALLY DEFERRED" in sql
print(json.dumps({"modules": modules, "fk_resolved": True, "table_count": len(tables)}))
'''


class StandaloneModelRegistrationTests(unittest.TestCase):
    def check_imports(self, modules):
        env = os.environ.copy()
        env.update({"PYTHONPATH": str(API), "DATABASE_URL": "sqlite://", "SEMANTIC_ENV": "development",
            "EMBEDDING_ENABLED": "false", "EMBEDDING_WARMUP_ENABLED": "false", "OPENAI_API_KEY": "",
            "SENTRY_DSN": "", "VITE_SENTRY_DSN": ""})
        completed = subprocess.run([sys.executable, "-B", "-c", CHECK, json.dumps(modules)],
            cwd=API, env=env, capture_output=True, text=True, timeout=90)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        result = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertTrue(result["fk_resolved"])
        self.assertEqual(result["modules"], modules)

    def test_standalone_seed_entrypoint_resolves_video_fk_without_api_startup(self):
        self.check_imports(["app.scripts.seed_demo"])

    def test_both_declaration_orders_and_canonical_registry_resolve_same_fk_closure(self):
        for modules in (["app.models.entities", "app.models.publication_job"],
                ["app.models.publication_job", "app.models.entities"],
                ["app.db.base", "app.models.entities"]):
            with self.subTest(modules=modules):
                self.check_imports(modules)

    def test_importers_exporters_readback_and_worker_entrypoints_register_fk_targets(self):
        for module in ("app.scripts.import_public_projection", "app.scripts.export_public_projection",
                "app.scripts.verify_public_projection", "app.scripts.publication_generation",
                "app.worker.main", "app.worker.jobs"):
            with self.subTest(module=module):
                self.check_imports([module])


if __name__ == "__main__":
    unittest.main()
