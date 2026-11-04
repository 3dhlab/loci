"""Real sync function bodies with generated DB/SSH boundaries, not an API/DB proof.

The explicit AST slice lets these low-load unit regressions run without loading
API dependencies or creating a database/server. No source body is rewritten.
Full SQLAlchemy/PostgreSQL and two-installation SSH acceptance remain required.
"""
import ast
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import secrets
import shlex
import tempfile
from threading import Lock
from types import SimpleNamespace
import unittest
from uuid import UUID

from app.services.publication_readback import (
    COLLECTIONS, IGNORED_FIELDS, PATH_FIELDS, PublicationReadbackError, canonical,
    expected_receipt, observe_receipt, require_receipt,
)


def sync_functions():
    path = Path(__file__).parents[1] / "app/services/public_sync.py"
    tree = ast.parse(path.read_text())
    names = {"PublicSyncConfig", "PublicSyncResult", "PublicSyncStatus", "PublicSyncUncertainError",
             "_default_sync_actor", "_utc_now_iso", "_counts_from_manifest", "_stable_projection_digest",
             "_destination_readback_command", "_check_destination_readback_protocol",
             "_require_current_publication_authority", "_prepare_destination_readback", "_verify_destination", "sync_public_projection",
             "_run_live_database_sync", "begin_live_database_sync", "_command_error"}
    nodes = [node for node in tree.body if getattr(node, "name", None) in names]
    assert {node.name for node in nodes} == names
    module = ast.Module(body=[ast.ImportFrom(module="__future__",names=[ast.alias(name="annotations")],level=0),*nodes],type_ignores=[])
    env = {"dataclass":dataclass,"replace":replace,"datetime":datetime,"timezone":timezone,"hashlib":hashlib,
           "json":json,"Path":Path,"secrets":secrets,"shlex":shlex,"tempfile":tempfile,"UUID":UUID,
           "canonical":canonical,"expected_receipt":expected_receipt,"require_receipt":require_receipt}
    exec(compile(ast.fix_missing_locations(module),str(path),"exec"),env)
    return env


