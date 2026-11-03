"""Exercise the actual backend client with an inert owner exchange."""

import asyncio
import copy
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib/workstation"))
import hyperloom_exchange as exchange
import hyperloom_owner_backend as backend
from test_hyperloom_latency import POLICY, paired_record


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        for name in ("requests", "replies", "output"):
            (self.root / name).mkdir()
        source = self.root / "candidate.py"
        source.write_text("answer = 42\n")
        fixed = {"framework": "sglang", "model": "/frozen/model", "runner_type": "gfx12",
                 "precision": "mxfp4", "envs": {"TP": "2"}, "workstation_workload": "balanced_c1_c2"}
        self.binding = {"schema": 1, "backend": backend.NAME, "campaign_sha256": "a" * 64,
            "selection_sha256": "b" * 64, "controller_sha256": "c" * 64, "runtime_sha256": "d" * 64,
            "workload_sha256": "e" * 64, "campaign_deadline_unix_ms": int((time.time() + 14400) * 1000),
            "requests_root": str(self.root / "requests"), "replies_root": str(self.root / "replies"),
            "sources": {"candidate.py": str(source)}, "benchmark": fixed,
            "selected_source_set_sha256": "e" * 64, "profile_primary_window": "decode-0",
            "caller_seconds": {"serving": 7160, "profile": 7160, "component": 2190},
            "latency_policy": dict(POLICY), "automatic_retry": False, "adoption_authorized": False}
        self.config = {"benchmark": dict(fixed, timeout_seconds=7660)}
        self.binding["selected_source_set_sha256"] = backend.source_set(self.binding)

    def reply(self, job, replies, request, *, controller_sha256):
        _, _, reply = exchange.claim(job, replies, campaign_sha256=self.binding["campaign_sha256"],
                                    selection_sha256=self.binding["selection_sha256"],
                                    controller_sha256=controller_sha256)
        report = self.report(request)
        return exchange.publish_result(reply, request, status="SUCCEEDED", payload={"benchmark_report": report},
                                       owner_seal_sha256="f" * 64, independent_release=True)

    def report(self, request):
        pair = paired_record("f")
        observed, control = pair["workstation_latency"], pair["workstation_latency_control"]
        for row in (observed, control):
            row.update({k: self.binding[k] for k in ("selection_sha256", "runtime_sha256", "workload_sha256")})
        observed["candidate_sha256"] = request["source_set_sha256"]
        control["candidate_sha256"] = self.binding["selected_source_set_sha256"]
        return {"success": True, "framework": "sglang", "model": "/frozen/model",
            "benchmark_backend": backend.NAME, "benchmark_valid": True, "diagnostic_only": False,
            "quality_gate": {"passed": True}, "adoption_authorized": False,
            "throughput": {"output_throughput": 42.5, "unit": "tokens/s"},
            "workstation_latency": observed, "workstation_latency_control": control,
            "owner_binding": {"owner_seal_sha256": "f" * 64,
                              "request_sha256": exchange.digest(exchange.canonical(request)),
                              "source_set_sha256": request["source_set_sha256"]}}

    def qualification(self):
        """Explicit inert qualification fixture, never usable by a live controller."""
        job, request = backend.start(self.config, self.root / "output", self.binding)
        result = self.reply(job, self.root / "replies", request,
                            controller_sha256=self.binding["controller_sha256"])
        report = backend.validate_report(result, request, self.binding)
        inputs = self.root / "inputs/qualification"
        workspace = inputs / "serving"
        workspace.mkdir(parents=True)
        reference = {"workspace": str(workspace)}
        for name, key, value in (("OWNER-JOB.json", "request_sha256", request),
                                 ("OWNER-RESULT.json", "result_sha256", result),
                                 ("benchmark_report.json", "report_sha256", report)):
            raw = exchange.canonical(value)
            (workspace / name).write_bytes(raw)
            reference[key] = exchange.digest(raw)
        checks = {name: True for name in ("serving_aa", "c1_c2_profile", "component_oracle", "owner_release",
            "failure_retention", "response_mount_read_only", "agents_have_no_gpu_devices",
            "upstream_balanced_keep_paths", "campaign_deadline")}
        proof = dict(schema=1, kind="workstation-owner-native-qualification", status="PASS",
            fixture="CPU ONLY: synthetic proof; no workstation authority", checks=checks,
            measurements={"serving_aa": reference}, owner_seals=[], **{name:self.binding[name] for name in
                ("campaign_sha256", "campaign_deadline_unix_ms", "selection_sha256", "runtime_sha256",
                 "workload_sha256", "controller_sha256")})
        for name in ("seal-a", "seal-b"):
            path = inputs / name / "FINAL-ARTIFACTS.sha256"
            path.parent.mkdir(); path.write_text("CPU fixture, not real owner evidence\n")
            proof["owner_seals"].append(dict(path=str(path), sha256=exchange.digest(path.read_bytes())))
        self.proof_path = inputs / "QUALIFICATION.json"
        self.proof = proof
        self.repin_proof()
        return request, result, report, workspace

    def repin_proof(self):
        self.proof_path.write_bytes(exchange.canonical(self.proof))
        self.binding["qualification"] = dict(path=str(self.proof_path),
            sha256=exchange.digest(self.proof_path.read_bytes()))

    def test_qualification_reuse_keeps_original_measurement_and_submits_nothing(self):
        request, result, report, workspace = self.qualification()
        target=self.root/'reused';target.mkdir()
        before=list((self.root/'requests').iterdir())
        self.assertEqual(backend.reuse_qualification('serving',target,self.binding),report)
        self.assertEqual((target/'benchmark_report.json').read_bytes(),(workspace/'benchmark_report.json').read_bytes())
        self.assertEqual(list((self.root/'requests').iterdir()),before)
        self.assertEqual(exchange.decoded_json((target/'OWNER-JOB.json').read_bytes()),request)
        self.assertFalse(exchange.decoded_json((target/'REUSED-FROM.json').read_bytes())['new_measurement'])
        with self.assertRaises(ValueError):backend.reuse_qualification('serving',target,self.binding)

    def test_qualification_reuse_reauthenticates_bytes_result_kind_source_and_campaign(self):
        request,result,report,workspace=self.qualification()
        original={p.name:p.read_bytes() for p in workspace.iterdir()}
        proof=copy.deepcopy(self.proof)
        mutations=[('benchmark_report.json',lambda v:v.update(success=False),False),
            ('OWNER-RESULT.json',lambda v:v.update(id='0'*32),True),
            ('OWNER-RESULT.json',lambda v:v.update(controller_sha256='0'*64),True),
            ('OWNER-RESULT.json',lambda v:v['payload']['benchmark_report'].update(diagnostic_only=True),True),
            ('OWNER-JOB.json',lambda v:v.update(source_set_sha256='0'*64),True),
            ('OWNER-JOB.json',lambda v:v.update(campaign_sha256='0'*64),True)]
        for index,(name,mutate,repin) in enumerate(mutations):
            for filename,raw in original.items():(workspace/filename).write_bytes(raw)
            self.proof=copy.deepcopy(proof)
            value=exchange.decoded_json(original[name]);mutate(value)
            raw=exchange.canonical(value);(workspace/name).write_bytes(raw)
            if repin:
                key={'OWNER-JOB.json':'request_sha256','OWNER-RESULT.json':'result_sha256'}[name]
                self.proof['measurements']['serving_aa'][key]=exchange.digest(raw)
            self.repin_proof()
            target=self.root/('refused-'+str(index));target.mkdir()
            with self.subTest(name=name,index=index),self.assertRaises(ValueError):
                backend.reuse_qualification('serving',target,self.binding)
            self.assertEqual(list(target.iterdir()),[])

    def test_qualification_cannot_be_sourced_from_mutable_results_or_foreign_proof(self):
        self.qualification()
        target=self.root/'reused';target.mkdir()
        original=copy.deepcopy(self.proof)
        for field,value in (('campaign_sha256','0'*64),('campaign_deadline_unix_ms',1)):
            self.proof=copy.deepcopy(original);self.proof[field]=value;self.repin_proof()
            with self.assertRaises(ValueError):backend.reuse_qualification('serving',target,self.binding)
        self.proof=copy.deepcopy(original)
        self.proof['measurements']['serving_aa']['workspace']=str(self.root/'output')
        self.repin_proof()
        with self.assertRaises(ValueError):backend.reuse_qualification('serving',target,self.binding)

    def test_real_backend_interface_and_no_persistent_lifecycle(self):
        runner = backend.OwnerBackend()
        argv = runner.build_command(python_exe="/frozen/python", config_path=Path("/config.yaml"),
                                    output_dir=Path("/output"))
        self.assertEqual(argv[0], "/frozen/python")
        self.assertEqual(Path(argv[2]), Path(backend.__file__).resolve())
        self.assertEqual(argv[3:], ["benchmark", "--benchmark-config", "/config.yaml",
                                    "--output-dir", "/output", "--run-mode", "local"])
        self.assertFalse(runner.lifecycle_eligibility({})["eligible"])
        self.assertEqual(dict(runner.boot_phase_envs), {})

    def test_pinned_binding_and_json_configuration(self):
        binding = self.root / "BINDING.json"
        raw = exchange.canonical(self.binding)
        binding.write_bytes(raw)
        self.assertEqual(backend.read_binding(binding, exchange.digest(raw)), self.binding)
        config = self.root / "config.json"
        config.write_bytes(exchange.canonical(self.config))
        self.assertEqual(backend.read_config(config), self.config)
        with self.assertRaises(ValueError):
            backend.read_binding(binding, "0" * 64)

    def test_client_preserves_real_report_and_records_one_invocation(self):
        output = self.root / "output"
        report = backend.run(self.config, output, self.binding, wait=self.reply)
        self.assertEqual(report["throughput"]["output_throughput"], 42.5)
        self.assertEqual(report["throughput"]["unit"], "tokens/s")
        self.assertEqual(exchange.decoded_json((output / "benchmark_report.json").read_bytes()), report)
        self.assertTrue((output / "OWNER-STARTED.json").exists())
        with self.assertRaises(ValueError):
            backend.run(self.config, output, self.binding, wait=self.reply)
        self.assertEqual(len(list((self.root / "requests").iterdir())), 1)

    def test_async_client_has_the_same_single_use_report_contract(self):
        async def reply(*args, **kwargs):
            await asyncio.sleep(0)
            return self.reply(*args, **kwargs)

        output = self.root / "output"
        report = asyncio.run(backend.run_async(self.config, output, self.binding, wait=reply))
        self.assertTrue(report["benchmark_valid"])
        self.assertEqual(exchange.decoded_json((output / "benchmark_report.json").read_bytes()), report)
        with self.assertRaises(ValueError):
            asyncio.run(backend.run_async(self.config, output, self.binding, wait=reply))
        self.assertEqual(len(list((self.root / "requests").iterdir())), 1)

    def test_benchmark_workspace_is_discoverable_and_cannot_be_reused(self):
        root = self.root / "output"
        report = backend.benchmark(self.config, root, self.binding, wait=self.reply)
        workspaces = list(root.glob("benchmark_*"))
        self.assertEqual(len(workspaces), 1)
        self.assertEqual(exchange.decoded_json((workspaces[0] / "benchmark_report.json").read_bytes()), report)
        with self.assertRaises(FileExistsError):
            backend.benchmark(self.config, root, self.binding, wait=self.reply)
        self.assertEqual(len(list((self.root / "requests").iterdir())), 1)

    def test_changed_fixed_env_or_model_is_refused_before_submit(self):
        for change in ({"envs": {"TP": "1"}}, {"model": "/other/model"},
                       {"precision": "fp8"}, {"workstation_workload": "c1_only"},
                       {"server_lifecycle": {"reuse": True}}):
            config = copy.deepcopy(self.config)
            config["benchmark"].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                backend.run(config, self.root / "output", self.binding, wait=self.reply)
        self.assertEqual(list((self.root / "requests").iterdir()), [])

    def test_upstream_timeout_cannot_erase_cleanup_headroom(self):
        for seconds in (600, 7220, 7540, 7659):
            config = copy.deepcopy(self.config)
            config["benchmark"]["timeout_seconds"] = seconds
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                backend.run(config, self.root / "output", self.binding, wait=self.reply)

    def test_unsupported_profiler_or_modified_capture_settings_refused(self):
        for profiler in ({"rocprof": {"enabled": True}},
                         {"torch_profiler": {"enabled": True, "steps": 500}},
                         {"torch_profiler": {"enabled": 1}}):
            config = copy.deepcopy(self.config)
            config["benchmark"]["profiler"] = profiler
            with self.subTest(profiler=profiler), self.assertRaises(ValueError):
                backend.validate_config(config, self.binding)

    def test_invalid_provenance_or_quality_never_writes_a_report(self):
        mutations = [lambda r: r.update(benchmark_backend="bypass"),
                     lambda r: r["owner_binding"].update(source_set_sha256="0" * 64),
                     lambda r: r["workstation_latency"].update(candidate_sha256="0" * 64),
                     lambda r: r["quality_gate"].update(passed=False),
                     lambda r: r.update(diagnostic_only=True)]
        for i, mutation in enumerate(mutations):
            output = self.root / ("invalid-" + str(i))
            output.mkdir()

            def wrong(job, replies, request, **kwargs):
                result = self.reply(job, replies, request, **kwargs)
                mutation(result["payload"]["benchmark_report"])
                return result

            with self.subTest(i=i), self.assertRaises(ValueError):
                backend.run(self.config, output, self.binding, wait=wrong)
            self.assertFalse((output / "benchmark_report.json").exists())
            self.assertTrue((output / "OWNER-RESULT.json").exists())

    def test_failed_submission_cannot_automatically_retry(self):
        bad = copy.deepcopy(self.binding)
        bad["sources"]["candidate.py"] = str(self.root / "missing.py")
        output = self.root / "output"
        with self.assertRaises(ValueError):
            backend.run(self.config, output, bad, wait=self.reply)
        self.assertTrue((output / "OWNER-STARTED.json").exists())
        with self.assertRaises(ValueError):
            backend.run(self.config, output, self.binding, wait=self.reply)

    def component_reply(self, job, replies, request, *, controller_sha256, useful=True):
        _, _, reply = exchange.claim(job, replies, campaign_sha256=self.binding["campaign_sha256"],
                                    selection_sha256=self.binding["selection_sha256"], controller_sha256=controller_sha256)
        report = dict(schema=1, diagnostic_only=True, serving_tested=False, adoption_authorized=False,
                      correct_both_cards=True, useful_both_cards=useful, zero_prefix_no_regression=useful,
                      eligible_for_serving=useful, summary={"fixture": True},
                      owner_binding=dict(owner_seal_sha256="f"*64, request_sha256=exchange.digest(exchange.canonical(request)),
                                         source_set_sha256=request["source_set_sha256"], controller_sha256=controller_sha256))
        return exchange.publish_result(reply, request, status="SUCCEEDED", payload={"component_report": report},
                                       owner_seal_sha256="f"*64, independent_release=True)

    def changed_source(self):
        Path(self.binding["sources"]["candidate.py"]).write_text("answer = 43\n")
        self.config["benchmark"]["timeout_seconds"] = 10570

    def test_changed_source_runs_component_before_serving_with_exact_source(self):
        self.changed_source()
        requests = []
        def reply(job, replies, request, **kwargs):
            requests.append(request)
            method = self.component_reply if request["kind"] == "component" else self.reply
            return method(job, replies, request, **kwargs)
        backend.run(self.config, self.root/"output", self.binding, wait=reply)
        self.assertEqual([r["kind"] for r in requests], ["component", "serving"])
        self.assertEqual(requests[0]["source_set_sha256"], requests[1]["source_set_sha256"])
        self.assertTrue((self.root/"output/component-screen/component_report.json").is_file())
        self.assertFalse((self.root/"output/component-screen/benchmark_report.json").exists())

    def test_rejected_component_retains_diagnostic_and_starts_no_serving(self):
        self.changed_source()
        kinds = []
        def reply(job, replies, request, **kwargs):
            kinds.append(request["kind"])
            return self.component_reply(job, replies, request, useful=False, **kwargs)
        with self.assertRaises(backend.ComponentRejected):
            backend.run(self.config, self.root/"output", self.binding, wait=reply)
        self.assertEqual(kinds, ["component"])
        self.assertFalse((self.root/"output/benchmark_report.json").exists())
        with self.assertRaises(ValueError):
            backend.run(self.config, self.root/"output", self.binding, wait=reply)

    def test_source_change_after_screen_refuses_before_serving_request(self):
        self.changed_source()
        def reply(job, replies, request, **kwargs):
            result = self.component_reply(job, replies, request, **kwargs)
            Path(self.binding["sources"]["candidate.py"]).write_text("answer = 44\n")
            return result
        with self.assertRaisesRegex(ValueError, "changed after"):
            backend.run(self.config, self.root/"output", self.binding, wait=reply)
        self.assertEqual(len(list((self.root/"requests").iterdir())), 1)

    def test_async_changed_source_also_screens_before_serving(self):
        self.changed_source()
        kinds = []
        async def reply(job, replies, request, **kwargs):
            kinds.append(request["kind"])
            method = self.component_reply if request["kind"] == "component" else self.reply
            return method(job, replies, request, **kwargs)
        asyncio.run(backend.run_async(self.config, self.root/"output", self.binding, wait=reply))
        self.assertEqual(kinds, ["component", "serving"])

    def test_component_cannot_change_geometry_mix_profiling_or_become_serving_score(self):
        config = copy.deepcopy(self.config)
        config["benchmark"].update(workstation_component={"contract":"c4-fixed-geometry-v1"},timeout_seconds=2910)
        self.assertEqual(backend.validate_config(config, self.binding), "component")
        for values in ({"workstation_component":{"contract":"other"}},
                       {"profiler":{"torch_profiler":{"enabled":True}}}, {"timeout_seconds":2250}):
            changed = copy.deepcopy(config);changed["benchmark"].update(values)
            with self.assertRaises(ValueError):backend.validate_config(changed,self.binding)

    def test_candidate_copy_race_is_refused_before_publishing_a_request(self):
        from unittest.mock import patch
        original = exchange._read
        target = Path(self.binding["sources"]["candidate.py"])
        reads = []
        def reading(path, *args, **kwargs):
            raw = original(path, *args, **kwargs)
            if Path(path) == target:
                reads.append(True)
                if len(reads) == 2:target.write_text("answer = 45\n")
            return raw
        with patch.object(exchange, "_read", side_effect=reading), self.assertRaisesRegex(ValueError, "copying"):
            backend.run(self.config,self.root/"output",self.binding,wait=self.reply)
        self.assertEqual(list((self.root/"requests").iterdir()), [])

    def test_profile_is_diagnostic_and_requires_coverage(self):
        job, request = exchange.submit(self.root / "requests", campaign_sha256="a" * 64,
            selection_sha256="b" * 64, kind="profile", config=self.config,
            sources=self.binding["sources"], caller_seconds=7160,
            campaign_deadline_unix_ms=self.binding["campaign_deadline_unix_ms"])
        result = self.reply(job, self.root / "replies", request, controller_sha256="c" * 64)
        report = result["payload"]["benchmark_report"]
        report.update(diagnostic_only=True, workstation_latency=None, workstation_latency_control=None,
                      profile_coverage={"complete": True, "owner_seal_sha256": "f" * 64})
        self.assertEqual(backend.validate_report(result, request, self.binding), report)
        report["profile_coverage"]["complete"] = False
        with self.assertRaises(ValueError):
            backend.validate_report(result, request, self.binding)


if __name__ == "__main__":
    unittest.main()
