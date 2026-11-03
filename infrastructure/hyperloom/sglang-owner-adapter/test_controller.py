"""Campaign composition with the real exchange and an inert owner boundary."""

import copy
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[2] / "tests/workstation"))
import controller
import hyperloom_exchange as exchange
import hyperloom_exchange_host as host
import hyperloom_owner_backend as backend
import test_hyperloom_owner_backend as backend_tests


class InertOperation:
    """Only the GPU/packet boundary is substituted, with explicitly fake data."""

    def __init__(self, *, failure=False, raise_after_admission=False):
        self.calls = []
        self.failure, self.raise_after_admission = failure, raise_after_admission

    def prepare(self, snapshot, bound, *, destination, **kwargs):
        self.calls.append("prepare")
        self.request = json.loads((snapshot / "REQUEST.json").read_bytes())
        destination.mkdir()
        return dict(status="PREPARED_NOT_REGISTERED_OR_RUN", record=controller.write(destination / "RECORD.json", {"fixture": True}))

    def propose(self, preparation, bound, *, campaign_parent, **kwargs):
        self.calls.append("propose")
        return dict(status="PROPOSED_NOT_REGISTERED_OR_RUN", campaign_root=str(campaign_parent),
                    proposal=dict(sha256="1" * 64), execute_once=dict(sha256="2" * 64), nonce="3" * 16, index=191,
                    record=controller.write(campaign_parent / "PROPOSAL.json", {"fixture": True}))

    def execute(self, proposal, authorization, bound, *, cancelled, **kwargs):
        self.calls.append("execute")
        self.authorization = controller.read(authorization)
        if self.raise_after_admission:
            raise RuntimeError("synthetic lost owner response")
        if cancelled():
            return dict(status="CANCELLED", owner_started=False, park_campaign=False)
        return dict(status="FAILED" if self.failure else "SUCCEEDED", independent_release=True,
                    owner_started=True, owner_seal_sha256="4" * 64, park_campaign=self.failure,
                    payload={"fixture": True})


