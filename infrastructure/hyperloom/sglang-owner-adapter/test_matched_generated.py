"""Composed-scope ports of the four canonical test_shared_generated cases that cannot pass on the
matched package as written (proposed for G7 as a new canonical file; the legacy suite stays unchanged
for the op217 control-only package).

Legacy case -> composed port (same intent, exact composed values, no weaker assertion):
* closure/menu/pins/budget: RAW_SESSION_LIMIT 350748672 -> the derived 354942976; member limits
  HTTP + 61 frames; payload = HTTP sum + 15 MiB; worst-width headers < 64 KiB.
* worker wraps the target once: one scope -> exactly two nested scopes (bundle outer, shared inner)
  around one `return target(`; parent barrier still follows client.run.
* parent barrier replay: schema-1 C4 fixture -> real schema-2 two-rank starts (e2e_harness).
* generated callback: schema-1 fixture without bundle readiness -> the real composed callback; only
  op227's unchanged readiness helpers (FULL-ENABLED, HIP sampling, bundle readout/client) are
  recording stubs, and their order before client.run/parent_barrier is asserted.
"""
import ast
import copy
import hashlib
import json
import sys
import time
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

import e2e_harness as h
import test_e2e_schema2 as e2e

s = h.s
PACKAGE = h.REVIEW


