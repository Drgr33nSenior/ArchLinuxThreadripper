"""Native accounting and refusal checks; no model, GPU or provider required."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib/workstation"))
import hyperloom_sglang as bridge


def summary():
    rows = []
    for ordinal in range(3):
        rows.append({"ordinal": ordinal, "wire": {"status": 200, "primary_failure": None,
                     "response_truncated": False}, "contract": {"usage": {"completion_tokens": 3,
                     "prompt_tokens": 2}, "response_token_ids": [8, 4, 2], "prompt_token_ids": [5, 6],
                     "id_count_equals_reported_count": True, "client_elapsed_seconds": 1.0}})
    return {"schema": 1, "mode": "native-http-fp8-sprint01-v1", "stopped_early": False,
            "paired_error": None, "qualification": False, "rows": rows,
            "executed_case_count": 3, "frozen_case_count": 3,
            "groups": [{"group_id": "warmup", "measured": False, "kind": "warmup"},
                       {"group_id": "timing-c2-1", "measured": True, "kind": "timing",
                        "excluded_warmup": False, "ordinals": [1, 2], "concurrency": 2,
                        "prepared_monotonic": 10.0, "received_monotonic": 12.0,
                        "makespan_seconds": 2.0, "completion_tokens": 6,
                        "combined_tp2_tokens_per_second": 3.0}]}


class NativeBridgeTests(unittest.TestCase):
    def test_counts_real_ids_and_keeps_http_wall_scope(self):
        rows = bridge.measurements(summary())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["total_output_tokens"], 6)
        self.assertEqual(rows[0]["total_input_tokens"], 4)
        self.assertEqual(rows[0]["output_throughput"], 3)
        self.assertEqual(rows[0]["completed"], 2)
        self.assertEqual(rows[0]["ordinals"], [1, 2])

    def test_bad_counts_windows_warmups_and_responses_refuse(self):
        mutations = [
            lambda s: s["rows"][1]["contract"]["response_token_ids"].pop(),
            lambda s: s["rows"][1]["contract"]["prompt_token_ids"].append(7),
            lambda s: s["rows"][1]["wire"].update(status=500),
            lambda s: s["rows"][1]["wire"].update(response_truncated=True),
            lambda s: s.update(stopped_early=True),
            lambda s: s.update(executed_case_count=2),
            lambda s: s["groups"][1].update(excluded_warmup=True),
            lambda s: s["groups"][1].update(makespan_seconds=float("nan")),
            lambda s: s["groups"][1].update(received_monotonic=14),
            lambda s: s["groups"][1].update(completion_tokens=9),
            lambda s: s["groups"][1].update(combined_tp2_tokens_per_second=9),
            lambda s: s["groups"][1].update(ordinals=[1, 1]),
            lambda s: s["groups"].append(dict(s["groups"][1], group_id="reused")),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                value = summary()
                mutate(value)
                with self.assertRaises(ValueError):
                    bridge.measurements(value)

    def test_evidence_substitution_duplicate_keys_and_symlinks_refuse(self):
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / "evidence.json"
            file.write_text('{"a":1,"a":2}')
            sha = hashlib.sha256(file.read_bytes()).hexdigest()
            with self.assertRaises(ValueError):
                bridge.checked_json(file, sha)
            file.write_text('{"a":1}')
            with self.assertRaises(ValueError):
                bridge.checked_json(file, sha)
            link = Path(temp) / "link"
            link.symlink_to(file)
            with self.assertRaises(ValueError):
                bridge.checked_json(link, hashlib.sha256(file.read_bytes()).hexdigest())

    def test_stale_stack_refuses_before_output_or_upstream_calls(self):
        profile = json.loads((ROOT / "infrastructure/hyperloom/sglang-rocm102.json").read_text())
        with tempfile.TemporaryDirectory() as temp:
            proof = Path(temp) / "proof.json"
            observed = {key: profile[key] for key in ("sdk_version", "torch_version", "hip_version", "sdk_manifest_sha256")}
            observed["sdk_version"] = "10.1.0"
            proof.write_text(json.dumps({"result": observed}))
            sha = hashlib.sha256(proof.read_bytes()).hexdigest()
            output = Path(temp) / "output"
            with self.assertRaisesRegex(ValueError, "stale stack"):
                bridge.export(profile, proof, sha, "unused", "unused", output, None)
            self.assertFalse(output.exists())

    def test_report_import_cannot_promote_or_invent_latency(self):
        profile = json.loads((ROOT / "infrastructure/hyperloom/sglang-rocm102.json").read_text())
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            proof = root / "proof.json"
            native = root / "native.json"
            proof.write_text(json.dumps({"result": profile}))
            claimed_qualified = summary()
            claimed_qualified["qualification"] = True
            native.write_text(json.dumps(claimed_qualified))
            proof_sha = hashlib.sha256(proof.read_bytes()).hexdigest()
            native_sha = hashlib.sha256(native.read_bytes()).hexdigest()

            def writer(workspace, report):
                (workspace / "benchmark_report.json").write_text(json.dumps(report))

            upstream = SimpleNamespace(ReportSubject=lambda *args: args,
                RunOutcome=lambda *args, **kwargs: kwargs,
                build_report=lambda *args, **kwargs: {"latency": {"ttft": {}, "tpot": {}, "itl": {}, "e2el": {}}},
                write_report=writer)
            output = root / "output"
            result = bridge.export(profile, proof, proof_sha, native, native_sha, output, upstream)
            self.assertTrue(result["native_qualification"])
            self.assertFalse(result["model_identity_verified"])
            self.assertFalse(result["cross_run_stack_binding_verified"])
            self.assertEqual(result["excluded_groups"][0]["group_id"], "warmup")
            report = json.loads((output / "group-0/benchmark_report.json").read_text())
            self.assertFalse(report["quality_gate"]["passed"])
            self.assertFalse(report["qualification"])
            self.assertIsNone(report["latency"]["ttft"])
            self.assertIsNone(report["decode_throughput"])
            with self.assertRaisesRegex(ValueError, "fresh absolute output"):
                bridge.export(profile, proof, proof_sha, native, native_sha, output, upstream)

    def test_automatic_or_standalone_configuration_refuses(self):
        profile = json.loads((ROOT / "infrastructure/hyperloom/sglang-rocm102.json").read_text())
        for key in ("automatic_optimization", "standalone_launch"):
            invalid = dict(profile, **{key: True})
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "unsupported bridge configuration"):
                bridge.export(invalid, "unused", "unused", "unused", "unused", "unused", None)


class FixedC1Tests(unittest.TestCase):
    def fixture(self):
        raw = {"schema": 1, "mode": "native-http-fp8-sprint01-v1", "qualification": False,
               "frozen_case_count": 4, "executed_case_count": 4, "stopped_early": False, "rows": []}
        settings = {"performance_benchmark": {"mode": "m2047-fixed1024-c1-v1",
                    "warmup_requests": 1, "measured_requests": 3}, "requests": []}
        intents = []
        for i in range(4):
            case = "perf-warmup" if i == 0 else "perf-sample-" + str(i)
            pin = str(i) * 64
            row = copy.deepcopy(summary()["rows"][0])
            row.update(ordinal=i, case=case, body_sha256=pin)
            row["wire"].update(response_observed=True, received_monotonic=12.0+3*i, client_elapsed_seconds=2.0)
            row["contract"].update(client_elapsed_seconds=2.0, prompt_token_ids=list(range(2047)),
                response_token_ids=list(range(1024)), usage={"prompt_tokens": 2047, "completion_tokens": 1024})
            raw["rows"].append(row)
            settings["requests"].append({"ordinal": i, "case": case, "sha256": pin})
            intents.append({"ordinal": i, "case": case, "body_sha256": pin, "prepared_monotonic": 10.0+3*i})
        return raw, settings, intents

    def test_authenticated_request_walls_exclude_frozen_warmup_without_changing_summary(self):
        raw, settings, intents = self.fixture()
        original = copy.deepcopy(raw)
        projected = bridge.fixed_c1_projection(raw, settings, intents)
        measured = bridge.measurements(projected)
        self.assertEqual(raw, original)
        self.assertNotIn("c2_clock", projected)
        self.assertEqual([r["ordinals"] for r in measured], [[1], [2], [3]])
        self.assertEqual([r["output_throughput"] for r in measured], [512.0]*3)
        self.assertTrue(projected["groups"][0]["excluded_warmup"])

    def test_changed_clock_identity_counts_warmup_or_profile_refuse(self):
        changes = [lambda r,s,i: i[1].update(prepared_monotonic=9.0),
                   lambda r,s,i: i[1].update(body_sha256="f"*64),
                   lambda r,s,i: i[1].update(case="other"),
                   lambda r,s,i: r["rows"][1]["contract"]["response_token_ids"].pop(),
                   lambda r,s,i: r["rows"][1]["contract"].update(client_elapsed_seconds=1.0),
                   lambda r,s,i: r["rows"][0]["wire"].update(status=500),
                   lambda r,s,i: s["performance_benchmark"].update(mode="other"),
                   lambda r,s,i: s["performance_benchmark"].update(warmup_requests=0),
                   lambda r,s,i: r.update(groups=[])]
        for change in changes:
            raw, settings, intents = self.fixture()
            change(raw, settings, intents)
            with self.subTest(change=change), self.assertRaises(ValueError):
                bridge.fixed_c1_projection(raw, settings, intents)

    def test_older_readout_enrichment_requires_exact_raw_response_and_all_other_fields(self):
        raw, _, _ = self.fixture()
        responses, readout = [], []
        for row in raw["rows"]:
            body = json.dumps({"sglext": {"spec_tokens_details": {"fixture": row["ordinal"]}}}).encode()
            pin = hashlib.sha256(body).hexdigest()
            row["wire"]["response_sha256"] = pin
            row["contract"]["raw_http_response_sha256"] = pin
            checked = copy.deepcopy(row["contract"])
            checked.pop("raw_http_response_sha256")
            checked["spec_tokens_details"] = {"fixture": row["ordinal"]}
            responses.append(body)
            readout.append(checked)
        bridge.fixed_c1_readout(raw["rows"], readout, responses)
        changed = copy.deepcopy(readout)
        changed[1]["spec_tokens_details"]["fixture"] = 99
        with self.assertRaises(ValueError):
            bridge.fixed_c1_readout(raw["rows"], changed, responses)
        changed = copy.deepcopy(readout)
        changed[1]["client_elapsed_seconds"] = 99
        with self.assertRaises(ValueError):
            bridge.fixed_c1_readout(raw["rows"], changed, responses)
        with self.assertRaises(ValueError):
            bridge.fixed_c1_readout(raw["rows"], readout, [*responses[:3], b"substitution"])


class OwnerBindingTests(unittest.TestCase):
    """Synthetic owner envelope around the actual retained parsed config."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = json.loads((ROOT / "infrastructure/hyperloom/sglang-rocm102.json").read_text())

    def put(self, name, value):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(value, sort_keys=True).encode()
        target.write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()

    def get(self, name):
        return json.loads((self.root / name).read_text())

    def seal(self):
        files = sorted(p for p in self.root.rglob("*") if p.is_file() and p.name != "FINAL-ARTIFACTS.sha256")
        text = "".join(hashlib.sha256(p.read_bytes()).hexdigest() + "  " + p.relative_to(self.root).as_posix() + "\n"
                       for p in files)
        (self.root / "FINAL-ARTIFACTS.sha256").write_text(text)
        return hashlib.sha256(text.encode()).hexdigest()

    def fixture(self, configure=None):
        parsed = json.loads((ROOT / "tests/fixtures/hyperloom/parsed-configuration.json").read_text())
        if configure:
            configure(parsed)
        session, source = "a" * 64, "b" * 64
        boot = "00000000-0000-0000-0000-000000000001"
        revision = parsed["canonical_server_cli"][parsed["canonical_server_cli"].index("--revision") + 1]
        native = summary()
        native["c2_clock"] = {"started": 9.0, "finished": 14.0}
        for row in native["rows"]:
            row.update(case="case-" + str(row["ordinal"]), body_sha256=str(row["ordinal"]) * 64)
            row["contract"].update(request_sha256=row["body_sha256"])
        settings = {"requests": [{"ordinal": r["ordinal"], "case": r["case"], "sha256": r["body_sha256"]}
                                  for r in native["rows"]]}
        files = {}
        files["review/FINAL-SETTINGS.json"] = self.put("review/FINAL-SETTINGS.json", settings)
        candidate = {"version": self.profile["torch_version"], "hip_version": self.profile["hip_version"],
                     "sdk": {"version": self.profile["sdk_version"],
                             "manifest": {"sha256": self.profile["sdk_manifest_sha256"]}}}
        files["review/SERVING-STACK.json"] = self.put("review/SERVING-STACK.json", {"torch_candidate": candidate})
        sources = {"asset-" + name.rsplit("/", 1)[1]: {"path": "/mnt/" + name, "sha256": sha}
                   for name, sha in files.items()}
        config = {"binding_template": {"arm": "control", "session_sha256": None, "spec_sha256": None,
                   "source_manifest_sha256": source, "sources": sources,
                   "canonical_server_cli": parsed["canonical_server_cli"], "resolved_contract": parsed["resolved_contract"]},
                  "worker_template": {"arm_config": parsed["arm_config"]}}
        config_sha = self.put("review/HTTP-BINDING-control.json", config)
        spec = {"http_serving": {"bindings": {"control": {"path": "review/HTTP-BINDING-control.json", "sha256": config_sha}}},
                "server_args": {"revision": revision}}
        files["review/BENCHMARK-SPEC2.json"] = self.put("review/BENCHMARK-SPEC2.json", spec)
        spec_sha = files["review/BENCHMARK-SPEC2.json"]
        files["review/COMMON-STACK-RECEIPT.json"] = self.put("review/COMMON-STACK-RECEIPT.json",
            {"target": {"sglang": self.profile["framework_version"]}})
        files["review/MODEL-INVENTORY.json"] = self.put("review/MODEL-INVENTORY.json",
            [{"name": "config.json", "sha256": parsed["resolved_contract"]["model_config_sha256"]}])
        self.put("MANIFEST.json", {"session_sha256": session, "spec_sha256": spec_sha, "package_files": files,
                 "boot_id": boot, "model_root": "/models/" + revision, "model_inventory_file": "review/MODEL-INVENTORY.json"})
        stamp = {"session_sha256": session, "spec_sha256": spec_sha, "source_manifest_sha256": source}
        workers = [dict(stamp, arm="control", rank=i, boot_id=boot, pid=20+i, start_ticks=100+i,
                        identity_sha256=str(i)*64, sources=copy.deepcopy(sources), arm_config=copy.deepcopy(parsed["arm_config"]))
                   for i in range(2)]
        gate = dict(stamp, arm="control", post_warmup_native_ready=True, workers=[{k: w[k]
                    for k in ("rank", "boot_id", "pid", "start_ticks", "identity_sha256")} for w in workers])
        result = dict(stamp, arm="bf16", logical_arm="control", status="HTTP_ARM_COMPLETE", parent_error=None,
                      native_gate=gate, client_summary=copy.deepcopy(native))
        summary_bytes = json.dumps(native, sort_keys=True).encode()
        self.put("results/bf16-http-client-tree/RUN-SUMMARY.json", native)
        artifacts = {"result.json": result, "native-http-bundle.json": {"workers": workers},
                     "http-client-bundle.json": {"files": {"RUN-SUMMARY.json": {
                         "bytes": len(summary_bytes), "sha256": hashlib.sha256(summary_bytes).hexdigest(),
                         "base64": base64.b64encode(summary_bytes).decode()}}}}
        members = {}
        for name, value in artifacts.items():
            members[name] = {"exists": True, "sha256": self.put("results/bf16-artifacts/" + name, value)}
        release = {"session_sha256": session, "boot_id": boot, "failures": [],
                   "authenticated_processes_present": [], "allocation": {"gpu_requests": 0,
                       "scope": "all-namespaces-active-workloads"}, "observed_monotonic": 15.0,
                   "holders": {"exit_code": 1, "stdout": "", "stderr": "", "visibility": "all-users-platform",
                               "visibility_contract": "native-host-root-fuser-prerequisites-checked"},
                   "root_fuser_prerequisites": {"passed": True},
                   "target": {"hostname": "threadripper-ai", "uid": 1000, "boot_id": boot},
                   "root_resource_release": {"scopes": [{"arm": "bf16", "absent": True}]}}
        controller = {"scope": "matched-physical-model-benchmark-v2", "status": "MATCHED_PHYSICAL_BENCHMARK_COMPLETE",
                      "native_run": True, "failure": None,
                      "artifact_receipts": {"bf16": {"transport_valid": True, "all_members_present": True,
                          "binding": dict(stamp, arm="bf16"), "members": members}},
                      "http_serving": {"bf16": {"binding": dict(stamp, arm="control"), "rows": copy.deepcopy([r["contract"] for r in native["rows"]]),
                          "status": "SERVING_SCREEN_COMPLETE", "wire_complete": True, "qualification": False,
                          "code_correctness_graded": False}},
                      "campaign": {"independent_release_verified": True, "cleanup_errors": [], "unresolved": False,
                                   "independent_postcheck": release},
                      "resource_receipts": [{"final": {"arm": "bf16", "session_sha256": session,
                          "benchmark_spec_sha256": spec_sha, "complete": True, "child_exit": 0,
                          "child_reaped": True, "resource_boundaries_clear": True}}]}
        self.put("results/CONTROLLER-RESULT.json", controller)
        return self.seal()

    def change(self, name, mutate):
        value = self.get(name)
        mutate(value)
        sha = self.put(name, value)
        if name.startswith("results/bf16-artifacts/"):
            controller = self.get("results/CONTROLLER-RESULT.json")
            controller["artifact_receipts"]["bf16"]["members"][name.rsplit("/", 1)[1]]["sha256"] = sha
            self.put("results/CONTROLLER-RESULT.json", controller)
        return self.seal()

    def test_one_owner_run_binds_actual_parsed_configuration(self):
        sha = self.fixture()
        native, summary_sha, binding = bridge.owner_run(self.profile, self.root, sha, "bf16")
        parsed = json.loads((ROOT / "tests/fixtures/hyperloom/parsed-configuration.json").read_text())
        self.assertEqual(binding["parsed_configuration"], parsed["resolved_contract"])
        self.assertEqual(summary_sha, hashlib.sha256((self.root / "results/bf16-http-client-tree/RUN-SUMMARY.json").read_bytes()).hexdigest())
        self.assertEqual(len(native["rows"]), 3)
        self.assertTrue(binding["model_identity_bound"])
        self.assertTrue(binding["independent_release_bound"])
        self.assertFalse(binding["native_quality"]["qualification"])
        self.assertFalse(binding["qualification"])

    def test_runtime_null_or_foreign_session_and_other_arm_refuse(self):
        for value in (None, "f" * 64):
            self.fixture()
            sha = self.change("results/bf16-artifacts/result.json", lambda r: r.update(session_sha256=value))
            with self.subTest(session=value), self.assertRaisesRegex(ValueError, "cross-run"):
                bridge.owner_run(self.profile, self.root, sha, "bf16")
        with self.assertRaises(ValueError):
            bridge.owner_run(self.profile, self.root, sha, "kv4")

    def test_worker_source_configuration_and_coverage_substitution_refuse(self):
        mutations = [lambda r: r["workers"][0]["sources"].update(unknown={"sha256": "0"*64}),
                     lambda r: r["workers"][0].pop("source_manifest_sha256"),
                     lambda r: r["workers"][0]["arm_config"].update(disable_cuda_graph=True),
                     lambda r: r["workers"].__setitem__(1, copy.deepcopy(r["workers"][0]))]
        for mutate in mutations:
            self.fixture()
            sha = self.change("results/bf16-artifacts/native-http-bundle.json", mutate)
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                bridge.owner_run(self.profile, self.root, sha, "bf16")

    def test_failed_owner_or_uncertain_release_refuses(self):
        mutations = [lambda c: c.update(status="FAILED"),
                     lambda c: c["campaign"].update(independent_release_verified=False),
                     lambda c: c["campaign"]["independent_postcheck"]["allocation"].update(gpu_requests=1),
                     lambda c: c["campaign"]["independent_postcheck"]["holders"].update(stderr="visibility failed"),
                     lambda c: c["campaign"]["independent_postcheck"]["holders"].update(visibility="container-only"),
                     lambda c: c["campaign"]["independent_postcheck"].update(observed_monotonic=13.0),
                     lambda c: c["campaign"]["independent_postcheck"]["root_resource_release"]["scopes"][0].update(absent=False),
                     lambda c: c["resource_receipts"][0]["final"].update(child_reaped=False)]
        for mutate in mutations:
            self.fixture()
            sha = self.change("results/CONTROLLER-RESULT.json", mutate)
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                bridge.owner_run(self.profile, self.root, sha, "bf16")

    def test_native_summary_substitution_and_invalid_parsed_tp_refuse(self):
        self.fixture()
        sha = self.change("results/bf16-http-client-tree/RUN-SUMMARY.json", lambda r: r.update(qualification=True))
        with self.assertRaisesRegex(ValueError, "summary/readout substitution"):
            bridge.owner_run(self.profile, self.root, sha, "bf16")
        sha = self.fixture(lambda p: p["resolved_contract"].update(tp_size=1))
        with self.assertRaisesRegex(ValueError, "placement mismatch"):
            bridge.owner_run(self.profile, self.root, sha, "bf16")

    def test_stale_stack_and_artifact_bytes_refuse(self):
        sha = self.fixture()
        stale = dict(self.profile, torch_version="old")
        with self.assertRaisesRegex(ValueError, "stale bound stack"):
            bridge.owner_run(stale, self.root, sha, "bf16")
        (self.root / "results/CONTROLLER-RESULT.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "identity changed"):
            bridge.owner_run(self.profile, self.root, sha, "bf16")

    def test_seal_traversal_and_symlink_ancestors_refuse(self):
        self.fixture()
        seal = self.root / "FINAL-ARTIFACTS.sha256"
        seal.write_text("a" * 64 + "  ../outside.json\n")
        with self.assertRaisesRegex(ValueError, "invalid owner seal"):
            bridge.owner_run(self.profile, self.root, hashlib.sha256(seal.read_bytes()).hexdigest(), "bf16")
        sha = self.fixture()
        (self.root / "review").rename(self.root / "real-review")
        (self.root / "review").symlink_to(self.root / "real-review", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            bridge.owner_run(self.profile, self.root, sha, "bf16")


class QualityJoinTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.file = Path(self.temp.name) / "quality.json"
        self.binding = {"seal_sha256": "a" * 64, "logical_arm": "control",
                        "verified_members": {"results/CONTROLLER-RESULT.json": "b" * 64}}
        cases = [(15, "quality-c1-a"), (16, "quality-c1-b"), (17, "quality-c2-a"), (18, "quality-c2-b"),
                 (21, "prefix-branch"), (28, "raw-tool-json"), (29, "sampled-running-totals")]
        self.native = {"rows": [{"ordinal": ordinal, "case": case} for ordinal, case in cases]}
        self.quality = {"status": "QUALITY_PASS", "qualification": False, "selected": False,
            "actual_input_sha256": "c" * 64, "final_Astra_review_deferred": True,
            "parent_readout": {"parent_authentication": {"remote_seals": {"workload": {"manifest_sha256": "a" * 64}},
                "controller_result_sha256": "b" * 64, "actual_transport_and_release_verified": True},
                "all_timed_work_matched": False},
            "summary": {"status": "NATIVE_CPU_FINAL_QUALITY_AND_RELEASE_VALIDATED", "quality_pass": True,
                "independent_release_verified": True, "native_generation_performed": False,
                "sampling_distribution_verified": False, "actual_sampled_verification": "NOT PROVEN",
                "grades": [{"arm": arm, "ordinal": ordinal, "case": case, "quality_passed": True, "status": "QUALITY_PASS"}
                           for arm in ("control", "mtp") for ordinal, case in cases]}}

    def write(self):
        self.file.write_text(json.dumps(self.quality))
        return hashlib.sha256(self.file.read_bytes()).hexdigest()

    def test_pass_binds_graded_cases_and_retains_limits(self):
        result = bridge.quality_join(self.binding, self.native, self.file, self.write())
        self.assertTrue(result["task_checks_passed"])
        self.assertEqual(len(result["grades"]), 7)
        self.assertEqual(result["total_graded_answers"], 14)
        self.assertFalse(result["qualification"])
        self.assertFalse(result["sampling_distribution_verified"])
        self.assertFalse(result["all_timed_work_matched"])
        self.assertTrue(result["final_review_deferred"])

    def test_foreign_run_failed_grade_and_incomplete_coverage_refuse(self):
        changes = [lambda q: q["parent_readout"]["parent_authentication"].update(controller_result_sha256="d" * 64),
                   lambda q: q["parent_readout"]["parent_authentication"]["remote_seals"]["workload"].update(manifest_sha256="d" * 64),
                   lambda q: q["summary"]["grades"].pop(),
                   lambda q: q["summary"]["grades"].__setitem__(-1, copy.deepcopy(q["summary"]["grades"][0])),
                   lambda q: q["summary"]["grades"][-1].update(quality_passed=False),
                   lambda q: q["summary"].update(independent_release_verified=False),
                   lambda q: q["summary"].update(native_generation_performed=True),
                   lambda q: q.update(status="NOT RUN")]
        original = copy.deepcopy(self.quality)
        for mutate in changes:
            self.quality = copy.deepcopy(original)
            mutate(self.quality)
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                bridge.quality_join(self.binding, self.native, self.file, self.write())

    def test_changed_bytes_missing_case_and_unpaired_flags_refuse(self):
        sha = self.write()
        self.file.write_text("{}")
        with self.assertRaisesRegex(ValueError, "identity changed"):
            bridge.quality_join(self.binding, self.native, self.file, sha)
        self.native["rows"].pop()
        with self.assertRaisesRegex(ValueError, "task coverage"):
            bridge.quality_join(self.binding, self.native, self.file, self.write())
        profile = json.loads((ROOT / "infrastructure/hyperloom/sglang-rocm102.json").read_text())
        with self.assertRaisesRegex(ValueError, "required together"):
            bridge.export_bound(profile, "unused", "unused", "bf16", "unused", None, quality_path=self.file)


if __name__ == "__main__":
    unittest.main()