class EntrypointTests(unittest.TestCase):
    def test_exact_isolated_controller_cli_imports_without_inherited_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, "-I", "-B", str(HERE / "controller.py"), "--help"],
                cwd=directory, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--config-sha256", result.stdout)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        backend_tests.BackendTests.setUp(self)
        self.binding["sources"] = {"extend_attention_fwd_candidate.py": self.binding["sources"]["candidate.py"]}
        source = Path(self.binding["sources"]["extend_attention_fwd_candidate.py"]).read_bytes()
        self.binding["selected_source_set_sha256"] = exchange.digest(exchange.canonical([
            dict(name="extend_attention_fwd_candidate.py", bytes=len(source), sha256=exchange.digest(source))]))
        pins = {name: controller.pin(controller.INFRA / name)["sha256"] for name in controller.REQUIRED_SOURCES}
        self.binding["controller_sha256"] = exchange.digest(exchange.canonical(pins))
        bound = controller.write(self.root / "BINDING.json", self.binding)
        started = self.binding["campaign_deadline_unix_ms"] - 14400 * 1000
        self.grant = dict(schema=1, kind="hyperloom-sglang-campaign", execution_authorized=True,
            campaign_seconds=14400, objective="balanced-c1-c2-no-regressions", automatic_retry=False,
            adoption_authorized=False, existing_observer_only=True, watts_per_card=235,
            **{key: self.binding[key] for key in controller.IDENTITIES}, started_unix_ms=started,
            deadline_unix_ms=self.binding["campaign_deadline_unix_ms"], owner="test fixture",
            user_instruction="Synthetic CPU-only test grant; no GPU work.", allowed_kinds=["serving", "profile"])
        grant = controller.write(self.root / "GRANT.json", self.grant)
        (self.root / "controller").mkdir()
        self.configuration = dict(schema=1, binding=bound, campaign_grant=grant, source_pins=pins,
            root=str(self.root / "controller"), rpc=dict(native_binding=dict(path="/not-used-in-fixture", sha256=bound["sha256"])),
            inputs={kind: dict(owner={}, support={}) for kind in ("serving", "profile")},
            checkpoint_root=str(self.root / "fixture-checkpoints"), run_parent=str(self.root / "fixture-runs"))
        self.rpc_calls = []

    def rpc(self, config, action, destination, *, job_id=None, result=None, deadline_unix_ms=None):
        self.rpc_calls.append(action)
        self.assertLessEqual(deadline_unix_ms, self.binding["campaign_deadline_unix_ms"])
        return host.dispatch(action, self.binding, job_id=job_id, result=result)

    def start(self, operation=None, rpc=None):
        self.operation = operation or InertOperation()
        return controller.Controller(self.configuration, rpc=rpc or self.rpc, operation=self.operation)

    def submit(self):
        return backend.start(self.config, self.root / "output", self.binding)

    def test_one_snapshot_one_derived_grant_one_owner_one_reply(self):
        _, request = self.submit()
        instance = self.start()
        result = instance.step()
        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(self.operation.calls, ["prepare", "propose", "execute"])
        self.assertEqual(self.rpc_calls, ["list", "take", "cancelled", "cancelled", "publish"])
        authority = self.operation.authorization
        self.assertEqual(authority["approval"], self.configuration["campaign_grant"])
        self.assertIn(self.grant["user_instruction"], authority["owner_answer"])
        self.assertEqual(authority["request_sha256"], exchange.digest(exchange.canonical(request)))
        self.assertFalse(authority["adoption_authorized"])
        loaded = exchange.read_result(self.root / "replies" / request["id"], request,
                                      controller_sha256=self.binding["controller_sha256"])
        self.assertEqual(loaded, result)
        self.assertEqual(instance.step()["status"], "IDLE")
        self.assertEqual(self.operation.calls.count("execute"), 1)

    def test_failure_parks_even_after_independent_release(self):
        self.submit()
        instance = self.start(InertOperation(failure=True))
        result = instance.step()
        self.assertEqual(result["status"], "FAILED")
        self.assertTrue(result["independent_release"] and instance.parked)
        with self.assertRaises(ValueError):
            instance.step()
        self.assertEqual(self.operation.calls.count("execute"), 1)

    def test_unknown_execution_failure_does_not_claim_owner_was_never_started(self):
        self.submit()
        instance = self.start(InertOperation(raise_after_admission=True))
        result = instance.step()
        self.assertEqual(result["status"], "FAILED")
        self.assertIsNone(result["owner_started"])
        self.assertFalse(result["independent_release"])
        self.assertTrue(instance.parked)

    def test_publication_failure_retains_result_and_never_retries(self):
        _, request = self.submit()
        def rpc(*args, **kwargs):
            if args[1] == "publish":
                raise RuntimeError("synthetic transport failure")
            return self.rpc(*args, **kwargs)
        instance = self.start(rpc=rpc)
        self.assertEqual(instance.step()["status"], "FAILED")
        root = self.root / "controller/jobs" / request["id"]
        self.assertTrue((root / "OUTCOME.json").is_file() and (root / "PUBLICATION-FAILURE.json").is_file())
        self.assertTrue(instance.parked)
        with self.assertRaises(ValueError):
            instance.step()
        self.assertEqual(self.operation.calls.count("execute"), 1)

    def test_subprocess_timeout_is_published_and_closes_pending_waits(self):
        job, request = self.submit()
        operation = InertOperation()
        def expired(*args, **kwargs):
            raise subprocess.TimeoutExpired(["CPU-fixture-command"], 1)
        operation.execute = expired
        instance = self.start(operation)
        result = instance.step()
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("TimeoutExpired",result["reason"])
        self.assertTrue((self.root/"replies/CAMPAIGN-PARKED.json").is_file())
        # A completed per-job disposition remains readable after campaign closeout.
        self.assertEqual(exchange.wait_result(job,self.root/"replies",request,
            controller_sha256=self.binding["controller_sha256"]),result)

    def test_expired_take_never_invokes_owner_and_pending_client_stops(self):
        job, request = self.submit()
        def rpc(config, action, destination, **kwargs):
            if action == "take":
                return host.dispatch(action,self.binding,job_id=kwargs['job_id'],
                                     now=request['created_unix_ms']/1000+61)
            return self.rpc(config,action,destination,**kwargs)
        instance=self.start(rpc=rpc)
        result=instance.step()
        self.assertEqual(result['status'],'FAILED')
        self.assertEqual(self.operation.calls,[])
        self.assertTrue((self.root/'controller/DISPATCH-FAILURE.json').is_file())
        with self.assertRaisesRegex(RuntimeError,'campaign parked'):
            exchange.wait_result(job,self.root/'replies',request,
                                 controller_sha256=self.binding['controller_sha256'])
        self.assertTrue((job/'CANCELLED.json').is_file())

    def test_snapshot_failure_after_claim_closes_exchange_without_invented_result(self):
        job, request=self.submit();instance=self.start()
        with patch.object(host,'retain_snapshot',side_effect=ValueError('fixture corrupt snapshot')):
            result=instance.step()
        self.assertEqual(result['status'],'FAILED')
        self.assertTrue((self.root/'replies'/request['id']/'CLAIM.json').is_file())
        self.assertFalse((self.root/'replies'/request['id']/'RESULT.json').exists())
        with self.assertRaisesRegex(RuntimeError,'campaign parked'):
            exchange.wait_result(job,self.root/'replies',request,
                                 controller_sha256=self.binding['controller_sha256'])
        self.assertEqual(self.operation.calls,[])

    def test_ssh_take_timeout_is_retained_and_terminal_transport_is_attempted_once(self):
        self.submit()
        actions=[]
        def rpc(*args, **kwargs):
            actions.append(args[1])
            if args[1] in ('take','park'):
                raise subprocess.TimeoutExpired(['fixture-ssh'],1)
            return self.rpc(*args,**kwargs)
        instance=self.start(rpc=rpc)
        self.assertEqual(instance.step()['status'],'FAILED')
        self.assertEqual(actions,['list','take','park'])
        self.assertTrue((self.root/'controller/TERMINAL-PUBLICATION-FAILURE.json').is_file())
        self.assertEqual(self.operation.calls,[])

    def test_campaign_exhaustion_starts_no_rpc_or_owner(self):
        self.submit()
        instance = self.start()
        instance.deadline = time.monotonic() - 1
        self.assertEqual(instance.step()["status"], "BUDGET_EXHAUSTED")
        self.assertEqual(self.rpc_calls, [])
        self.assertEqual(self.operation.calls, [])

    def test_cancellation_after_claim_prevents_preparation(self):
        job, _ = self.submit()
        def rpc(*args, **kwargs):
            result = self.rpc(*args, **kwargs)
            if args[1] == "take":
                (job / "CANCELLED.json").write_text("{}\n")
            return result
        instance = self.start(rpc=rpc)
        self.assertEqual(instance.step()["status"], "CANCELLED")
        self.assertEqual(self.operation.calls, [])

    def test_source_closure_or_native_binding_drift_refuses_before_start(self):
        original = copy.deepcopy(self.configuration)
        for change in (lambda c: c["source_pins"].update({next(iter(controller.REQUIRED_SOURCES)): "0" * 64}),
                       lambda c: c["rpc"]["native_binding"].update(sha256="0" * 64)):
            self.configuration = copy.deepcopy(original)
            change(self.configuration)
            with self.assertRaises(ValueError):
                self.start()
        self.assertFalse((self.root / "controller/CONTROLLER-STARTED.json").exists())

    def test_no_execution_without_the_retained_campaign_grant(self):
        for field, value in (("execution_authorized", False), ("watts_per_card", 250),
                             ("campaign_seconds", 14401), ("adoption_authorized", True),
                             ("existing_observer_only", False), ("runtime_sha256", "0" * 64)):
            value = dict(self.grant, **{field: value})
            path = self.root / (field + ".json")
            self.configuration["campaign_grant"] = controller.write(path, value)
            with self.subTest(field), self.assertRaises(ValueError):
                self.start()
        self.assertFalse((self.root / "controller/CONTROLLER-STARTED.json").exists())

    def component_campaign(self):
        self.grant["allowed_kinds"] = ["serving", "profile", "component"]
        self.configuration["campaign_grant"] = controller.write(self.root/"COMPONENT-GRANT.json",self.grant)
        self.configuration["inputs"]["component"] = dict(owner={},support={})

    def test_changed_source_requires_component_result_before_serving(self):
        Path(next(iter(self.binding["sources"].values()))).write_text("answer = 43\n")
        self.submit()
        instance=self.start()
        result=instance.step()
        self.assertEqual(result["status"],"FAILED")
        self.assertIn("component screen",result["reason"])
        self.assertEqual(self.operation.calls,[])

    def test_component_requests_route_to_separate_operation_and_record_eligibility(self):
        self.component_campaign()
        config=copy.deepcopy(self.config)
        config["benchmark"].update(timeout_seconds=2910,workstation_component={"contract":"c4-fixed-geometry-v1"})
        backend.start(config,self.root/"output",self.binding)
        component=InertOperation()
        original=component.execute
        def execute(*args,**kwargs):
            result=original(*args,**kwargs)
            result['payload']={'component_report':{'eligible_for_serving':False,'fixture':True}}
            return result
        component.execute=execute
        serving=InertOperation()
        instance=controller.Controller(self.configuration,rpc=self.rpc,operation=serving,component=component)
        result=instance.step()
        self.assertEqual(result['status'],'SUCCEEDED')
        self.assertEqual(component.calls,['prepare','propose','execute'])
        self.assertEqual(serving.calls,[])
        self.assertFalse(instance.components[result['source_set_sha256']]['eligible_for_serving'])
        self.assertFalse(instance.parked)

    def test_cpu_resource_rejection_is_retained_without_gpu_admission(self):
        self.component_campaign()
        config=copy.deepcopy(self.config)
        config["benchmark"].update(timeout_seconds=2910,workstation_component={"contract":"c4-fixed-geometry-v1"})
        backend.start(config,self.root/"output",self.binding)
        component=InertOperation()
        def prepare(*args,**kwargs):
            component.calls.append('prepare')
            return dict(status='CPU_CANDIDATE_REJECTED',reason='synthetic resource gate refusal',gpu_work=False)
        component.prepare=prepare
        instance=controller.Controller(self.configuration,rpc=self.rpc,component=component)
        result=instance.step()
        self.assertEqual(result['status'],'FAILED')
        self.assertFalse(result['owner_started'])
        self.assertFalse(instance.parked)
        self.assertEqual(component.calls,['prepare'])
        self.assertIn('component_compile',result['payload'])


if __name__ == "__main__":
    unittest.main(verbosity=2)
