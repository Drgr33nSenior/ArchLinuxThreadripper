"""Synthetic offline graph contracts; these tests do not qualify a GPU/model."""

import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib/workstation"))
import graph_profile as graph

NOW = datetime(2026, 10, 8, 13, 0, tzinfo=timezone.utc)


def fixtures(now=NOW):
    pinned = graph.profile()
    args = []
    for option, value in pinned["settings"].items():
        args += [option] if value is True else [option, value]
    args += ["--cuda-graph-config", json.dumps(graph.graph_config(1)), "--disable-cuda-graph-padding"]
    container = {"name": "sglang", "image": pinned["image"], "imagePullPolicy": "Never",
        "command": ["/opt/workstation/sglang/bin/python3", "-m", "sglang.launch_server"], "args": args,
        "env": [{"name": name, "value": value} for name, value in graph.ENVIRONMENT.items()] + [{"name": "TRACE_LABEL", "value": "synthetic-preserved"}],
        "resources": {"requests": copy.deepcopy(pinned["resources"]), "limits": copy.deepcopy(pinned["resources"])},
        "securityContext": {"readOnlyRootFilesystem": True, "allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
        "volumeMounts": [{"name": "cache", "mountPath": "/cache"}, {"name": "models", "mountPath": "/models", "readOnly": True},
                         {"name": "notes", "mountPath": "/notes", "readOnly": True}]}
    security = {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000, "fsGroup": 1000, "seccompProfile": {"type": "RuntimeDefault"}}
    deployment = {"kind": "Deployment", "spec": {"replicas": 0, "template": {
        "metadata": {"labels": {"workstation-serving": "baseline"}, "annotations": {"fixture": "preserve"}},
        "spec": {"automountServiceAccountToken": False, "securityContext": security, "containers": [container],
                 "volumes": [{"name": "cache", "hostPath": {"path": "/home/developer/fresh-fixture/cache", "type": "Directory"}},
                             {"name": "models", "hostPath": {"path": "/home/developer/pinned-models", "type": "Directory"}},
                             {"name": "notes", "configMap": {"name": "unrelated-notes"}}]}}}}
    pod = {"metadata": {"uid": "11111111-2222-3333-4444-555555555555", "namespace": "default", "labels": {"workstation-serving": "baseline"}},
           "spec": copy.deepcopy(deployment["spec"]["template"]["spec"])}
    pod["spec"]["nodeName"] = "threadripper-ai"
    devices = [{"index": 0, "uuid": pinned["devices"][0]["uuid"], "bdf": pinned["devices"][0]["bdf"], "vram_bytes": 34208743424},
               {"index": 1, "uuid": pinned["devices"][1]["uuid"], "bdf": pinned["devices"][1]["bdf"], "vram_bytes": 34208743424}]
    effective = {**copy.deepcopy(pinned["effective"]), "precision": copy.deepcopy(pinned["precision"]), "cuda_graph_config": {
        "decode": {**graph.graph_config(1)["decode"], "tc_compiler": "eager", "full_prefill_max_req": None, "full_prefill_prefix_chunk_tokens": None},
        "prefill": {"backend": "disabled", "tc_compiler": "eager"}}}
    observation = {"preflight": {"observed_at": now.isoformat().replace("+00:00", "Z"), "boot_id": pinned["target"]["boot_id"],
        "kernel": pinned["target"]["kernel_release"], "node": {"name": "threadripper-ai", "ready": True},
        "swap": False, "active_gpu_allocations": 0, "device_holders": 0, "session_policy_present": False},
        "pod": pod, "image": {"images": [{"id": pinned["config_digest"], "repoDigests": [pinned["image"]]}]},
        "runtime_versions": copy.deepcopy(pinned["runtime"]), "source_before": copy.deepcopy(pinned["source_pins"]),
        "source_after": copy.deepcopy(pinned["source_pins"]), "model_before": copy.deepcopy(pinned["model"]["artifacts"]),
        "model_after": copy.deepcopy(pinned["model"]["artifacts"]),
        "compiler_identity": {"before": copy.deepcopy(pinned["compiler"]), "after": copy.deepcopy(pinned["compiler"]), "unchanged": True},
        "torch_graphs_before": {"identity": copy.deepcopy(pinned["torch_graphs"])}, "torch_graphs_after": {"identity": copy.deepcopy(pinned["torch_graphs"])},
        "singleton_topology": {"tp_size": 1, "dp_size": 1, "pp_size": 1, "attn_cp_size": 1, "moe_dp_size": 1, "ep_size": 1},
        "launch": {"devices": devices, "selected": devices[0]}, "resolved_contract": effective,
        "policy_before": {"uid": pinned["policy"]["uid"], "spec": {"podSelector": {"matchLabels": {"workstation-serving": "baseline"}}, "policyTypes": ["Ingress", "Egress"]}},
        "effective_policy_before": [{"name": pinned["policy"]["name"], "uid": pinned["policy"]["uid"], "ingress_rules": 0, "egress_rules": 0}]}
    workload = {"schema": 1, "profile": "batch", "model_revision": pinned["model"]["revision"], "prefix_state": "new-prefix",
                "concurrency": [2, 4], "repetitions": 3, "requests_per_worker": 1,
                "cases": [{"input_ids": [61037] + [1] * 511, "output_tokens": 128, "context_tokens": 4096, "source_sha256": "f" * 64}]}
    _, paths = graph.cache_paths(pinned, observation, workload)
    observation["caches"] = {name: {"root": path, "files": {}, "bytes": 0} for name, path in paths.items()}
    return deployment, workload, observation


def apply_patch(document, operations):
    result = copy.deepcopy(document)
    for operation in operations:
        names = operation["path"].strip("/").split("/")
        parent = result
        for name in names[:-1]:
            parent = parent[name]
        if operation["op"] == "test":
            if parent[names[-1]] != operation["value"]:
                raise ValueError("JSON Patch guard failed")
        elif operation["op"] == "replace":
            parent[names[-1]] = copy.deepcopy(operation["value"])
        else:
            raise AssertionError("unexpected patch operation")
    return result


class GraphProfileTests(unittest.TestCase):
    def setUp(self):
        self.deployment, self.workload, self.observation = fixtures()

    def result(self, **kwargs):
        return graph.plan(self.deployment, self.workload, self.observation, experimental=True, now=NOW, **kwargs)

    def changed_pod(self):
        self.observation["pod"]["spec"] = {**copy.deepcopy(self.deployment["spec"]["template"]["spec"]), "nodeName": "threadripper-ai"}

    def test_exact_graph_only_change_and_reverse_preserve_every_other_field(self):
        before = copy.deepcopy((self.deployment, self.workload, self.observation))
        result = self.result()
        self.assertEqual(result["selected"], "bf16-full-b4")
        self.assertEqual(result["fallback_reasons"], [])
        self.assertEqual(result["cache_admission"]["status"], "NOT RUN")
        self.assertFalse(result["whole_model_qualified"])
        changed = apply_patch(self.deployment, result["patch"])
        self.assertEqual(apply_patch(changed, result["rollback"]), self.deployment)
        target = changed["spec"]["template"]["spec"]["containers"][0]
        original = self.deployment["spec"]["template"]["spec"]["containers"][0]
        index = target["args"].index("--cuda-graph-config")
        self.assertEqual(json.loads(target["args"][index + 1]), graph.graph_config(4))
        target["args"][index + 1] = original["args"][index + 1]
        target["env"] = original["env"]
        self.assertEqual(changed, self.deployment)
        self.assertEqual(before, (self.deployment, self.workload, self.observation))
        self.assertEqual(target["args"][target["args"].index("--max-running-requests") + 1], "4")

    def test_rollback_guards_subsequent_network_metadata_and_unrelated_mount_changes(self):
        result = self.result()
        for field in ("labels", "annotations"):
            changed = apply_patch(self.deployment, result["patch"])
            changed["spec"]["template"]["metadata"][field]["new"] = "do-not-erase"
            with self.assertRaises(ValueError):
                apply_patch(changed, result["rollback"])
        changed = apply_patch(self.deployment, result["patch"])
        changed["spec"]["template"]["spec"]["volumes"].append({"name": "new", "emptyDir": {}})
        with self.assertRaises(ValueError):
            apply_patch(changed, result["rollback"])

    def test_explicit_experimental_required(self):
        with self.assertRaisesRegex(ValueError, "experimental"):
            graph.plan(self.deployment, self.workload, self.observation, now=NOW)

    def test_missing_stale_source_model_image_runtime_and_compiler_fallback(self):
        mutations = [lambda o: o.pop("source_after"), lambda o: o["source_before"].update({"srt/models/qwen3.py": "0" * 64}),
            lambda o: o["source_before"].update({"extra-DP-source.py": "1" * 64}), lambda o: o["model_after"].pop(),
            lambda o: o["image"]["images"][0].update(id="sha256:" + "0" * 64),
            lambda o: o["runtime_versions"].update(torch="different"),
            lambda o: o["compiler_identity"]["after"].update(sha256="0" * 64),
            lambda o: o["torch_graphs_after"]["identity"]["math_flags"].update(tunableop_enabled=True),
            lambda o: o["resolved_contract"].update(enable_fp32_lm_head=True),
            lambda o: o["resolved_contract"]["precision"].update(kv="FP8"),
            lambda o: o["singleton_topology"].update(dp_size=2)]
        original = copy.deepcopy(self.observation)
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                self.observation = copy.deepcopy(original)
                mutate(self.observation)
                result = self.result()
                self.assertEqual(result["selected"], "incumbent")
                self.assertTrue(result["fallback_reasons"])
                self.assertEqual(apply_patch(self.deployment, result["patch"]), self.deployment)

    def test_stale_boot_and_policy_require_new_owner_evidence(self):
        original = copy.deepcopy(self.observation)
        for mutate in (lambda o: o["preflight"].update(observed_at=(NOW - timedelta(minutes=11)).isoformat()),
                       lambda o: o["preflight"].update(boot_id="new-boot"),
                       lambda o: o["effective_policy_before"].append({"name": "another", "ingress_rules": 1}),
                       lambda o: o["preflight"].update(session_policy_present=True)):
            self.observation = copy.deepcopy(original)
            mutate(self.observation)
            self.assertEqual(self.result()["selected"], "incumbent")

    def test_every_cache_namespace_must_be_empty_and_current_or_refuse(self):
        original = copy.deepcopy(self.observation)
        for name in graph.CACHE_NAMES:
            for state in ({"root": "/cache/stale", "files": {}, "bytes": 0}, None,
                          {**original["caches"][name], "bytes": False}, {**original["caches"][name], "bytes": 0.0},
                          {**original["caches"][name], "files": {"kernel": {"sha256": "a" * 64}}, "bytes": 1}):
                self.observation = copy.deepcopy(original)
                if state is None:
                    self.observation["caches"].pop(name)
                else:
                    self.observation["caches"][name] = state
                result = self.result()
                self.assertEqual(result["selected"], "incumbent")
                self.assertIsNotNone(result["conditional_patch"])
                self.assertEqual(apply_patch(self.deployment, result["patch"]), self.deployment)

    def test_historical_receipts_remain_historical_and_conditional_not_selected(self):
        self.observation["preflight"]["observed_at"] = "2026-10-08T08:15:18Z"
        self.observation.pop("caches")
        before = copy.deepcopy(self.observation)
        result = self.result()
        self.assertEqual(result["selected"], "incumbent")
        self.assertIsNotNone(result["conditional_patch"])
        self.assertIn("NOT SELECTED", result["conditional_status"])
        proposed = apply_patch(self.deployment, result["conditional_patch"])
        args = proposed["spec"]["template"]["spec"]["containers"][0]["args"]
        self.assertEqual(json.loads(args[args.index("--cuda-graph-config") + 1])["decode"]["bs"], [1, 2, 3, 4])
        self.assertEqual(self.observation, before)

    def test_incompatible_current_base_remains_noop_with_no_proposal(self):
        target = self.deployment["spec"]["template"]["spec"]["containers"][0]
        target["image"] = "localhost/workstation/sglang@sha256:" + "c" * 64
        target["resources"]["requests"]["amd.com/gpu"] = "1"
        target["resources"]["limits"]["amd.com/gpu"] = "1"
        target["securityContext"].pop("readOnlyRootFilesystem")
        result = self.result()
        self.assertEqual(result["selected"], "incumbent")
        self.assertIsNone(result["conditional_patch"])
        self.assertEqual(apply_patch(self.deployment, result["patch"]), self.deployment)

    def test_rendered_namespace_must_match_fixed_owner_and_network_policy(self):
        self.deployment["metadata"] = {"namespace": "default"}
        self.assertEqual(self.result()["selected"], "bf16-full-b4")
        self.deployment["metadata"]["namespace"] = "another-namespace"
        result = self.result()
        self.assertEqual(result["selected"], "incumbent")
        self.assertIsNone(result["conditional_patch"])
        self.assertEqual(apply_patch(self.deployment, result["patch"]), self.deployment)

    def test_security_privilege_capabilities_runtime_mounts_and_unknown_math_env_rejected(self):
        original = copy.deepcopy(self.deployment)
        mutations = [lambda t: t["securityContext"].update(privileged=True),
            lambda t: t["securityContext"]["capabilities"].update(add=["SYS_ADMIN"]),
            lambda t: t["securityContext"].update(seccompProfile={"type": "Unconfined"}),
            lambda t: t["securityContext"].update(procMount="Unmasked"),
            lambda t: t["volumeMounts"].append({"name": "notes", "mountPath": "/opt/workstation/sglang/lib"}),
            lambda t: t["env"].append({"name": "SGLANG_USE_NEW_KERNEL", "value": "1"}),
            lambda t: t["env"].append({"name": "TORCHINDUCTOR_COMPILE_THREADS", "value": "4"}),
            lambda t: t["env"].append({"name": "ROCBLAS_USE_HIPBLASLT", "value": "1"}),
            lambda t: t["env"].append({"name": "ROCBLAS_TENSILE_LIBPATH", "value": "/notes/override"}),
            lambda t: t["env"].append({"name": "HIPBLASLT_TUNING_FILE", "value": "/notes/override"}),
            lambda t: t["env"].append({"name": "HIPBLASLT_TUNING_OVERRIDE_FILE", "value": "/notes/override"}),
            lambda t: t.update(envFrom=[{"configMapRef": {"name": "unresolved"}}])]
        for mutate in mutations:
            self.deployment = copy.deepcopy(original)
            mutate(self.deployment["spec"]["template"]["spec"]["containers"][0])
            self.changed_pod()
            result = self.result()
            self.assertEqual(result["selected"], "incumbent")
            self.assertIsNone(result["conditional_patch"])

    def test_unsupported_or_duplicate_arguments_and_environment_not_hidden(self):
        original = copy.deepcopy(self.deployment)
        for extra in (["--tp", "2"], ["--dp-size", "2"], ["--disable-cuda-graph"], ["--enable-torch-compile"],
                      ["--quantization", "fp8"], ["--cuda-graph-bs-decode", "1", "2", "4"]):
            self.deployment = copy.deepcopy(original)
            self.deployment["spec"]["template"]["spec"]["containers"][0]["args"] += extra
            self.changed_pod()
            self.assertEqual(self.result()["selected"], "incumbent")
        self.deployment = copy.deepcopy(original)
        target = self.deployment["spec"]["template"]["spec"]["containers"][0]
        target["env"].append(copy.deepcopy(target["env"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.result()

    def test_native_bounds_shared_pool_and_unqualified_concurrency(self):
        original = copy.deepcopy(self.workload)
        mutations = [lambda w: w.update(concurrency=[8]), lambda w: w.update(concurrency=[3]),
            lambda w: w.update(model_revision="0" * 40), lambda w: w["cases"][0].update(context_tokens=8192),
            lambda w: w["cases"][0].update(input_ids=[1] * 2048), lambda w: w["cases"][0].update(input_ids=[True] * 512),
            lambda w: w["cases"][0].update(input_ids=[151936] * 512), lambda w: w["cases"][0].update(output_tokens=129)]
        for mutate in mutations:
            self.workload = copy.deepcopy(original)
            mutate(self.workload)
            with self.assertRaises(ValueError):
                self.result()

    def test_explicit_native_wave_count_and_capacity(self):
        row = copy.deepcopy(self.workload["cases"][0])
        row.pop("context_tokens")
        self.workload["concurrency"] = [2]
        self.workload["cases"] = [{"context_tokens": 4096, "requests": [copy.deepcopy(row), copy.deepcopy(row)]}]
        graph.validate_workload(self.workload, graph.profile())
        self.workload["cases"][0]["requests"][1]["input_ids"] = [1] * 4096
        with self.assertRaisesRegex(ValueError, "allocation domain"):
            graph.validate_workload(self.workload, graph.profile())
        self.workload["cases"][0]["requests"].pop()
        with self.assertRaisesRegex(ValueError, "exactly"):
            graph.validate_workload(self.workload, graph.profile())

    def test_logical_index_is_bound_to_current_uuid_not_permanent_ordinal(self):
        devices = self.observation["launch"]["devices"]
        devices[0]["index"], devices[1]["index"] = 1, 0
        self.deployment["spec"]["template"]["spec"]["containers"][0]["args"] += ["--base-gpu-id", "1"]
        self.changed_pod()
        self.assertEqual(self.result()["selected"], "bf16-full-b4")
        self.observation["launch"]["selected"] = devices[1]
        self.assertEqual(self.result()["selected"], "incumbent")

    def test_malformed_nested_receipts_fail_closed_not_attributeerror(self):
        original = copy.deepcopy(self.observation)
        for name in ("pod", "launch", "compiler_identity", "runtime_versions", "preflight", "torch_graphs_before"):
            self.observation = copy.deepcopy(original)
            self.observation[name] = None
            self.assertEqual(self.result()["selected"], "incumbent")

    def test_private_fresh_output_and_file_closure(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "new"
            graph.emit(output, self.result())
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
            lines = (output / "SOURCE.sha256").read_text().splitlines()
            self.assertEqual(len(lines), 5)
            for line in lines:
                digest, name = line.split("  ")
                self.assertEqual(graph.hashlib.sha256((output / name).read_bytes()).hexdigest(), digest)
                self.assertEqual(stat.S_IMODE((output / name).stat().st_mode), 0o600)
            with self.assertRaises(FileExistsError):
                graph.emit(output, self.result())
            link = Path(directory) / "link"
            link.symlink_to(output, target_is_directory=True)
            with self.assertRaises(FileExistsError):
                graph.emit(link, self.result())

    def test_json_links_duplicates_nonfinite_and_profile_drift_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.json"
            for raw in ('{"a":1,"a":2}', '{"a":NaN}'):
                path.write_text(raw)
                with self.assertRaises(ValueError):
                    graph.read_json(path)
            path.write_text('{"ok":true}')
            link = Path(directory) / "link.json"
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                graph.read_json(link)
            shutil.copyfile(graph.PROFILE, path)
            with path.open("a") as stream:
                stream.write(" ")
            with patch.object(graph, "PROFILE", path):
                with self.assertRaisesRegex(ValueError, "profile changed"):
                    graph.profile()

    def test_bootstrap_and_bridge_include_runtime_and_contract(self):
        for name in ("source.files", "bridge-runtime.files"):
            rows = (ROOT / "infrastructure/packages/bootstrap" / name).read_text().splitlines()
            self.assertIn("lib/workstation/graph_profile.py", rows)
            self.assertIn("infrastructure/sglang/bf16-full-b4/profile.json", rows)
        self.assertIn("tests/hardware/test_graph_profile.py", (ROOT / "infrastructure/packages/bootstrap/source.files").read_text().splitlines())

if __name__ == "__main__":
    unittest.main()
