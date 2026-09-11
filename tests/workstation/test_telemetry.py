"""Offline fixtures: no cluster, hardware, credentials or model requests."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib/workstation"))
import telemetry
import telemetry_sample


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.work = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def args(self, name="render", **kwargs):
        values = dict(root=str(ROOT), output=str(self.work / name), enabled="true",
                      gpu_exporter="false", trace="false", kubelet="false", node_name="fixture-node",
                      api_address="192.168.50.10", host_address="192.168.50.10",
                      reserve_mib=6144, workloads="sglang,open-webui", overlay="apps/overlays/dual-gpu")
        values.update(kwargs)
        return argparse.Namespace(**values)

    def test_real_render_integrity_preserves_baseline(self):
        args = self.args()
        telemetry.render(args)
        evidence = telemetry.verify(args.output)
        self.assertEqual(evidence["status"], "generated-not-deployed")
        self.assertEqual(evidence["hardware_qualification"], "NOT RUN")
        objects = telemetry.load_objects((Path(args.output) / "workloads.yaml").read_text())
        actual = next(o for o in objects if o["kind"] == "Deployment" and o["metadata"]["name"] == "sglang")
        baseline = next(o for o in telemetry.kustomize(ROOT / args.overlay) if o["kind"] == "Deployment" and o["metadata"]["name"] == "sglang")
        self.assertEqual(actual["spec"]["replicas"], 0)
        got, original = (o["spec"]["template"]["spec"]["containers"][0] for o in (actual, baseline))
        self.assertEqual(got["args"], original["args"] + ["--enable-metrics"])
        for key in ("image", "resources", "env", "volumeMounts"):
            self.assertEqual(got[key], original[key])
        self.assertNotIn("192.0.2.1/32", (Path(args.output) / "stack.yaml").read_text())
        with self.assertRaises(FileExistsError):
            telemetry.render(args)
        (Path(args.output) / "workloads.yaml").write_text("tampered")
        with self.assertRaises(telemetry.InvalidTelemetry):
            telemetry.verify(args.output)

    def test_disabled_does_not_discover_or_deploy(self):
        args = self.args(enabled="false", api_address="", host_address="")
        with patch.object(telemetry, "kustomize", side_effect=AssertionError("unexpected discovery")):
            telemetry.render(args)
        self.assertEqual(telemetry.verify(args.output)["status"], "disabled-not-deployed")
        self.assertEqual((Path(args.output) / "stack.yaml").read_text(), "")

    def test_exact_trace_and_kubelet_profiles(self):
        args = self.args(trace="true", kubelet="true")
        telemetry.render(args)
        text = (Path(args.output) / "workloads.yaml").read_text()
        self.assertIn("--trace-modules", text)
        self.assertIn("SGLANG_TRACE_LEVEL", text)
        self.assertNotIn("--trace-level", text)
        self.assertNotIn("--log-requests", text)
        stack = (Path(args.output) / "stack.yaml").read_text()
        self.assertIn("fixture-node", stack)
        self.assertIn("192.168.50.10:10250", stack)
        objects = telemetry.load_objects(stack)
        self.assertFalse(any("nodes/proxy" in r.get("resources", []) for o in objects
                             if o["kind"] == "ClusterRole" for r in o.get("rules", [])))
        self.assertNotIn("review-required-node", stack)

    def test_changed_target_refreshes_configmap_identity(self):
        first = self.args("first", kubelet="true")
        second = self.args("second", kubelet="true", host_address="192.168.50.11")
        telemetry.render(first)
        telemetry.render(second)
        def names(path):
            return {o["metadata"]["name"] for o in telemetry.load_objects((Path(path) / "stack.yaml").read_text())
                    if o["kind"] == "ConfigMap" and o["metadata"]["name"].startswith("alloy-config-")}
        self.assertTrue(names(first.output))
        self.assertNotEqual(names(first.output), names(second.output))
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(telemetry.source_identity(Path(directory))["git_revision"])

    def test_invalid_inputs_fail_before_output(self):
        for kwargs in ({"api_address": "8.8.8.8"}, {"host_address": "127.0.0.1"},
                       {"api_address": ""}, {"gpu_exporter": "true"}, {"reserve_mib": 1},
                       {"overlay": "../elsewhere"}, {"workloads": "sglang,sglang"}):
            with self.assertRaises((ValueError, OSError)):
                telemetry.render(self.args(**kwargs))
            self.assertFalse((self.work / "render").exists())

    def test_empty_failed_discovery_and_secrets_rejected(self):
        for text in ("", "---\n", "[]", "kind: Secret\ndata: {key: DUMMY_SECRET_SENTINEL}"):
            with self.assertRaises(telemetry.InvalidTelemetry):
                telemetry.load_objects(text)
        result = argparse.Namespace(returncode=1, stdout="", stderr="DUMMY_SECRET_SENTINEL")
        with patch.object(telemetry.subprocess, "run", return_value=result):
            with self.assertRaises(telemetry.InvalidTelemetry) as error:
                telemetry.kustomize(ROOT)
        self.assertNotIn("DUMMY_SECRET_SENTINEL", str(error.exception))

    def test_budget_64_128_and_insufficient_cpu_memory(self):
        args = self.args()
        telemetry.render(args)
        source = self.work / "resources.json"
        for index, (ram, cpu, fits) in enumerate(((47104, 42, True), (112640, 42, True),
                                                 (45000, 42, False), (47104, 20, False))):
            source.write_text(json.dumps({"schema_version": 1, "status": "offline-plan-not-applied",
                                          "memory": {"allocatable_mib": ram},
                                          "allocatable_logical_cpus": cpu, "gpu_count": 2}))
            out = self.work / f"plan{index}"
            self.assertEqual(telemetry.plan(argparse.Namespace(resources=source, rendered=args.output, output=out)),
                             0 if fits else 1)
            record = json.loads((out / "capacity.json").read_text())
            self.assertEqual(record["fits"], fits)
            self.assertEqual(record["workloads"][0]["shared_memory_mib"], 16384)
        source.write_text('{"schema_version":1,"status":"unknown"}')
        with self.assertRaises(telemetry.InvalidTelemetry):
            telemetry.plan(argparse.Namespace(resources=source, rendered=args.output, output=self.work / "unknown"))

    def test_rag_memory_is_not_free(self):
        args = self.args(overlay="apps/overlays/rag")
        telemetry.render(args)
        source = self.work / "resources.json"
        source.write_text(json.dumps({"schema_version": 1, "status": "offline-plan-not-applied",
                                      "memory": {"allocatable_mib": 47104}, "allocatable_logical_cpus": 42, "gpu_count": 2}))
        result = telemetry.plan(argparse.Namespace(resources=source, rendered=args.output, output=self.work / "rag-plan"))
        self.assertEqual(result, 1)

    def test_sampler_two_devices_unknown_and_no_secret_fields(self):
        values = {"mem_info_vram_total": "34359738368", "mem_info_vram_used": "1024",
                  "gpu_busy_percent": "N/A", "current_link_width": "16", "pp_dpm_sclk": "0: 100Mhz\n1: 2200Mhz *"}
        sample = {"unix_ns": 1000000000, "gpus_by_bdf": {"0000:01:00.0": values, "0000:09:00.0": values},
                  "cpu_power": {"boost": None, "policies": {}}, "host_memory": "DUMMY_SECRET_SENTINEL"}
        text = telemetry_sample.metrics(sample, self.work)
        self.assertIn('pci_bdf="0000:01:00.0"', text)
        self.assertIn('pci_bdf="0000:09:00.0"', text)
        self.assertNotIn("workstation_gpu_busy_percent{", text)
        self.assertIn('sensor="busy_percent"} 0', text)
        self.assertIn("2200000000", text)
        self.assertNotIn("DUMMY_SECRET_SENTINEL", text)
        self.assertNotIn("NaN", text)
        telemetry_sample.write_textfile(self.work, text)
        telemetry_sample.write_textfile(self.work, text)
        self.assertEqual((self.work / "hardware.prom").read_text(), text)
        (self.work / "hardware.prom").unlink()
        (self.work / "unrelated").write_text("preserve")
        (self.work / "hardware.prom").symlink_to(self.work / "unrelated")
        with self.assertRaises(ValueError):
            telemetry_sample.write_textfile(self.work, "bad")
        self.assertEqual((self.work / "unrelated").read_text(), "preserve")


if __name__ == "__main__":
    unittest.main()
