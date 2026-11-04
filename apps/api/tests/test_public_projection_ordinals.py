"""Actual ordinal-preparation body at a constraint-simulating SQL boundary.

These benign tests use no SQLAlchemy/service and are not PostgreSQL acceptance.
The companion _pg module exercises real constraints and transaction rollback.
"""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
import uuid
from uuid import uuid4


class Column:
    def __init__(self, name): self.name = name
    def __eq__(self, value): return self.name, value
    def in_(self, value): return self.name, set(value)


class Statement:
    def __init__(self, model): self.model = model
    def where(self, predicate): self.predicate = predicate; return self
    def values(self, value): self.value = value; return self
    def execution_options(self, **value): self.options = value; return self


def actual_helper(db, name="_prepare_ordinal_replacement", select=None):
    path = Path(__file__).parents[1] / "app/scripts/import_public_projection.py"
    tree = ast.parse(path.read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    env = {"select": select or (lambda *columns: ("select", columns)), "update": Statement,
           "_delete_missing": db.prune, "uuid": uuid}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future, node], type_ignores=[])), str(path), "exec"), env)
    return env[name]


class Boundary:
    def __init__(self, values):
        # id -> (transcript id, ordinal), retaining real unique-key semantics.
        self.values = dict(values); self.events = []
    def prune(self, db, model, ids):
        self.events.append("prune")
        self.values = {ident: value for ident, value in self.values.items() if ident in ids}
    def execute(self, statement):
        if isinstance(statement, tuple):
            self.events.append("select")
            return SimpleNamespace(all=lambda: [(ident, value[1]) for ident, value in self.values.items()])
        self.events.append("update")
        if statement.options != {"synchronize_session": "fetch"}:
            raise AssertionError("Loaded ORM identities would not be synchronized.")
        ident = statement.predicate[1]; parent = self.values[ident][0]
        value = next(iter(statement.value.values()))
        if any(key != ident and row == (parent, value) for key, row in self.values.items()):
            raise AssertionError("Immediate unique constraint collision.")
        self.values[ident] = parent, value
    def commit(self): raise AssertionError("The importer cannot commit the caller's transaction.")


class OrdinalReplacementBoundaryTests(unittest.TestCase):
    def run_preparation(self, values, selected):
        db = Boundary(values)
        model = type("Segment", (), {"id": Column("id"), "position": Column("position")})
        actual_helper(db)(db, model, "position", [{"position": value} for value in selected.values()], set(selected))
        return db

    def test_reingest_prunes_obsolete_uuid_before_reusing_its_position(self):
        old, new, parent = uuid4(), uuid4(), uuid4()
        db = self.run_preparation({old: (parent, 0)}, {new: 0})
        self.assertEqual(db.values, {})
        self.assertEqual(db.events, ["prune", "select"])

    def test_retained_swaps_have_free_keys_before_any_final_assignment(self):
        first, second, parent = uuid4(), uuid4(), uuid4()
        db = self.run_preparation({first: (parent, 0), second: (parent, 1)}, {first: 1, second: 0})
        self.assertEqual(set(db.values), {first, second})
        self.assertEqual({value[1] for value in db.values.values()}, {-1, -2})
        self.assertEqual(db.events, ["prune", "select", "update", "update"])

    def test_parking_avoids_existing_and_selected_negative_ordinals(self):
        first, second, parent = uuid4(), uuid4(), uuid4()
        db = self.run_preparation({first: (parent, -1), second: (parent, 0)}, {first: -2, second: -3})
        self.assertEqual({value[1] for value in db.values.values()}, {-4, -5})

    def test_selected_primary_keys_and_parent_relationships_are_preserved(self):
        first, second, p1, p2 = uuid4(), uuid4(), uuid4(), uuid4()
        db = self.run_preparation({first: (p1, 0), second: (p2, 0)}, {first: 1, second: 1})
        self.assertEqual({ident: value[0] for ident, value in db.values.items()}, {first: p1, second: p2})

    def test_mixed_replacement_does_not_delete_retained_identity(self):
        old, retained, new, parent = uuid4(), uuid4(), uuid4(), uuid4()
        db = self.run_preparation({old: (parent, 0), retained: (parent, 1)}, {retained: 0, new: 1})
        self.assertEqual(set(db.values), {retained})
        self.assertLess(db.values[retained][1], 0)

    def test_empty_selection_has_no_intermediate_work(self):
        ident, parent = uuid4(), uuid4()
        db = self.run_preparation({ident: (parent, 0)}, {})
        self.assertEqual(db.events, [])
        self.assertEqual(db.values, {ident: (parent, 0)})

    def test_retained_reparenting_fails_during_read_only_validation(self):
        ident, old, new = uuid4(), uuid4(), uuid4()
        model = type("Segment", (), {"id": Column("id"), "transcript_id": Column("transcript_id")})
        db = Boundary({})
        db.execute = lambda statement: SimpleNamespace(all=lambda: [(ident, old)])
        helper = actual_helper(db, "_reject_retained_reparenting", select=lambda *columns: Statement(model))
        with self.assertRaisesRegex(ValueError, "retained transcript child"):
            helper(db, model, "transcript_id", [{"id": str(ident), "transcript_id": str(new)}])
        self.assertEqual(db.events, [])
        helper(db, model, "transcript_id", [{"id": str(ident), "transcript_id": str(old)}])


if __name__ == "__main__":
    unittest.main()
