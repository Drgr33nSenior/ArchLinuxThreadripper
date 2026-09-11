"""Real shell orchestration, synthetic boundaries and harmless process groups."""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib/workstation"))
import performance_profiles as profiles

# Each external launch crosses exec/setsid exactly as on the target. No kubectl,
# server, model, telemetry tool or actual profiling endpoint is invoked.
HELPER = r'''
import hashlib, json, os, pathlib, signal, subprocess, sys, time
root = pathlib.Path(os.environ["CASE_ROOT"])
name = pathlib.Path(sys.argv[0]).name
if name == "setsid":
    os.setsid()
    os.execvp(sys.argv[1], sys.argv[1:])
if name == "timeout":
    os.execvp(sys.argv[3], sys.argv[3:])
if name == "kubectl":
    (root/"forward.pid").write_text(str(os.getpid()))
    print("Forwarding from 127.0.0.1:18000 -> 30000", flush=True)
    while True: time.sleep(.05)
if sys.argv[2] == "finish":
    sys.exit(0)
output = pathlib.Path(sys.argv[-1]); output.mkdir()
(output/"compiler-before.json").write_bytes(pathlib.Path(sys.argv[-2]).read_bytes())
runtime = json.loads((root/"evidence/runtime.json").read_text())
pod = json.loads((root/"evidence/pod.json").read_text())
workload = root/"workload.json"
digest = hashlib.sha256(workload.read_bytes()).hexdigest()
summary = {"n":24,"min":.9,"median":1,"p95":1.1,"p99":1.2,"max":1.3,"stdev":.02}
record = {"schema":1, "status":"measured-awaiting-provenance", "workload_sha256":digest,
          "runtime_sha256":hashlib.sha256((root/"evidence/runtime.json").read_bytes()).hexdigest(),
          "runtime":runtime, "pod":pod, "profile":"interactive", "prefix_state":"warm-prefix",
          "repeat_summary":[{"context_tokens":4096,"concurrency":1,"failures":0,
                              "throughput_across_repetitions":summary,
                              "request_latency_seconds":summary,"ttft_seconds":summary}],
          "profile_path":"/cache/xdg/workstation-profiles/" + "a"*32,
          "profile_stop":"explicit-awaiting-traces"}
(output/"result.json").write_text(json.dumps(record))
if os.environ["CASE_OUTCOME"] == "failure":
    record["status"] = "failed"
    (output/"result.json").write_text(json.dumps(record)); sys.exit(7)
if os.environ["CASE_OUTCOME"] in ("INT", "TERM"):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    (root/"child.pid").write_text(str(child.pid))
    def stop(sig, _frame):
        record["status"] = "failed-interrupted"
        (output/"result.json").write_text(json.dumps(record))
        child.wait(timeout=5)
        sys.exit(128 + sig)
    signal.signal(signal.SIGTERM, stop)
    (root/"worker.pid").write_text(str(os.getpid()))
    while True: time.sleep(.05)
'''


