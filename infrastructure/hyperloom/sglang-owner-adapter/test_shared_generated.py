"""Exercise the generated streaming protocol and the concrete parent handoff."""
import ast
from array import array
from contextlib import contextmanager
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import shared_diagnostic as core
import test_shared_owner

PACKAGE = Path(os.environ["SHARED_PACKAGE_FIXTURES"])
NAMES = ("shared_diagnostic", "shared_trace_io", "shared_runtime_binding", "shared_capture", "shared_readout", "shared_owner")


def load(name):
    spec = importlib.util.spec_from_file_location("generated_" + name, PACKAGE / (name + ".py"))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


class TestGenerated(unittest.TestCase):
    def test_real_menu_first_prefill_uses_pinned_native_output_container(self):
        from types import SimpleNamespace as NS
        source=Path(os.environ["SHARED_SOURCE_FIXTURES"])/"schedule_batch.py"
        binding=json.loads((PACKAGE/"HTTP-BINDING-control.json").read_bytes())
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),
                         binding["binding_template"]["sources"]["schedule_batch"]["sha256"])
        req_class=next(n for n in ast.parse(source.read_bytes()).body if isinstance(n,ast.ClassDef) and n.name=="Req")
        init=next(n for n in req_class.body if isinstance(n,ast.FunctionDef) and n.name=="__init__")
        statement=next(n for n in init.body if isinstance(n,ast.Assign) and
                       any(isinstance(t,ast.Attribute) and t.attr=="output_ids" for t in n.targets))
        # Execute the retained constructor assignment itself, without importing SGLang.
        code=compile(ast.Module(body=[statement],type_ignores=[]),str(source),"exec")
        sys.modules["mtp_contract"]=load("mtp_contract")
        contract=load("serving_contract");owner=self.entry.modules()
        ident=test_shared_owner.identity();menu=self.entry.CONFIG["menu"]
        for i,row in enumerate(menu):
            with self.subTest(ordinal=i):
                frozen=contract._KV_ROWS[i]
                ids=array("q",frozen["expected"]["input_ids"])
                req=NS(rid=row["rid"],cache_salt=frozen["body"]["cache_salt"],origin_input_ids=ids,
                       finished_len=None,finished_reason=None,kv=NS(kv_committed_len=0))
                exec(code,{"array":array,"self":req})
                self.assertEqual(type(req.output_ids),array);self.assertEqual(req.output_ids.typecode,"q")
                directory=self.root/str(i);directory.mkdir()
                observer=owner.core.Observer(directory,ident,menu)
                batch=NS(reqs=[req],forward_mode=NS(is_extend=lambda:True,is_decode=lambda:False),
                         prefix_lens=[0],extend_lens=[min(2048,len(ids))],extend_num_tokens=min(2048,len(ids)))
                try:
                    token=observer.begin(batch)
                    self.assertEqual(token["before"],[dict(output=0,finished=False,kv_committed_len=0)])
                    self.assertEqual(token["members"],[i])
                finally:observer.close(True)

    def setUp(self):
        self.cache = patch.dict(sys.modules); self.cache.start()
        for name in NAMES: sys.modules.pop(name, None)
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name).resolve()
        self.entry = load("shared_entry"); self.artifacts = load("benchmark_artifacts")
        self.binding = dict(arm="bf16", experiment_mode=self.artifacts.HTTP_MODE,
                            session_sha256="a" * 64, spec_sha256="b" * 64,
                            artifact_transport=self.artifacts.RAW_TRANSPORT)
    def tearDown(self):
        self.tmp.cleanup(); self.cache.stop()

    def test_generated_closure_menu_pins_and_budget(self):
        owner = self.entry.modules(); owner.core.menu_check(self.entry.CONFIG["menu"])
        self.assertEqual(len(self.entry.CONFIG["menu"]), 30)
        for name, pin in self.entry.MODULE_PINS.items():
            self.assertEqual(hashlib.sha256((PACKAGE / (name + ".py")).read_bytes()).hexdigest(), pin)
        controller = ast.parse((PACKAGE / "benchmark_physical_controller.py").read_bytes())
        declarations = [n for n in ast.walk(controller) if isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Attribute) and n.func.attr == "update" and n.args
                        and any(isinstance(x, ast.Constant) and x.value == "review/shared_entry.py" for x in ast.walk(n.args[0]))]
        self.assertEqual(len(declarations), 1)
        self.assertIsInstance(declarations[0].args[0], ast.List)
        names = ast.literal_eval(declarations[0].args[0]); self.assertEqual(names, sorted(names))
        a = self.artifacts
        self.assertEqual(a.RAW_SESSION_LIMIT, 350748672)
        self.assertEqual(a.RAW_PAYLOAD_LIMIT, 62 << 20)
        self.assertEqual(len(a.RAW_MEMBER_LIMITS), 62)
        # Worst-width valid headers still fit the reserved 64 KiB.
        headers = [dict(schema=1, binding=self.binding, index=i, name=n, exists=True,
                        bytes=v, sha256="f" * 64) for i, (n, v) in enumerate(a.RAW_MEMBER_LIMITS.items())]
        headers.append(dict(schema=1, binding=self.binding, index=62, name="END"))
        self.assertLess(sum(len(a.RAW_PREFIX) + len(a._canonical(h)) + 1 for h in headers), 65536)

    def test_stream_roundtrip_retains_partial_rank_without_semantic_acceptance(self):
        a = self.artifacts; source = self.root / "source"; source.mkdir()
        shared = source / "shared-diagnostic"; shared.mkdir()
        for name in a.HTTP_MEMBER_LIMITS: (source / name).write_bytes(b"{}")
        (shared / "SHARED-CLOSE-rank-1.json").write_bytes(b"{")
        wire = io.BytesIO(); a.export_raw_frames(source, self.binding, wire)
        receiver = a.RawFrameReceiver(self.root / "received", self.binding, io.BytesIO())
        raw = wire.getvalue()
        for start in range(0, len(raw), 37): receiver.feed(raw[start:start+37])
        result = receiver.finish(); receiver.close()
        self.assertTrue(result["transport_valid"])
        self.assertTrue(result["all_members_present"])  # HTTP envelopes only.
        self.assertFalse(result["qualification"])
        failed = self.entry.modules().failed_start(self.root / "received/shared-diagnostic")
        self.assertEqual(failed["present_close_ranks"], [1])
        self.assertFalse(failed["semantic_validation"])

    def test_receiver_refuses_journal_partition_before_opening_fifth_megabyte(self):
        a = self.artifacts; receiver = a.RawFrameReceiver(self.root / "received", self.binding, io.BytesIO())
        try:
            # Advance through the fixed frame sequence, omitting everything but
            # four rank-0 chunks. The fourth exceeds the 3.75 MiB partition.
            for i, name in enumerate(a.RAW_MEMBER_LIMITS):
                header = dict(schema=1, binding=self.binding, index=i, name=name, exists=False)
                if "OBS-rank-0-chunk" in name:
                    payload = b"x" * (1 << 20)
                    header.update(exists=True, bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())
                else: payload = b""
                if name.endswith("OBS-rank-0-chunk-003.jsonl"):
                    with self.assertRaisesRegex(ValueError, "shared received partition"):
                        receiver.feed(a.RAW_PREFIX + a._canonical(header) + b"\n")
                    self.assertFalse((self.root / "received" / name).exists())
                    return
                receiver.feed(a.RAW_PREFIX + a._canonical(header) + b"\n")
                for start in range(0, len(payload), 65536): receiver.feed(payload[start:start+65536])
            self.fail("fourth chunk was not reached")
        finally: receiver.close()

    def test_exporter_refuses_unlisted_directory(self):
        source = self.root / "source"; source.mkdir(); shared = source / "shared-diagnostic"; shared.mkdir()
        (shared / "unlisted").mkdir()
        with self.assertRaisesRegex(ValueError, "unexpected diagnostic"):
            self.artifacts.export_raw_frames(source, self.binding, io.BytesIO())

    @contextmanager
    def barrier_fixture(self):
        fixture = test_shared_owner.TestOwner(); fixture.setUp()
        try:
            sys.modules["mtp_contract"] = load("mtp_contract")
            contract = load("serving_contract"); sys.modules["serving_contract"] = contract
            client_module = load("serving_client")
            output = self.root / "output"; output.mkdir()
            native = output / "native"; native.mkdir()
            sources = {"scheduler": dict(path="/mnt/composition/scheduler.py", sha256="d"*64)}
            binding = dict(mode=contract.MODE, arm="control", evidence_dir=str(native),
                           session_sha256="a"*64, spec_sha256="e"*64, sources=sources,
                           source_manifest_sha256=core.sha(core.canonical(sources)[:-1]),
                           canonical_server_cli=[])
            workers = []
            for rank in (0, 1):
                base = test_shared_owner.identity(rank)
                full = dict(schema=1, rank=rank, pid=base["pid"], start_ticks=100+rank, boot_id="fixture-boot",
                            **{k: binding[k] for k in ("mode", "arm", "session_sha256", "spec_sha256", "source_manifest_sha256", "sources")})
                full["identity_sha256"] = core.sha(contract.canonical(full))
                workers.append(full)
                (native / f"worker-rank-{rank}.json").write_bytes(core.canonical(full))
            original_identity = test_shared_owner.identity
            def joined_identity(rank=0):
                return dict(original_identity(rank), source_manifest_sha256=binding["source_manifest_sha256"],
                            worker_identity_sha256=workers[rank]["identity_sha256"])
            with patch.object(test_shared_owner, "identity", joined_identity):
                root, identities, pins, mounts, devices, summary, http_pin = fixture.paired(1)
            (root / "SHARED-HTTP-START.json").unlink()  # Fresh producer fixture.
            self.entry.CONFIG = dict(menu=fixture.menu, nonces={"control": identities[0]["nonce"], "mtp": "2"*16})
            import shutil
            shutil.copytree(root, output / "shared-diagnostic")
            client = output / "client"; (client / "paired").mkdir(parents=True)
            (client / "paired/RUN-SUMMARY.json").write_bytes(summary)
            expected = {k: binding[k] for k in ("arm", "source_manifest_sha256", "session_sha256", "spec_sha256")}
            gate = dict(schema=1, mode=contract.MODE, status="NATIVE_POSTWARMUP_READY", post_warmup_native_ready=True,
                        workers=[{k: w[k] for k in client_module.GATE_WORKER_FIELDS} for w in workers], **expected)
            compact = client_module.validate_gate(gate, expected)
            self.assertEqual(set(compact[0]), client_module.GATE_WORKER_FIELDS)
            self.assertNotIn("arm", compact[0])
            top = dict(blocks=[dict(name="paired", summary_bytes=len(summary), summary_sha256=core.sha(summary))])
            material = dict(entry_binding=binding, client_dir=client, native_dir=native,
                            worker_template=dict(preweight=dict(graph_observer={})))
            yield output, binding, workers, top, material, gate, client_module, compact
        finally:
            fixture.tearDown()

    def test_parent_barrier_replay_uses_real_gate_and_full_worker_join(self):
        with self.barrier_fixture() as (output, binding, workers, top, material, gate, client_module, compact):
            import time
            receipt = self.entry.parent_barrier(material, compact, top, time.monotonic()+5)
            result = dict(status="HTTP_ARM_COMPLETE", shared_diagnostic=receipt, client_summary=top)
            checked = self.entry.replay(output, binding, workers, result, material["client_dir"])
            self.assertEqual(checked["status"], "OWNER_OBSERVATIONS_REPLAYED")
            self.assertLess(len(core.canonical(checked)), 4096)
            self.assertNotIn("records", checked)
            receipt["identities"][0]["pid"] += 1
            with self.assertRaisesRegex(ValueError, "barrier/live-worker join"):
                self.entry.replay(output, binding, workers, result, material["client_dir"])

    def test_generated_callback_passes_real_validated_gate_through_barrier(self):
        with self.barrier_fixture() as (output, binding, workers, top, material, gate, client_module, compact):
            import time
            from types import SimpleNamespace as NS
            text = (PACKAGE / "serving_runtime.py").read_text()
            node = next(n for n in ast.parse(text).body if isinstance(n, ast.FunctionDef) and n.name == "_client_callback")
            validate_summary = Mock()
            namespace = dict(betterbench_owner=None, time=time, hashlib=hashlib, serving_client=client_module,
                mtp_contract=NS(instrumentation_enabled=lambda: False, phase_config=lambda: (0, 300)),
                _port=lambda argv: 1, _require=core.need, _hip_sampling_ready=lambda *args: None,
                _canonical=lambda value: core.canonical(value)[:-1],
                _write_exclusive=lambda path, value: path.write_bytes(core.canonical(value)),
                _complete_normal_stop_summary=validate_summary, _shared_entry=lambda: self.entry,
                _proc_live=lambda worker: None)
            module = ast.parse("from __future__ import annotations\n")
            module.body.append(node)
            exec(compile(module, str(PACKAGE / "serving_runtime.py"), "exec"), namespace)
            holder = {}
            callback = namespace["_client_callback"](material, deadline=time.monotonic()+10,
                client_factory=lambda **kw: NS(run=lambda *args: top), holder=holder)
            self.assertEqual(callback(gate), top)
            validate_summary.assert_called_once()
            self.assertEqual(material["shared_receipt"]["identities"],
                             [self.entry.identity(binding, worker) for worker in workers])

    def test_barrier_refuses_foreign_full_identity_and_missing_worker_before_publication(self):
        with self.barrier_fixture() as (output, binding, workers, top, material, gate, client_module, compact):
            import time
            path = material["native_dir"] / "worker-rank-0.json"
            for key, value in (("arm", "mtp"), ("session_sha256", "f"*64), ("pid", 999),
                               ("boot_id", "foreign"), ("start_ticks", 999),
                               ("identity_sha256", "f"*64), ("unbound_extra", 1)):
                full = dict(workers[0]); full[key] = value; path.write_bytes(core.canonical(full))
                with self.subTest(key=key), self.assertRaisesRegex(ValueError, "full worker / validated gate"):
                    self.entry.parent_barrier(material, compact, top, time.monotonic()+5)
                self.assertFalse((output / "shared-diagnostic/SHARED-HTTP-START.json").exists())
            path.unlink()
            with self.assertRaises(FileNotFoundError):
                self.entry.parent_barrier(material, compact, top, time.monotonic()+5)

    def test_generated_worker_wraps_the_actual_target_once_and_parent_barrier_precedes_return(self):
        tree = ast.parse((PACKAGE / "worker_entry.py").read_bytes())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run_scheduler_process")
        scopes = [n for n in ast.walk(function) if isinstance(n, ast.With) and any(
            isinstance(i.context_expr, ast.Call) and isinstance(i.context_expr.func, ast.Attribute)
            and i.context_expr.func.attr == "worker" for i in n.items)]
        self.assertEqual(len(scopes), 1)
        returns = [n for n in ast.walk(scopes[0]) if isinstance(n, ast.Return)]
        self.assertEqual(len(returns), 1); self.assertEqual(returns[0].value.func.id, "target")
        text = (PACKAGE / "serving_runtime.py").read_text()
        self.assertLess(text.index('holder["summary"] = client.run'), text.index('material["shared_receipt"] ='))
        self.assertIn('shared_diagnostic=material["shared_receipt"]', text)
        adapter = (PACKAGE / "benchmark_owner_adapter.py").read_text()
        self.assertIn("stack['scratch_transfer_bytes']-(64 << 20)", adapter)
        for rank in (0, 1): self.assertIn(f"/mnt/shared-trace-raw-rank-{rank}", adapter)


if __name__ == "__main__": unittest.main()