class PublicSyncReadbackTests(unittest.TestCase):
    def setUp(self):
        self.env=sync_functions(); self.events=[]; self.state={"state":"IDLE"};self.fail_commit=False
        self.actor={"type":"user","user_id":"00000000-0000-0000-0000-000000000001"}
        self.user=SimpleNamespace(is_active=True)
        self.manifest={"media_paths":[],"objects":[{"id":"generated","website_object_id":"generated-slug"}]}
        self.config=self.env["PublicSyncConfig"]("configured-target",Path("/generated/key"),"/app","/import","/media","manifest.json","media.txt")
        test=self
        class DB:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def get(self,model,actor_id):
                test.events.append("authority")
                return test.user
            def commit(self):
                test.events.append("commit")
                if test.fail_commit:raise RuntimeError("generated local commit acknowledgement failure")
        self.env.update(SessionLocal=DB,User=object,build_projection=lambda:(self.manifest,[]),
            _load_sync_config=lambda:self.config,_build_media_inventory=lambda *a,**k:{},
            persist_publication_manifests=lambda *a,**k:SimpleNamespace(current_manifest_ids=["generated"]),
            mark_publication_manifests_projected=lambda *a,**k:self.events.append("projected"),
            append_audit_event=lambda *a,**k:self.events.append(("audit",k)),
            _record_public_sync_event=lambda **k:self.events.append(("failure-audit",k)),
            _update_sync_status=lambda op,**k:self.state.update(k),
            _load_remote_sync_state=lambda config:{},
            _ssh=self.ssh,_scp=lambda *a,**k:self.events.append("scp"),
            bounded_readback_command=self.command,
        )

    def command(self,argv,request,*,timeout):
        self.assertNotIn("-n",argv)
        self.assertIn("StrictHostKeyChecking=yes",argv)
        if argv[-1].endswith(" --protocol-version"):
            self.events.append("protocol")
            return canonical({"protocol":"loci-publication-readback","version":1})+b"\n"
        self.events.append("readback")
        value=json.loads(request)
        return canonical({"receipt":expected_receipt(value["manifest"],value["media_inventory"]),"challenge":value["challenge"]})

    def ssh(self,config,command,**kwargs):
        self.events.append("import" if "import_public_projection" in command else "ssh")
        return SimpleNamespace(stdout="")

    def run_sync(self):return self.env["sync_public_projection"](actor=self.actor)

    def test_protocol_precedes_every_remote_path_or_upload(self):
        result=self.run_sync()
        self.assertEqual(result.object_count,1)
        first=self.events.index("protocol")
        self.assertTrue(all(index>first for index,event in enumerate(self.events) if isinstance(event,str) and event in {"ssh","scp","import"}))
        self.assertLess(self.events.index("import"),self.events.index("readback"))
        self.assertLess(self.events.index("readback"),self.events.index("projected"))
        complete=[event[1] for event in self.events if isinstance(event,tuple) and event[0]=="audit"][-1]
        self.assertEqual(complete["payload_json"]["destination_receipt"],expected_receipt(self.manifest,{}))

    def test_old_destination_fails_without_any_remote_mutation(self):
        self.env["bounded_readback_command"]=lambda *a,**k: b"old or unavailable"
        with self.assertRaisesRegex(RuntimeError,"Upgrade the destination"):
            self.run_sync()
        self.assertFalse(any(isinstance(event,str) and event in {"ssh","scp","import","readback","projected"} for event in self.events))
        self.assertEqual(self.state["state"],"IDLE")

    def test_protocol_version_or_extra_output_does_not_fall_back(self):
        for value in (b'{"protocol":"loci-publication-readback","version":2}\n',
                      canonical({"protocol":"loci-publication-readback","version":1})+b"\nnoise"):
            self.env["bounded_readback_command"]=lambda *a,**k:value
            with self.assertRaises(RuntimeError):self.run_sync()

    def test_noop_still_observes_real_destination(self):
        self.env["_load_remote_sync_state"]=lambda config:{"projection_digest":self.env["_stable_projection_digest"](self.manifest),"media_inventory":{}}
        result=self.run_sync()
        self.assertFalse(result.manifest_changed)
        self.assertIn("readback",self.events)
        self.assertNotIn("import",self.events)
        self.assertLess(self.events.index("readback"),self.events.index("projected"))

    def test_changed_receipt_never_marks_projected_and_blocks_same_process(self):
        original=self.command
        def command(argv,request,**kwargs):
            result=original(argv,request,**kwargs)
            if request:
                payload=json.loads(result);payload["receipt"]["media_file_count"]=7
                return canonical(payload)
            return result
        self.env["bounded_readback_command"]=command
        with self.assertRaises(self.env["PublicSyncUncertainError"]):self.run_sync()
        self.assertNotIn("projected",self.events)
        self.assertEqual(self.state["state"],"NEEDS_RECONCILIATION")

    def test_lost_or_stale_readback_ack_is_uncertain_not_success(self):
        original=self.command
        def command(argv,request,**kwargs):
            result=original(argv,request,**kwargs)
            if request:
                payload=json.loads(result);payload["challenge"]="stale"
                return canonical(payload)
            return result
        self.env["bounded_readback_command"]=command
        with self.assertRaises(self.env["PublicSyncUncertainError"]):self.run_sync()
        self.assertNotIn("projected",self.events)

    def test_import_failure_is_ambiguous_and_does_not_allow_ordinary_retry(self):
        def ssh(config,command,**kwargs):
            self.ssh(config,command,**kwargs)
            if "import_public_projection" in command:raise RuntimeError("generated timeout")
            return SimpleNamespace(stdout="")
        self.env["_ssh"]=ssh
        with self.assertRaises(self.env["PublicSyncUncertainError"]):self.run_sync()
        self.assertEqual(self.state["state"],"NEEDS_RECONCILIATION")
        self.assertNotIn("projected",self.events)

    def test_revocation_before_any_remote_contact_is_denied(self):
        self.user.is_active=False
        with self.assertRaisesRegex(RuntimeError,"no longer authorized"):self.run_sync()
        self.assertNotIn("protocol",self.events)
        self.assertNotIn("import",self.events)

    def test_revocation_after_upload_stops_before_import(self):
        def upload(*args,**kwargs):self.events.append("scp");self.user.is_active=False
        self.env["_scp"]=upload
        with self.assertRaisesRegex(RuntimeError,"no longer authorized"):self.run_sync()
        self.assertNotIn("import",self.events)
        self.assertNotIn("projected",self.events)

    def test_active_non_admin_preserves_existing_policy(self):
        self.user.is_platform_admin=False
        self.run_sync()
        self.assertIn("projected",self.events)

    def test_invalid_actor_never_contacts_destination(self):
        self.actor["user_id"]="invalid"
        with self.assertRaisesRegex(RuntimeError,"actor is invalid"):self.run_sync()
        self.assertNotIn("protocol",self.events)

    def test_system_internal_calls_remain_supported(self):
        self.actor={"type":"system"}
        self.run_sync()
        self.assertNotIn("authority",self.events)
        self.assertIn("projected",self.events)

    def test_readback_exception_scrubs_remote_output_and_private_paths(self):
        original=self.command
        def command(argv,request,**kwargs):
            if request:raise RuntimeError("generated confidential stderr /private/secret")
            return original(argv,request,**kwargs)
        self.env["bounded_readback_command"]=command
        with self.assertRaises(self.env["PublicSyncUncertainError"]) as error:self.run_sync()
        self.assertNotIn("/private/secret",str(error.exception))

    def test_audit_failure_cannot_downgrade_uncertainty(self):
        def ssh(config,command,**kwargs):
            if "import_public_projection" in command:raise RuntimeError("generated timeout")
            return SimpleNamespace(stdout="")
        self.env["_ssh"]=ssh
        def audit(**kwargs):raise RuntimeError("generated DB failure")
        self.env["_record_public_sync_event"]=audit
        with self.assertRaises(self.env["PublicSyncUncertainError"]):self.run_sync()
        self.assertEqual(self.state["state"],"NEEDS_RECONCILIATION")

    def test_local_commit_failure_after_destination_readback_stays_uncertain(self):
        self.fail_commit=True
        with self.assertRaises(self.env["PublicSyncUncertainError"]):self.run_sync()
        self.assertIn("readback",self.events)
        self.assertEqual(self.state["state"],"NEEDS_RECONCILIATION")

    def test_noop_local_commit_failure_stays_uncertain(self):
        self.fail_commit=True
        self.env["_load_remote_sync_state"]=lambda config:{"projection_digest":self.env["_stable_projection_digest"](self.manifest),"media_inventory":{}}
        with self.assertRaises(self.env["PublicSyncUncertainError"]):self.run_sync()
        self.assertIn("readback",self.events)
        self.assertNotIn("import",self.events)
        self.assertEqual(self.state["state"],"NEEDS_RECONCILIATION")

    def test_oversized_readback_request_fails_before_remote_contact(self):
        self.env["canonical"]=lambda value:b"x"*(32*1024*1024+1)
        with self.assertRaisesRegex(ValueError,"too large"):self.run_sync()
        self.assertNotIn("protocol",self.events)
        self.assertNotIn("scp",self.events)

    def test_invalid_local_media_selection_fails_before_remote_contact(self):
        self.manifest["media_paths"]=["missing-generated"]
        with self.assertRaises(Exception):self.run_sync()
        self.assertNotIn("protocol",self.events)

    def test_async_runner_keeps_ambiguous_outcome_in_reconciliation(self):
        original=self.command
        def command(argv,request,**kwargs):
            if request:raise RuntimeError("generated lost acknowledgement")
            return original(argv,request,**kwargs)
        self.env["bounded_readback_command"]=command
        self.env["_snapshot_sync_status"]=lambda:SimpleNamespace(progress_percent=33)
        self.env["_run_live_database_sync"]("generated-operation",self.actor)
        self.assertEqual(self.state["state"],"NEEDS_RECONCILIATION")
        self.assertEqual(self.state["stage_key"],"reconciliation")
        self.assertIsNone(self.state["finished_at"])
        self.assertNotIn("projected",self.events)

    def test_async_start_cannot_relaunch_ambiguous_same_process_operation(self):
        self.env["_SYNC_STATUS_LOCK"]=Lock()
        self.env["_SYNC_STATUS"]=self.env["PublicSyncStatus"](state="NEEDS_RECONCILIATION",operation_id="generated")
        status=self.env["begin_live_database_sync"](actor=self.actor)
        self.assertEqual(status.state,"NEEDS_RECONCILIATION")
        self.assertEqual(status.operation_id,"generated")

    def test_transport_diagnostics_do_not_expose_keys_paths_or_remote_output(self):
        error=self.env["_command_error"](["/usr/bin/ssh","-i","/private/generated-key"],
            SimpleNamespace(returncode=1,stdout="generated record body",stderr="generated credential"))
        self.assertIn("ssh",str(error))
        self.assertIn("exit 1",str(error))
        for value in ("/private/", "generated-key", "generated record", "generated credential"):
            self.assertNotIn(value,str(error))


