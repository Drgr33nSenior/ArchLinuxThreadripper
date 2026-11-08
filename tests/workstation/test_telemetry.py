"""Offline fixtures: no cluster, hardware, credentials or model requests."""
import argparse
import json
import shutil
import subprocess
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
    def test_scoped_single_image_and_retained_dual_image(self):
        locks = dict(line.split("=", 1) for line in (ROOT / "versions.lock").read_text().splitlines()
                     if line and not line.startswith("#"))
        for overlay, key in (("single-gpu", "SGLANG_SINGLE_ROCM_IMAGE"),
                             ("family", "SGLANG_SINGLE_ROCM_IMAGE"),
                             ("dual-gpu", "SGLANG_ROCM_IMAGE"), ("default", "SGLANG_ROCM_IMAGE"),
                             ("rdna4-compat", "SGLANG_ROCM_IMAGE")):
            with self.subTest(overlay=overlay):
                args = self.args(overlay, overlay="apps/overlays/" + overlay)
                telemetry.render(args)
                objects = telemetry.load_objects((Path(args.output) / "workloads.yaml").read_text())
                deployment = next(o for o in objects if o["kind"] == "Deployment" and o["metadata"]["name"] == "sglang")
                self.assertEqual(deployment["spec"]["replicas"], 0)
                self.assertEqual(deployment["spec"]["template"]["spec"]["containers"][0]["image"], locks[key])
                self.assertNotEqual(deployment["metadata"].get("annotations", {}).get("workstation.ai/qualification"), "qualified")

    def test_runtime_allowlist_closes_both_telemetry_profiles(self):
        # Use only files shipped to the installed runtime, not the larger
        # source export or any imports/assets from the development checkout.
        runtime = self.work / "runtime"
        runtime.mkdir()
        names = (ROOT / "infrastructure/packages/bootstrap/bridge-runtime.files").read_text().splitlines()
        self.assertEqual(len(names), len(set(names)))
        for name in names:
            if not name or name.startswith("#"):
                continue
            source = ROOT / name
            self.assertFalse(source.is_symlink(), name)
            self.assertTrue(source.is_file(), name)
            target = runtime / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        status_help = subprocess.run([sys.executable, "-B", str(runtime / "lib/workstation/monitoring.py"), "--help"],
                                     capture_output=True, text=True, timeout=10)
        self.assertEqual(status_help.returncode, 0, status_help.stderr)
        for profile, allowance in (("full", 6656), ("metrics", 4864)):
            with self.subTest(profile=profile):
                args = self.args(profile, root=str(runtime), profile=profile)
                argv = [sys.executable, "-B", str(runtime / "lib/workstation/telemetry.py"), "render", args.output, args.overlay]
                for key, value in vars(args).items():
                    if key in ("output", "overlay"):
                        continue
                    argv.extend(["--" + key.replace("_", "-"), str(value)])
                completed = subprocess.run(argv, capture_output=True, text=True, timeout=45)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                checked = telemetry.verify(args.output)
                self.assertEqual(checked["calculated_allowance_mib"], allowance)
                self.assertEqual(checked["profile"], profile)
                host = runtime / "infrastructure/observability/host" / ("config.alloy" if profile == "full" else "config.metrics.alloy")
                self.assertTrue(host.is_file())
                self.assertEqual(host.read_bytes(), (ROOT / host.relative_to(runtime)).read_bytes())
                self.assertIn('prometheus.scrape "self"', host.read_text())
                stack = telemetry.load_objects((Path(args.output) / "stack.yaml").read_text())
                exporter = next(o for o in stack if o["kind"] == "DaemonSet" and
                                o["metadata"]["name"] == "node-exporter")
                pod = exporter["spec"]["template"]["spec"]
                self.assertIn("--collector.edac", pod["containers"][0]["args"])
                self.assertIn("--collector.hwmon", pod["containers"][0]["args"])
                self.assertIn("--collector.textfile", pod["containers"][0]["args"])
                self.assertTrue(pod["securityContext"]["runAsNonRoot"])
                self.assertTrue(all(mount.get("readOnly") for mount in pod["containers"][0]["volumeMounts"]))
                if profile == "metrics":
                    self.assertNotIn("loki.source.journal", host.read_text())
                    self.assertNotIn("traces =", host.read_text())
                else:
                    self.assertIn("loki.source.journal", host.read_text())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.work = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def args(self, name="render", **kwargs):
        values = dict(root=str(ROOT), output=str(self.work / name), enabled="true",
                      gpu_exporter="false", trace="false", kubelet="false", node_name="fixture-node",
                      api_address="192.168.50.10", host_address="192.168.50.10",
                      reserve_mib=6656, margin_mib=768, profile="full",
                      workloads="sglang,open-webui", overlay="apps/overlays/dual-gpu")
        values.update(kwargs)
        return argparse.Namespace(**values)

    def test_tariff_allowance_is_additional_and_explicit(self):
        with self.assertRaises(ValueError):
            telemetry.render(self.args(tariff="true"))
        args = self.args(tariff="true", reserve_mib=6720)
        telemetry.render(args)
        evidence = telemetry.verify(args.output)
        self.assertEqual(evidence["component_limits_mib"]["tariff_refresher_mib"], 64)
        self.assertEqual(evidence["calculated_allowance_mib"], 6720)

    def test_real_render_integrity_preserves_baseline(self):
        args = self.args()
        telemetry.render(args)
        evidence = telemetry.verify(args.output)
        self.assertEqual(evidence["status"], "generated-not-deployed")
        self.assertEqual(evidence["profile"], "full")
        self.assertEqual(evidence["component_limits_mib"],
                         {"cluster_stack_mib": 5248, "host_alloy_mib": 512, "hardware_sampler_mib": 128})
        self.assertEqual(evidence["calculated_allowance_mib"], 6656)
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

    def test_metrics_profile_removes_log_trace_components_and_preserves_health(self):
        args = self.args(profile="metrics", reserve_mib=4864)
        telemetry.render(args)
        evidence = telemetry.verify(args.output)
        self.assertEqual(evidence["profile"], "metrics")
        self.assertEqual(evidence["component_limits_mib"]["cluster_stack_mib"], 3456)
        self.assertEqual(evidence["calculated_allowance_mib"], 4864)
        stack = (Path(args.output) / "stack.yaml").read_text()
        workloads = telemetry.load_objects(stack)
        names = {o["metadata"]["name"] for o in workloads if "metadata" in o}
        self.assertFalse({"loki", "tempo"} & names)
        alloy = next(o for o in workloads if o["kind"] == "ConfigMap" and
                     o["metadata"]["name"].startswith("alloy-config-"))
        config = alloy["data"]["config.alloy"]
        self.assertNotIn("otelcol.exporter.otlphttp", config)
        self.assertNotIn("otelcol.exporter.otlp \"tempo\"", config)
        self.assertNotIn("logs =", config)
        self.assertNotIn("traces =", config)
        self.assertIn('"job" = "workstation-host-alloy"',
                      (ROOT / "infrastructure/observability/host/config.metrics.alloy").read_text())
        self.assertIn("workstation_hardware_sample_timestamp_seconds",
                      (ROOT / "infrastructure/observability/config/alerts.yaml").read_text())
        prometheus = next(o for o in workloads if o["kind"] == "ConfigMap" and
                          o["metadata"]["name"].startswith("prometheus-config-"))
        alert_text = prometheus["data"]["alerts.yml"]
        self.assertNotIn("|loki|tempo", alert_text)
        self.assertNotIn("failed_spans", alert_text)
        self.assertNotIn("failed_log_records", alert_text)

    def resolving(self, *addresses):
        return patch.object(telemetry.socket, "getaddrinfo",
                            return_value=[(2, 1, 6, "", (a, 8080)) for a in addresses])

    def test_nas_receiver_keeps_collectors_and_uses_the_dns_name(self):
        for profile, reserve in (("full", 6656), ("metrics", 4864)):
            with self.subTest(profile=profile), self.resolving("192.168.30.10"):
                args = self.args(profile, profile=profile, reserve_mib=reserve,
                                 nas_receiver="microserver.lan.thespry.family")
                telemetry.render(args)
                evidence = telemetry.verify(args.output)
                self.assertEqual(evidence["backend"], {"kind": "nas", "receiver": "microserver.lan.thespry.family",
                                                       "resolved": ["192.168.30.10/32"]})
                objects = telemetry.load_objects((Path(args.output) / "stack.yaml").read_text())
                names = {(o["kind"], o["metadata"]["name"]) for o in objects}
                for backend in telemetry.BACKENDS:
                    self.assertFalse({("Deployment", backend), ("Service", backend),
                                      ("PersistentVolumeClaim", f"{backend}-data")} & names)
                self.assertEqual({n for k, n in names if k == "PersistentVolumeClaim"}, {"alloy-data"})
                self.assertFalse(any(o["kind"] == "ConfigMap" and o["metadata"]["name"].startswith(
                    telemetry.BACKEND_CONFIG_MAPS) for o in objects))
                for kept in ("alloy", "kube-state-metrics"):
                    self.assertIn(("Deployment", kept), names)
                self.assertIn(("DaemonSet", "node-exporter"), names)
                policy = next(o for o in objects if o["metadata"]["name"] == "alloy-to-nas-receiver-8080")
                self.assertEqual(policy["spec"]["egress"][0]["to"], [{"ipBlock": {"cidr": "192.168.30.10/32"}}])
                config = next(o for o in objects if o["kind"] == "ConfigMap" and
                              o["metadata"]["name"].startswith("alloy-config-"))["data"]["config.alloy"]
                for local in ("prometheus:9090", "loki:3100", "tempo:4317", "grafana:3000"):
                    self.assertNotIn(local, config)
                self.assertIn('"http://microserver.lan.thespry.family:8080/api/v1/write"', config)
                self.assertIn('"__metrics_path__" = "/metrics/grafana"', config)
                self.assertNotIn("192.168.30.10", config)
                host = (Path(args.output) / "host.alloy").read_text()
                self.assertIn("http://microserver.lan.thespry.family:8080/api/v1/write", host)
                self.assertNotIn("192.0.2.5", host)
                self.assertNotIn("192.0.2.3", host)
                self.assertIn("192.0.2.4:4318", host)
                if profile == "full":
                    self.assertIn('otelcol.exporter.otlphttp "tempo"', config)
                    self.assertIn("http://microserver.lan.thespry.family:8080/loki/api/v1/push", host)
                (Path(args.output) / "host.alloy").write_text("tampered")
                with self.assertRaises(telemetry.InvalidTelemetry):
                    telemetry.verify(args.output)

    def test_ebpf_collector_needs_the_nas_and_is_rendered_complete(self):
        with self.assertRaises(telemetry.InvalidTelemetry):
            telemetry.render(self.args("local-ebpf", ebpf="true"))
        with self.resolving("192.168.30.10"):
            args = self.args("ebpf", ebpf="true", nas_receiver="microserver.lan.thespry.family")
            telemetry.render(args)
        evidence = telemetry.verify(args.output)
        self.assertTrue(evidence["ebpf"])
        stack = (Path(args.output) / "stack.yaml").read_text()
        self.assertNotIn(telemetry.RECEIVER_PLACEHOLDER, stack)
        objects = telemetry.load_objects(stack)
        daemon = next(o for o in objects if o["kind"] == "DaemonSet" and o["metadata"]["name"] == "alloy-ebpf")
        pod = daemon["spec"]["template"]["spec"]
        self.assertEqual(daemon["metadata"]["namespace"], "workstation-observability-host")
        self.assertTrue(pod["hostPID"] and pod["hostNetwork"] and pod["containers"][0]["securityContext"]["privileged"])
        self.assertIn("127.0.0.1:12346", " ".join(pod["containers"][0]["args"]))
        config = next(o for o in objects if o["kind"] == "ConfigMap" and
                      o["metadata"]["name"].startswith("alloy-ebpf-config-"))["data"]["config.alloy"]
        self.assertIn('"http://microserver.lan.thespry.family:8080"', config)
        self.assertIn('"http://microserver.lan.thespry.family:8080/api/v1/write"', config)
        self.assertIn('exports = ["metrics"]', config)
        self.assertNotIn("payload_extraction", config)
        quota = next(o for o in objects if o["kind"] == "ResourceQuota" and o["metadata"]["name"] == "host-collector-budget")
        self.assertEqual(quota["spec"]["hard"]["pods"], "3")
        self.assertGreaterEqual(evidence["component_limits_mib"]["cluster_stack_mib"], 896 + 1024)
        # The default render has no eBPF collector.
        with self.resolving("192.168.30.10"):
            plain = self.args("plain", nas_receiver="microserver.lan.thespry.family")
            telemetry.render(plain)
        self.assertNotIn("alloy-ebpf", (Path(plain.output) / "stack.yaml").read_text())

    def test_nas_receiver_requires_a_private_dns_name(self):
        for name, addresses in (("192.168.30.10", ["192.168.30.10"]),
                                ("microserver.lan.thespry.family", ["203.0.113.7"])):
            with self.subTest(name=name), self.resolving(*addresses), self.assertRaises(telemetry.InvalidTelemetry):
                telemetry.render(self.args(nas_receiver=name))
        with patch.object(telemetry.socket, "getaddrinfo", side_effect=OSError), \
                self.assertRaises(telemetry.InvalidTelemetry):
            telemetry.render(self.args(nas_receiver="missing.lan.thespry.family"))

    def test_metrics_profile_rejects_trace_and_insufficient_complete_allowance(self):
        for kwargs in ({"profile": "metrics", "trace": "true"},
                       {"profile": "metrics", "reserve_mib": 4863},
                       {"margin_mib": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(telemetry.InvalidTelemetry):
                telemetry.render(self.args(**kwargs))

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
        # The old 46-GiB allowance no longer fits after Grafana's extra 512 MiB.
        # Keep that rejection and test the new exact boundary independently.
        for index, (ram, cpu, fits) in enumerate(((47104, 42, False), (47616, 42, True),
                                                 (112640, 42, True), (45000, 42, False),
                                                 (47616, 20, False))):
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
        for bdf, state in (("0000:01:00.0", "suspended"), ("0000:09:00.0", "DUMMY_SECRET_SENTINEL")):
            (self.work / "bus/pci/devices" / bdf / "power").mkdir(parents=True)
            (self.work / "bus/pci/devices" / bdf / "power/runtime_status").write_text(state + "\n")
        text = telemetry_sample.metrics(sample, self.work)
        self.assertIn('workstation_gpu_runtime_suspended{pci_bdf="0000:01:00.0"} 1', text)
        self.assertNotIn('workstation_gpu_runtime_suspended{pci_bdf="0000:09:00.0"}', text)
        (self.work / "bus/pci/devices/0000:09:00.0/power/runtime_status").write_text("active\n")
        self.assertIn('workstation_gpu_runtime_suspended{pci_bdf="0000:09:00.0"} 0',
                      telemetry_sample.metrics(sample, self.work))
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