class OrchestrationTests(unittest.TestCase):
    def test_modes_outcomes_and_owned_cleanup(self):
        for mode in ("kernel-warmup", "kernel-profile"):
            for outcome in ("success", "failure", "INT", "TERM"):
                with self.subTest(mode=mode, outcome=outcome):
                    self.run_case(mode, outcome)

    def test_unknown_temp_contents_preserve_primary_outcome(self):
        self.run_case("kernel-warmup", "success", foreign=True)
        self.run_case("kernel-profile", "failure", foreign=True)

    def test_real_wrapper_finalizes_a_profile_inspectable_serving_result(self):
        self.run_case("kernel-warmup", "success", inspect_finalized=True)

    def run_case(self, mode, outcome, foreign=False, inspect_finalized=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"bin").mkdir(); (root/"temp").mkdir(); (root/"evidence").mkdir()
            pod = {"name":"fixture", "node":"fixture-node", "image":"example@sha256:" + "1" * 64,
                   "image_id":"containerd://sha256:" + "2" * 64, "launch_spec_sha256":"f" * 64,
                   "resources":{"requests":{"cpu":"8","memory":"38Gi","amd.com/gpu":"1"},
                                "limits":{"cpu":"8","memory":"38Gi","amd.com/gpu":"1"}}}
            runtime = {"settings":{"MODEL_REVISION":"a" * 40, "MODEL_PATH":"/models/fixture", "TENSOR_PARALLEL":"1"},
                       "model_files":{"model.safetensors":{"bytes":1,"sha256":"c" * 64},
                                      "tokenizer.json":{"bytes":1,"sha256":"b" * 64}},
                       "packages":{"sglang":"fixture","torch":"fixture","triton":None,
                                   "pytorch-triton-rocm":None,"aiter":None,"transformers":None}, "hip":"fixture",
                       "devices":[{"uuid":"GPU-a","gfx":"gfx1201"}],
                       "launch":[{"--model-path":"/models/fixture"}],
                       "model_contract":{"quant_method":"fp8"}}
            (root/"evidence/pod.json").write_text(json.dumps(pod))
            (root/"evidence/runtime.json").write_text(json.dumps(runtime))
            (root/"workload.json").write_text('{"schema":1}')
            for name in ("setsid", "timeout", "kubectl", "helper-python"):
                path = root/"bin"/name
                path.write_text(f"#!{sys.executable}\n" + HELPER); path.chmod(0o700)
            env = dict(os.environ, CASE_ROOT=str(root), REPO_ROOT=str(ROOT), CASE_MODE=mode,
                       CASE_OUTCOME=outcome, FOREIGN_TEMP=str(int(foreign)), TMPDIR=str(root/"temp"),
                       PATH=str(root/"bin")+os.pathsep+os.environ["PATH"])
            unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
            process = subprocess.Popen(["bash", str(ROOT/"tests/fixtures/kernel-orchestration.sh")],
                                       env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            try:
                if outcome in ("INT", "TERM"):
                    deadline = time.monotonic()+10
                    while not (root/"worker.pid").exists() and time.monotonic() < deadline and process.poll() is None:
                        time.sleep(.02)
                    self.assertTrue((root/"worker.pid").exists())
                    os.kill(int((root/"shell.pid").read_text()), getattr(signal, "SIG"+outcome))
                stdout, stderr = process.communicate(timeout=15)
                expected = {"success":0, "failure":7, "INT":130, "TERM":143}[outcome]
                self.assertEqual(process.returncode, expected, (stdout, stderr))
                state = json.loads((root/"output/result.json").read_text())["status"]
                self.assertEqual(state, "measured-not-qualified" if outcome == "success" else
                                 "failed" if outcome == "failure" else "failed-interrupted")
                if inspect_finalized:
                    self.assertEqual(outcome, "success")
                    result_path = root/"output/result.json"
                    result = json.loads(result_path.read_text())
                    self.assertEqual((result["schema"], result["status"]), (1, "measured-not-qualified"))
                    bundle = root/"bundle"
                    (bundle/"serving").mkdir(parents=True)
                    (bundle/"startup").mkdir()
                    (bundle/"serving/result.json").write_bytes(result_path.read_bytes())
                    (bundle/"runtime.json").write_bytes((root/"evidence/runtime.json").read_bytes())
                    for index, phase in enumerate(("cold", "warm")):
                        (bundle/f"startup/{index}.json").write_text(json.dumps(
                            {"schema":1,"status":"observed-not-qualified","cache_state":phase,
                             "elapsed_seconds":20,"pod":pod}))
                    runtime_digest = profiles.digest_bytes((bundle/"runtime.json").read_bytes())
                    identity = profiles.producer_identity(result, runtime, runtime_digest)
                    manifest = {"schema":1,"kind":"workstation-performance-evidence",
                                "status":"measured-not-qualified","profile_id":"fixture-finalized",
                                "identity":identity,
                                "experiment":{"variables":{},"profile":"interactive","prefix_state":"warm-prefix"},
                                "sources":{"serving":"serving/result.json","runtime":"runtime.json",
                                           "startup":["startup/0.json","startup/1.json"]}}
                    (bundle/"manifest.json").write_text(json.dumps(manifest))
                    self.assertEqual(profiles.inspect(bundle)["metrics"]["serving"]["source_status"],
                                     "measured-not-qualified")
                temp = Path((root/"temp-path").read_text().strip())
                self.assertEqual(temp.exists(), foreign)
                if foreign:
                    self.assertEqual(sorted(p.name for p in temp.iterdir()), ["foreign"])
                self.assertIsNone(unrelated.poll())
                for filename in ("forward.pid", "worker.pid", "child.pid"):
                    if (root/filename).exists():
                        with self.assertRaises(ProcessLookupError, msg=filename):
                            os.kill(int((root/filename).read_text()), 0)
            finally:
                unrelated.terminate(); unrelated.wait(timeout=5)
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL); process.wait()


if __name__ == "__main__":
    unittest.main()