class DestinationReadbackQueryTests(unittest.TestCase):
    """Actual destination verifier body; query/session boundaries are generated fakes."""
    def setUp(self):
        script=Path(__file__).parents[1]/"app/scripts/verify_public_projection.py"
        tree=ast.parse(script.read_text());nodes=[node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name in {"verify","observe_database"}]
        self.manifest={name:[] for name in COLLECTIONS}
        self.manifest.update(media_paths=[],objects=[{"id":"generated","name":"Generated object"}])
        self.rows={name:[] for name in COLLECTIONS};self.rows["objects"]=[{"id":"generated","name":"Generated object","embedding_vector":[1]*8}]
        self.calls=[];self.options=[];test=self
        class Column:
            def __init__(self,collection,name):self.collection=collection;self.name=name
            def label(self,name):return(self.collection,name)
        models={}
        for name in COLLECTIONS:
            models[name]=SimpleNamespace(_collection=name,**{field:Column(name,field) for field in ("id","name","embedding_vector")})
        class Query:
            def __init__(self,selected):self.selected=selected;self.maximum=None
            def limit(self,value):self.maximum=value;return self
        class DB:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def connection(self,*,execution_options):test.options.append(execution_options)
            def execute(self,query):
                collection=query.selected[0][0];names=[name for _,name in query.selected]
                test.calls.append((collection,names,query.maximum))
                rows=[{name:row[name] for name in names} for row in test.rows[collection][:query.maximum]]
                return SimpleNamespace(mappings=lambda:iter(rows))
        env={"re":__import__("re"),"expected_receipt":expected_receipt,"observe_receipt":observe_receipt,
            "MODELS":models,"SessionLocal":DB,"settings":SimpleNamespace(media_root="/generated"),
            "inspect":lambda model:SimpleNamespace(column_attrs=[SimpleNamespace(key=key) for key in ("id","name","embedding_vector")]),
            "select":lambda *selected:Query(selected),"build_media_inventory":lambda *args:{},
            "IGNORED_FIELDS":IGNORED_FIELDS,"PATH_FIELDS":PATH_FIELDS,"PublicationReadbackError":PublicationReadbackError}
        exec(compile(ast.Module(body=nodes,type_ignores=[]),str(script),"exec"),env)
        self.verify=env["verify"]

    def request(self):return{"manifest":self.manifest,"media_inventory":{},"challenge":"a"*64}

    def test_compared_columns_only_expected_plus_one_and_readonly_snapshot(self):
        result=self.verify(self.request())
        self.assertEqual(result["receipt"],expected_receipt(self.manifest,{}))
        self.assertEqual(self.options,[{"isolation_level":"REPEATABLE READ","postgresql_readonly":True}])
        for collection,names,maximum in self.calls:
            self.assertNotIn("embedding_vector",names)
            self.assertEqual(maximum,len(self.manifest[collection])+1)

    def test_extra_rows_are_rejected_without_unbounded_table_materialization(self):
        self.rows["objects"] += [{"id":f"extra-{index}","name":"Generated"} for index in range(10)]
        with self.assertRaises(PublicationReadbackError):self.verify(self.request())
        self.assertEqual(next(item[2] for item in self.calls if item[0]=="objects"),2)

    def test_unsupported_column_is_refused_without_attribute_traversal(self):
        self.manifest["objects"][0]["__dict__"]="generated"
        with self.assertRaisesRegex(PublicationReadbackError,"Unsupported destination field"):self.verify(self.request())
        self.assertFalse(any(collection=="objects" for collection,_,_ in self.calls))


if __name__ == "__main__": unittest.main()
