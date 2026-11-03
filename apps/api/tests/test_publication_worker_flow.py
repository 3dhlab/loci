"""Actual worker/dispatcher bodies with repository/process boundaries injected.

These low-load orchestration units do not substitute for real PostgreSQL/RQ,
separate-process restart, destination transport or browser acceptance.
"""
import ast
from pathlib import Path
from threading import Event,Thread
from types import SimpleNamespace
import unittest
from uuid import UUID,uuid4

from app.services.publication_job_contract import StalePublicationAttempt


def functions():
    path=Path(__file__).parents[1]/"app/services/publication_jobs.py"
    tree=ast.parse(path.read_text());names={"run_publication_job","reconcile_operation","_dispatch_loop","start_dispatcher","stop_dispatcher"}
    nodes=[node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name in names]
    assert {node.name for node in nodes}==names
    env={"UUID":UUID,"Event":Event,"Thread":Thread,"StalePublicationAttempt":StalePublicationAttempt}
    exec(compile(ast.Module(body=nodes,type_ignores=[]),str(path),"exec"),env)
    return env


class PublicationWorkerFlowTests(unittest.TestCase):
    def setUp(self):
        self.env=functions();self.events=[];self.operation=uuid4();self.token=uuid4();self.actor=uuid4();self.status="QUEUED"
        self.selected={"operation_id":str(self.operation),"actor_id":str(self.actor),"selection_ref":str(self.operation),
            "selection_digest":"a"*64,"metadata":{"generated":True},"attempt_token":str(self.token)}
        test=self
        class DB:
            def __enter__(self):test.events.append("session");return self
            def __exit__(self,*args):pass
            def get(self,model,operation):return SimpleNamespace(status=test.status)
        self.env.update(engine=SimpleNamespace(dispose=lambda **kwargs:self.events.append(("dispose",kwargs))),
            SessionLocal=DB,Job=object,_claim=lambda operation:self.selected,
            admit_effect=lambda op,token:self.events.append(("effect-admitted",op,token)),
            heartbeat=lambda op,token,**kwargs:self.events.append(("heartbeat",op,token)),
            _succeed=lambda op,token,receipt:self.events.append(("succeed",op,token,receipt)) or {"state":"SUCCEEDED"},
            _fail_attempt=lambda op,token:self.events.append(("fail",op,token)),
            reconcile_operation=lambda op:self.events.append(("reconcile-only",op)) or {"state":"NEEDS_RECONCILIATION"},
            logger=SimpleNamespace(warning=lambda message:self.events.append(("warning",message))))

    def run_worker(self,transport):return self.env["run_publication_job"](str(self.operation),transport=transport)

    def test_child_disposes_inherited_pool_before_any_repository_use(self):
        self.env["_claim"]=lambda operation:None
        self.run_worker(lambda *args:self.fail("No claim, no transport."))
        self.assertEqual(self.events[:2],[("dispose",{"close":False}),"session"])

    def test_no_claim_never_runs_transport_or_finalizes(self):
        self.env["_claim"]=lambda operation:None
        self.assertIsNone(self.run_worker(lambda *args:self.fail("No transport without claim.")))
        self.assertFalse(any(isinstance(event,tuple) and event[0] in {"succeed","fail"} for event in self.events))

    def test_uncertain_restart_only_reconciles_never_reexecutes_effect(self):
        self.status="NEEDS_RECONCILIATION"
        result=self.run_worker(lambda *args:self.fail("Unknown effect cannot be repeated."))
        self.assertEqual(result["state"],"NEEDS_RECONCILIATION")
        self.assertIn(("reconcile-only",self.operation),self.events)

    def test_transport_receives_exact_selected_identity_and_before_effect_fence(self):
        def transport(op,token,ref,digest,metadata,actor,before,progress):
            self.assertEqual((op,token,ref,digest),(str(self.operation),str(self.token),str(self.operation),"a"*64))
            self.assertEqual(metadata,{"generated":True});self.assertEqual(actor["user_id"],str(self.actor))
            before();self.events.append("generated-effect");progress(stage_key="verify_destination",progress_percent=90)
            return {"generated-receipt":True}
        self.assertEqual(self.run_worker(transport)["state"],"SUCCEEDED")
        self.assertLess(self.events.index(("effect-admitted",self.operation,self.token)),self.events.index("generated-effect"))

    def test_lost_ack_invokes_durable_failure_boundary_once_without_retry(self):
        calls=[]
        def transport(*args):args[-2]();calls.append("generated-effect");raise RuntimeError("GENERATED_SECRET")
        self.run_worker(transport)
        self.assertEqual(calls,["generated-effect"])
        self.assertEqual(sum(isinstance(event,tuple) and event[0]=="fail" for event in self.events),1)
        self.assertNotIn("GENERATED_SECRET",str(self.events))

    def test_stale_authority_or_lease_prevents_effect(self):
        def deny(*args):raise StalePublicationAttempt("generated stale attempt")
        self.env["admit_effect"]=deny
        def transport(*args):args[-2]();self.fail("Effect cannot run after rejected admission.")
        self.run_worker(transport)
        self.assertIn(("fail",self.operation,self.token),self.events)

    def test_progress_losing_lease_does_not_continue_to_effect(self):
        def deny(*args,**kwargs):raise StalePublicationAttempt("generated lost lease")
        self.env["heartbeat"]=deny
        def transport(*args):args[-1](progress_percent=5);self.fail("No continued effect after lease loss.")
        self.run_worker(transport)
        self.assertIn(("fail",self.operation,self.token),self.events)

    def test_lease_thread_start_failure_is_recorded_without_joining_unstarted_thread(self):
        class Unstarted:
            ident=None
            def __init__(self,**kwargs):pass
            def start(self):raise RuntimeError("generated process limit")
            def join(self,**kwargs):raise AssertionError("Unstarted thread must not be joined.")
        self.env["Thread"]=Unstarted
        self.run_worker(lambda *args:self.fail("No transport after supervision startup failed."))
        self.assertIn(("fail",self.operation,self.token),self.events)