class TestMatchedGenerated(unittest.TestCase):
    def setUp(self):
        self.modules = patch.dict(sys.modules); self.modules.start(); self.addCleanup(self.modules.stop)
        self.artifacts = s.load(PACKAGE / "benchmark_artifacts.py", "matched_generated_benchmark_artifacts")

    def test_generated_closure_menu_pins_and_derived_budget(self):
        owner = h.ENTRY.modules(); owner.core.menu_check(h.ENTRY.CONFIG["menu"])
        self.assertEqual(len(h.ENTRY.CONFIG["menu"]), 30)
        for name, pin in h.ENTRY.MODULE_PINS.items():
            self.assertEqual(hashlib.sha256((PACKAGE / (name + ".py")).read_bytes()).hexdigest(), pin)
        controller = ast.parse((PACKAGE / "benchmark_physical_controller.py").read_bytes())
        declarations = [n for n in ast.walk(controller) if isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Attribute) and n.func.attr == "update" and n.args
                        and any(isinstance(x, ast.Constant) and x.value == "review/shared_entry.py" for x in ast.walk(n.args[0]))]
        self.assertEqual(len(declarations), 1)
        names = ast.literal_eval(declarations[0].args[0]); self.assertEqual(names, sorted(names))
        ident = json.loads((s.BUILD / "PACKAGE-IDENTITIES.json").read_bytes())["matched_profile"]
        self.assertEqual(set(names), {"review/" + n for n in ident["added_members"]})
        a = self.artifacts
        self.assertEqual(a.RAW_SESSION_LIMIT, 354942976)
        self.assertEqual(a.RAW_SESSION_LIMIT, ident["transport"]["raw_session_limit"])
        self.assertEqual(a.RAW_PAYLOAD_LIMIT, sum(a.HTTP_MEMBER_LIMITS.values()) + (15 << 20))
        self.assertEqual(len(a.RAW_MEMBER_LIMITS), len(a.HTTP_MEMBER_LIMITS) + 61)
        self.assertLess(max(a.RAW_SESSION_LIMIT, a.HTTP_SESSION_MAX_BYTES), 512 << 20)
        binding = dict(arm="bf16", experiment_mode=a.HTTP_MODE, session_sha256="a" * 64, spec_sha256="b" * 64,
                       artifact_transport=a.RAW_TRANSPORT)
        headers = [dict(schema=1, binding=binding, index=i, name=n, exists=True, bytes=v, sha256="f" * 64)
                   for i, (n, v) in enumerate(a.RAW_MEMBER_LIMITS.items())]
        headers.append(dict(schema=1, binding=binding, index=len(headers), name="END"))
        self.assertLess(sum(len(a.RAW_PREFIX) + len(a._canonical(h_)) + 1 for h_ in headers), 65536)

    def test_generated_worker_nests_shared_inside_bundle_and_barrier_precedes_return(self):
        tree = ast.parse((PACKAGE / "worker_entry.py").read_bytes())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run_scheduler_process")
        scopes = {i.context_expr.func.value.id: n for n in ast.walk(function) if isinstance(n, ast.With)
                  for i in n.items if isinstance(i.context_expr, ast.Call)
                  and isinstance(i.context_expr.func, ast.Attribute) and i.context_expr.func.attr == "worker"}
        self.assertEqual(sorted(scopes), ["bundle", "shared"])
        self.assertIn(scopes["shared"], list(ast.walk(scopes["bundle"])))
        returns = [n for n in ast.walk(scopes["shared"]) if isinstance(n, ast.Return)]
        self.assertEqual(len(returns), 1); self.assertEqual(returns[0].value.func.id, "target")
        self.assertEqual(len([n for n in ast.walk(function) if isinstance(n, ast.Call)
                              and isinstance(n.func, ast.Name) and n.func.id == "target"]), 1)
        text = (PACKAGE / "serving_runtime.py").read_text()
        self.assertLess(text.index('holder["summary"] = client.run'), text.index('material["shared_receipt"] ='))
        self.assertIn('shared_diagnostic=material["shared_receipt"]', text)
        adapter = (PACKAGE / "benchmark_owner_adapter.py").read_text()
        self.assertIn("stack['scratch_transfer_bytes']-(64 << 20)", adapter)
        for rank in (0, 1): self.assertIn(f"/mnt/shared-trace-raw-rank-{rank}", adapter)

    def test_parent_barrier_replay_uses_real_gate_and_full_worker_join(self):
        runs = e2e.runs()
        for arm in ("mtp", "control"):
            start = runs["starts"][arm]; received = runs["received"][arm]
            checked = start.replay(received)
            self.assertEqual(checked["status"], "OWNER_OBSERVATIONS_REPLAYED")
            self.assertLess(len(h.core.canonical(checked)), 4096)
            self.assertNotIn("records", checked)
            receipt = copy.deepcopy(start.receipt); receipt["identities"][0]["pid"] += 1
            result = dict(start.result, shared_diagnostic=receipt)
            with self.assertRaisesRegex(ValueError, "barrier/live-worker join"):
                h.ENTRY.replay(received, start.binding, start.workers, result, start.client)

    def test_generated_callback_runs_bundle_readiness_then_real_gate_through_barrier(self):
        start = h.Start(self, "control", e2e.fresh_root())
        h.ENTRY.before_start(start.material())
        for rank in (0, 1): start.worker(rank)
        top = start.http_summary()
        order = []
        integration = ModuleType("serving_integration")
        integration._read = lambda path, limit: (order.append(("full_enabled", path.name)), b"{}")[1]
        integration._json = lambda raw: {}
        integration.validate_full_enabled_record = lambda proof, binding, worker: order.append(("validate_full", worker["rank"]))
        bundle_readout = ModuleType("bundle_readout")
        bundle_readout.ready = lambda material, workers: order.append(("bundle_ready", [w["rank"] for w in workers]))
        bundle_client = ModuleType("bundle_client")
        bundle_client.run = lambda material, port, deadline, workers: order.append(("bundle_client", port))
        sys.modules.update(serving_integration=integration, bundle_readout=bundle_readout, bundle_client=bundle_client)
        client = h.serving_client()
        expected = {k: start.binding[k] for k in ("arm", "source_manifest_sha256", "session_sha256", "spec_sha256")}
        gate = dict(schema=1, mode=start.binding["mode"], status="NATIVE_POSTWARMUP_READY", post_warmup_native_ready=True,
                    workers=[{k: w[k] for k in client.GATE_WORKER_FIELDS} for w in start.workers], **expected)
        text = (PACKAGE / "serving_runtime.py").read_text()
        node = next(n for n in ast.parse(text).body if isinstance(n, ast.FunctionDef) and n.name == "_client_callback")
        validate_summary = Mock(side_effect=lambda *a, **k: order.append(("complete_normal_stop",)))
        barrier = h.ENTRY.parent_barrier

        def recorded_barrier(material, workers, summary, deadline):
            order.append(("parent_barrier", [w["rank"] for w in workers]))
            return barrier(material, workers, summary, deadline)
        entry = NS(parent_barrier=recorded_barrier)
        namespace = dict(betterbench_owner=None, time=time, hashlib=hashlib, serving_client=client,
                         mtp_contract=NS(instrumentation_enabled=lambda: False, phase_config=lambda: (0, 300),
                                         settings=lambda: {}),
                         _port=lambda argv: 31337, _require=h.core.need,
                         _hip_sampling_ready=lambda material, g: order.append(("hip_sampling",)),
                         _canonical=lambda value: h.core.canonical(value)[:-1],
                         _write_exclusive=lambda path, value: path.write_bytes(h.core.canonical(value)),
                         _complete_normal_stop_summary=validate_summary, _shared_entry=lambda: entry,
                         _bundle_entry=lambda: NS(modules=lambda: None), _proc_live=lambda worker: None,
                         _gdn_required=lambda sources: False)
        module = ast.parse("from __future__ import annotations\n"); module.body.append(node)
        exec(compile(module, str(PACKAGE / "serving_runtime.py"), "exec"), namespace)
        material = dict(start.material(), worker_template=dict(preweight=dict(graph_observer={})))
        material["entry_binding"] = dict(start.binding, canonical_server_cli=[])

        class Client:
            def __init__(self, **kw): order.append(("client_factory", kw["port"]))
            def run(self, g, sha):
                order.append(("client_run",)); return top
        callback = namespace["_client_callback"](material, deadline=time.monotonic() + 10, client_factory=Client, holder={})
        self.assertEqual(callback(gate), top)
        validate_summary.assert_called_once()
        self.assertEqual([o[0] for o in order], ["full_enabled", "validate_full", "full_enabled", "validate_full",
                                                 "hip_sampling", "bundle_ready", "bundle_client", "client_factory",
                                                 "client_run", "complete_normal_stop", "parent_barrier"])
        self.assertEqual(order[-1], ("parent_barrier", [0, 1]))
        receipt = material["shared_receipt"]
        self.assertEqual(receipt["identities"], [h.ENTRY.identity(start.binding, w) for w in start.workers])
        self.assertEqual([i["schema"] for i in receipt["identities"]], [2, 2])
        self.assertEqual(receipt["trust_root"], "parent-barrier-before-teardown")


if __name__ == "__main__":
    unittest.main()
