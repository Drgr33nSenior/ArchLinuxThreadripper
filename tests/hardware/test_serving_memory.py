"""Synthetic memory records only; no pod, model or GPU is qualified."""
import copy
from contextlib import contextmanager
import hashlib
import io
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/"lib/workstation"))
import model_kernels
import serving
import serving_memory as memory
from test_model_kernels import fixtures


def store(path, data):
    path.write_text(json.dumps(data, indent=2)+"\n")


def sample(offset, uid="1", total_mib=65536):
    gib = 1024**3
    return {"monotonic_ns": (offset+1)*10**9, "unix_ns": (offset+1000)*10**9,
            "host_memory": f"MemTotal: {total_mib*1024} kB\nMemAvailable: 30000000 kB\nSwapTotal: 0 kB\n",
            "pod_cgroup": {"id": "30:"+uid, "path": "/sys/fs/cgroup/pod00000000-0000-0000-0000-"+uid.zfill(12),
                "values": {"memory.current": str(8*gib), "memory.peak": str(10*gib),
                           "memory.max": str(38*gib), "memory.swap.current": "0", "memory.swap.max": "0",
                           "memory.stat": f"anon {5*gib}\nfile {3*gib}\nshmem {gib}",
                           "memory.events": "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0",
                           "memory.pressure": "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0"}}}


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.deployment, self.workload, (pod, self.runtime, _) = fixtures()
        self.deployment["kind"] = "Deployment"
        self.pod = {**pod, "name": "sglang-fixture", "node": "fixture-node", "container_id": "containerd://fixture",
                    "restart_count": 0, "ready": True, "shm": [{"medium": "Memory", "sizeLimit": "16Gi"}]}
        container = self.deployment["spec"]["template"]["spec"]["containers"][0]
        launch = {key: container.get(key, []) for key in ("command", "args", "env", "envFrom")}
        self.pod["launch_spec_sha256"] = hashlib.sha256((json.dumps(launch, sort_keys=True, separators=(",", ":"))+"\n").encode()).hexdigest()
        self.resource = {"schema_version": 1, "status": "offline-plan-not-applied", "gpu_count": 2,
                         "memory": {"total_mib": 65536, "host_reserve_mib": 12288, "kube_reserve_mib": 4096,
                                    "eviction_mib": 2048, "allocatable_mib": 47104}}
        store(self.root/"deployment.json", self.deployment)
        store(self.root/"workload.json", self.workload)
        store(self.root/"resource.json", self.resource)
        self.observations = []
        for index, phase in enumerate(("cold", "warm", "cold", "warm"), 1):
            start, run = self.root/f"start{index}", self.root/f"run{index}"
            start.mkdir(); run.mkdir(); (run/"after").mkdir()
            pod = {**self.pod, "uid": f"00000000-0000-0000-0000-{index:012d}", "container_id": f"containerd://fixture{index}"}
            store(start/"startup.json", {"schema": 1, "status": "observed-not-qualified", "cache_state": phase,
                "pod_before": {**pod, "ready": False}, "pod": pod, "memory_telemetry": "memory.jsonl"})
            store(run/"after/runtime.json", self.runtime)
            store(run/"after/pod.json", pod)
            rows = []
            for case in self.workload["cases"]:
                for count in self.workload["concurrency"]:
                    for repeat in range(self.workload["repetitions"]):
                        rows.append({"context_tokens": case["context_tokens"], "concurrency": count,
                            "repetition": repeat, "failures": 0, "cache_state_verified": True, "wall_seconds": 1,
                            "requests": [{"ok": True, "output_tokens": case["output_tokens"],
                                "ttft_seconds": .2, "latency_seconds": .4}]*count})
            store(run/"result.json", {"schema": 1, "status": "measured-not-qualified", "pod": pod,
                "measurement_started_monotonic_ns": 11*10**9, "measurement_finished_monotonic_ns": 71*10**9,
                "workload_finished_monotonic_ns": 70*10**9,
                "runtime": self.runtime, "workload_sha256": hashlib.sha256((self.root/"workload.json").read_bytes()).hexdigest(),
                "runtime_sha256": hashlib.sha256((run/"after/runtime.json").read_bytes()).hexdigest(), "runs": rows})
            for path, offsets in ((start/"memory.jsonl", (0, 1)), (run/"host-telemetry.jsonl", range(10, 71, 10))):
                path.write_text("".join(json.dumps(sample(offset, str(index)))+"\n" for offset in offsets))
            self.observations.append((start, run))

    def plan(self, **kwargs):
        return memory.plan(self.root/"deployment.json", self.root/"workload.json", self.root/"resource.json",
                           self.observations, kwargs.pop("other_mib", 8192), **kwargs)

    def change(self, path, fn):
        data = json.loads(path.read_text()); fn(data); store(path, data)

    def test_candidate_and_exact_rollback(self):
        result = self.plan()
        self.assertEqual(result["status"], "plan-only-unqualified")
        # Lifetime peak 10 GiB + full 16 GiB growth reserve + 25% = 32.5 GiB.
        self.assertEqual(result["candidate_mib"], 33280)
        original = copy.deepcopy(self.deployment["spec"]["template"]["spec"])
        spec = copy.deepcopy(original)
        for name in ("patch", "rollback"):
            operations = result[name]
            self.assertEqual(operations[0]["value"], spec)
            spec["containers"][0]["resources"] = copy.deepcopy(operations[1]["value"])
        self.assertEqual(spec, original)
        self.assertEqual(self.deployment["spec"]["template"]["spec"], original)

    def test_cli_package_and_private_artifacts(self):
        output = self.root/"plan"
        command = ["bash", str(ROOT/"bin/workstationctl"), "rocm", "serving-memory-plan",
                   str(self.root/"deployment.json"), str(self.root/"workload.json"), str(self.root/"resource.json"),
                   str(output), "--other-mib", "8192"]
        for pair in self.observations:
            command += ["--observation", *map(str, pair)]
        env = dict(os.environ, WORKSTATION_PYTHON=sys.executable)
        result = subprocess.run(command, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads((output/"plan.json").read_text())
        for name, digest in record["artifacts_sha256"].items():
            self.assertEqual(hashlib.sha256((output/name).read_bytes()).hexdigest(), digest)
            self.assertEqual((output/name).stat().st_mode & 0o777, 0o600)
        self.assertEqual(output.stat().st_mode & 0o777, 0o700)
        self.assertNotEqual(subprocess.run(command, env=env, capture_output=True).returncode, 0)
        for manifest in ("source.files", "bridge-runtime.files"):
            self.assertIn("lib/workstation/serving_memory.py", (ROOT/"infrastructure/packages/bootstrap"/manifest).read_text().splitlines())

    def test_limits_and_shared_memory_are_not_usage(self):
        rows = [sample(0), sample(10)]
        result = memory.window(rows, 38912, 16384, 65536, 0)
        self.assertEqual(result["sampled_current_max_bytes"], 8*1024**3)
        self.assertEqual(result["full_shm_envelope_bytes"], 26*1024**3)
        self.assertEqual(result["sampled_stat_max_bytes"]["shmem"], 1024**3)

    def test_unknown_malformed_pressure_swap_and_scope(self):
        for key, value in (("memory.stat", None), ("memory.peak", "max"), ("memory.max", "1"),
                           ("memory.events", "high 0\nmax 1\noom 0\noom_kill 0"),
                           ("memory.events", "high 0\nmax 0\noom 0\noom_kill 1"),
                           ("memory.swap.current", "1"), ("memory.pressure", "")):
            with self.subTest(key=key, value=value):
                rows = [sample(0), sample(10)]; rows[1]["pod_cgroup"]["values"][key] = value
                with self.assertRaises((ValueError, KeyError)):
                    memory.window(rows, 38912, 16384, 65536, 0)
        for fn in (lambda row: row["pod_cgroup"].update(id="30:2"),
                   lambda row: row.update(monotonic_ns=1),
                   lambda row: row["pod_cgroup"]["values"].update({"memory.pressure": "some total=1\nfull total=0"}),
                   lambda row: row["pod_cgroup"]["values"].update({"memory.peak": str(9*1024**3)})):
            rows = [sample(0), sample(10)]; fn(rows[1])
            with self.assertRaises(ValueError):
                memory.window(rows, 38912, 16384, 65536, 0)
        for rows in ([], [sample(0)], [sample(0), sample(60)]):
            with self.assertRaises(ValueError):
                memory.window(rows, 38912, 16384, 65536, 0)

    def test_dimm_change_requires_new_plan_and_insufficient_ram(self):
        with self.assertRaises(ValueError):
            memory.window([sample(0, total_mib=131072), sample(10, total_mib=131072)], 38912, 16384, 65536, 0)
        with self.assertRaises(ValueError):
            self.plan(other_mib=30000)
        for value in (1000, 38912, 65536):
            with self.assertRaises(ValueError):
                self.plan(memory_mib=value)
        self.assertEqual(self.plan(memory_mib=34816)["candidate_mib"], 34816)

    def test_missing_relabelled_and_failed_observations(self):
        saved = self.observations[:]
        self.observations = saved[:2]
        with self.assertRaises(ValueError): self.plan()
        self.observations = [saved[0]]*4
        with self.assertRaises(ValueError): self.plan()
        self.observations = saved
        self.change(saved[0][0]/"startup.json", lambda data: data.update(status="failed"))
        with self.assertRaises(ValueError): self.plan()

    def test_identity_template_model_and_workload_drift(self):
        path = self.observations[0][1]/"result.json"
        original = json.loads(path.read_text())
        mutations = (lambda r: r["pod"].update(restart_count=1), lambda r: r["pod"].update(container_id="changed"),
                     lambda r: r["pod"].update(launch_spec_sha256="0"*64),
                     lambda r: r["runtime"]["devices"][1].update(uuid="GPU-a"),
                     lambda r: r["runtime"]["settings"].update(CONTEXT_LENGTH="8192"),
                     lambda r: r.update(workload_sha256="b"*64), lambda r: r.update(status="failed"),
                     lambda r: r["runs"].pop(), lambda r: r["runs"][0].update(failures=1))
        for fn in mutations:
            with self.subTest(fn=fn):
                data = copy.deepcopy(original); fn(data); store(path, data)
                with self.assertRaises(ValueError): self.plan()
        store(path, original)
        self.change(self.root/"deployment.json", lambda r: r["spec"]["template"]["spec"]["containers"][0]["args"].append("--other-setting"))
        with self.assertRaises(ValueError): self.plan()

    def test_tampering_is_rechecked(self):
        original = memory.Inputs.unchanged
        def tamper(inputs):
            (self.root/"workload.json").write_text("{}")
            original(inputs)
        with patch.object(memory.Inputs, "unchanged", tamper), self.assertRaises(ValueError): self.plan()

    def test_shm_cgroup_and_end_of_workload_binding(self):
        for field, value in (("shm", [{"medium": "Memory", "sizeLimit": "32Gi"}]),):
            for start, run in self.observations:
                self.change(start/"startup.json", lambda r: (r["pod"].update({field: value}), r["pod_before"].update({field: value})))
                self.change(run/"result.json", lambda r: r["pod"].update({field: value}))
                self.change(run/"after/pod.json", lambda r: r.update({field: value}))
            with self.assertRaises(ValueError): self.plan()

    def test_foreign_or_truncated_memory_samples(self):
        start, run = self.observations[0]
        path = run/"host-telemetry.jsonl"
        original = path.read_text()
        rows = [json.loads(line) for line in original.splitlines()]
        path.write_text("".join(json.dumps(row)+"\n" for row in rows[:-1]))
        with self.assertRaises(ValueError): self.plan(minimum_seconds=30)
        path.write_text(original)
        for path in (start/"memory.jsonl", run/"host-telemetry.jsonl"):
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            for row in rows: row["pod_cgroup"]["path"] = "/sys/fs/cgroup/unrelated"
            path.write_text("".join(json.dumps(row)+"\n" for row in rows))
        with self.assertRaises(ValueError): self.plan()

    def test_serving_final_sample_covers_last_response_and_failure_is_incomplete(self):
        evidence = self.root/"serving-evidence"
        evidence.mkdir()
        store(evidence/"pod.json", self.pod)
        store(evidence/"runtime.json", self.runtime)
        expected_requests = 1 + sum(self.workload["concurrency"])*self.workload["repetitions"]
        for fail_final in (False, True):
            with self.subTest(fail_final=fail_final):
                ticks = itertools.count(1)
                completed, sampled_after = [], []
                output = self.root/f"serving-{fail_final}"

                class Response(io.BytesIO):
                    def __exit__(self, *args):
                        completed.append(next(ticks))
                        return super().__exit__(*args)

                def request(_base, path, payload=None, **_kwargs):
                    if path == "/model_info":
                        return io.BytesIO(json.dumps({"model_path": self.runtime["settings"]["MODEL_PATH"]}).encode())
                    self.assertEqual(path, "/generate")
                    meta = {"completion_tokens": payload["sampling_params"]["max_new_tokens"], "cached_tokens": 1}
                    return Response(b"data: " + json.dumps({"meta_info": meta}).encode() + b"\n\ndata: [DONE]\n\n")

                def snapshot():
                    sampled_after.append(len(completed))
                    if fail_final and len(sampled_after) == 2:
                        raise RuntimeError("synthetic final sample failure")
                    return {"monotonic_ns": next(ticks)}

                @contextmanager
                def sampler(path, *, collect):
                    path.write_text(json.dumps(collect())+"\n")
                    yield

                with patch.object(serving, "request", side_effect=request), \
                        patch.object(serving, "server_metrics", return_value={}), \
                        patch.object(serving, "server_load", return_value=[]), \
                        patch.object(serving, "Sampler", sampler), \
                        patch.object(serving, "snapshot", side_effect=snapshot), \
                        patch.object(serving, "pod_cgroup", return_value=Path("/synthetic/pod")), \
                        patch.object(serving, "cgroup_values", return_value=sample(0)["pod_cgroup"]), \
                        patch.object(serving.time, "monotonic_ns", side_effect=lambda: next(ticks)):
                    if fail_final:
                        with self.assertRaisesRegex(RuntimeError, "synthetic final sample failure"):
                            serving.run("http://synthetic.invalid", self.root/"workload.json", evidence, output)
                    else:
                        self.assertEqual(serving.run("http://synthetic.invalid", self.root/"workload.json", evidence, output), 0)
                report = json.loads((output/"result.json").read_text())
                rows = [json.loads(line) for line in (output/"host-telemetry.jsonl").read_text().splitlines()]
                self.assertEqual(len(completed), expected_requests)
                self.assertEqual(sampled_after, [0, expected_requests])
                self.assertTrue(all(row["failures"] == 0 for row in report["runs"]))
                self.assertGreater(report["workload_finished_monotonic_ns"], max(completed))
                if fail_final:
                    self.assertEqual(report["status"], "incomplete")
                    self.assertNotIn("measurement_finished_monotonic_ns", report)
                    self.assertEqual(len(rows), 1)
                else:
                    self.assertEqual(report["status"], "measured-awaiting-provenance")
                    self.assertEqual(len(rows), 2)
                    self.assertGreater(rows[-1]["monotonic_ns"], report["workload_finished_monotonic_ns"])
                    self.assertGreater(report["measurement_finished_monotonic_ns"], rows[-1]["monotonic_ns"])

    def test_workload_duration_and_host_swap(self):
        path = self.observations[0][1]/"result.json"
        self.change(path, lambda r: r["runs"][0].update(wall_seconds=600))
        with self.assertRaises(ValueError): self.plan()
        rows = [sample(0), sample(10)]
        for row in rows: row["pod_cgroup"]["values"]["memory.swap.max"] = "max"
        memory.window(rows, 38912, 16384, 65536, 0)
        rows[0]["host_memory"] = rows[0]["host_memory"].replace("SwapTotal: 0", "SwapTotal: 1024")
        with self.assertRaises(ValueError): memory.window(rows, 38912, 16384, 65536, 0)

    def test_shared_anonymous_memory_is_not_subtracted(self):
        rows = [sample(0), sample(10)]
        for row in rows:
            row["pod_cgroup"]["values"].update({"memory.current": str(24*1024**3), "memory.peak": str(24*1024**3),
                                              "memory.stat": f"anon {12*1024**3}\nfile {12*1024**3}\nshmem {12*1024**3}"})
        result = memory.window(rows, 38912, 16384, 65536, 0)
        self.assertEqual(result["full_shm_envelope_bytes"], 40*1024**3)

    def test_memory_only_numerical_comparison(self):
        _, _, observed = fixtures()
        baseline = {"status": "measured-not-qualified", "kind": "kernel-warmup", "memory": {"status": "checked"},
            "workload_sha256": "a"*64, "identity": model_kernels.runtime_key(*observed),
            "warmup_contract": {"repetitions": 3, "cases": [{"context_tokens": 4096, "output_tokens": 1}], "concurrency": [1]},
            "quality": [{"case": [r, 4096, 1, 0], "tokens": [1], "logprobs": [-.5]} for r in range(3)]}
        candidate = copy.deepcopy(baseline)
        for scope in ("requests", "limits"):
            candidate["identity"]["resources"][scope]["memory"] = "30Gi"
        with self.assertRaises(ValueError): model_kernels.compare_quality(baseline, candidate, .001, .001)
        result = model_kernels.compare_quality(baseline, candidate, .001, .001, memory_only=True)
        self.assertEqual(result["comparison"], "host-memory-only")
        candidate["identity"]["runtime"]["settings"]["MAX_RUNNING_REQUESTS"] = "1"
        with self.assertRaises(ValueError): model_kernels.compare_quality(baseline, candidate, .001, .001, memory_only=True)


if __name__ == "__main__":
    unittest.main()