class PublicationDispatcherFlowTests(unittest.TestCase):
    def setUp(self):self.env=functions();self.events=[]

    def test_dispatcher_is_a_separate_spawn_process_not_a_database_thread(self):
        test=self
        class Process:
            def start(self):test.events.append("owned-start")
        context=SimpleNamespace(get_start_method=lambda:"spawn",Process=lambda **kwargs:test.events.append(kwargs) or Process())
        stop=object();self.env["start_dispatcher"](stop,context=context)
        self.assertEqual(self.events[0]["target"],self.env["_dispatch_loop"])
        self.assertEqual(self.events[0]["args"],(stop,));self.assertEqual(self.events[1],"owned-start")

    def test_fork_dispatcher_is_refused_without_start(self):
        context=SimpleNamespace(get_start_method=lambda:"fork",Process=lambda **kwargs:self.fail("No ambient fork."))
        with self.assertRaises(ValueError):self.env["start_dispatcher"](object(),context=context)

    def test_stop_targets_only_owned_process_with_one_terminate_fallback(self):
        states=iter((True,False));test=self
        process=SimpleNamespace(join=lambda **kwargs:test.events.append(("join",kwargs)),
            is_alive=lambda:next(states),terminate=lambda:test.events.append("terminate-owned"))
        event=SimpleNamespace(set=lambda:test.events.append("set-stop"))
        self.env["stop_dispatcher"](process,event)
        self.assertEqual(self.events,["set-stop",("join",{"timeout":5}),"terminate-owned",("join",{"timeout":5})])

    def test_uncertain_dispatcher_cleanup_is_reported_without_group_or_kill(self):
        process=SimpleNamespace(join=lambda **kwargs:None,is_alive=lambda:True,terminate=lambda:self.events.append("one-owned-terminate"))
        with self.assertRaises(RuntimeError):self.env["stop_dispatcher"](process,SimpleNamespace(set=lambda:None))
        self.assertEqual(self.events,["one-owned-terminate"])

    def test_outbox_outage_retries_without_logging_connection_credentials(self):
        state={"stopped":False};messages=[]
        event=SimpleNamespace(is_set=lambda:state["stopped"],wait=lambda seconds:state.update(stopped=True))
        def unavailable():raise RuntimeError("GENERATED_SECRET_URL")
        self.env.update(recover_and_dispatch=unavailable,logger=SimpleNamespace(warning=messages.append))
        self.env["_dispatch_loop"](event)
        self.assertEqual(len(messages),1);self.assertNotIn("GENERATED_SECRET_URL",messages[0])


class PublicationReconciliationFlowTests(unittest.TestCase):
    def setUp(self):
        self.env=functions();self.operation=uuid4();self.attempt=uuid4();self.recon=uuid4();self.actor=uuid4();self.events=[];self.status="NEEDS_RECONCILIATION"
        self.selected={"operation_id":str(self.operation),"actor_id":str(self.actor),"attempt_token":str(self.attempt),
            "selection_ref":str(self.operation),"selection_digest":"a"*64,"metadata":{"generated":True}}
        self.env.update(_claim_reconciliation=lambda op,actor,**kwargs:(self.selected,self.recon,None),
            _heartbeat_reconciliation=lambda op,token:None,
            _release_reconciliation=lambda op,token:self.events.append(("release",op,token)),
            logger=SimpleNamespace(warning=lambda message:self.events.append(("warning",message))),
            get_status=lambda **kwargs:{"state":self.status},_succeed=self.resolve)

    def resolve(self,operation,token,receipt,*,reconciliation):
        self.assertTrue(reconciliation);self.assertEqual(token,self.recon)
        self.events.append(("resolve",token,receipt));self.status="SUCCEEDED"

    def test_duplicate_readback_claim_returns_current_status_without_fanout(self):
        self.env["_claim_reconciliation"]=lambda *args,**kwargs:(None,None,{"state":"NEEDS_RECONCILIATION"})
        result=self.env["reconcile_operation"](self.operation,actor_id=self.actor,
            transport=lambda *args:self.fail("No second target readback for an owned live lease."))
        self.assertEqual(result["state"],"NEEDS_RECONCILIATION");self.assertEqual(self.events,[])

    def test_readback_preserves_effect_attempt_but_finalizes_only_its_reconciliation_token(self):
        def readback(op,attempt,ref,digest,metadata,actor):
            self.assertEqual(attempt,str(self.attempt));return {"generated-receipt":True}
        result=self.env["reconcile_operation"](self.operation,actor_id=self.actor,transport=readback)
        self.assertEqual(result["state"],"SUCCEEDED")
        self.assertIn(("release",self.operation,self.recon),self.events)

    def test_absent_target_receipt_releases_only_owned_readback_claim_without_promoting(self):
        result=self.env["reconcile_operation"](self.operation,actor_id=self.actor,transport=lambda *args:None)
        self.assertEqual(result["state"],"NEEDS_RECONCILIATION")
        self.assertEqual(self.events,[("release",self.operation,self.recon)])

    def test_stale_completion_does_not_become_success_or_repeat_effect(self):
        def stale(*args,**kwargs):raise StalePublicationAttempt("generated stale readback")
        self.env["_succeed"]=stale
        result=self.env["reconcile_operation"](self.operation,actor_id=self.actor,transport=lambda *args:{"generated":True})
        self.assertEqual(result["state"],"NEEDS_RECONCILIATION")
        self.assertIn(("release",self.operation,self.recon),self.events)

    def test_supervision_start_failure_releases_claim_without_target_read(self):
        class Unstarted:
            ident=None
            def __init__(self,**kwargs):pass
            def start(self):raise RuntimeError("generated supervision failure")
        self.env["Thread"]=Unstarted
        result=self.env["reconcile_operation"](self.operation,actor_id=self.actor,
            transport=lambda *args:self.fail("No remote read without lease supervision."))
        self.assertEqual(result["state"],"NEEDS_RECONCILIATION")
        self.assertIn(("release",self.operation,self.recon),self.events)


if __name__=="__main__":unittest.main()
