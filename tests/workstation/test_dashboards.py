"""Dashboard navigation, data meaning and profile regressions; no cluster access."""
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib/workstation"))
import telemetry


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.directory = ROOT / "infrastructure/observability/dashboards"
        self.dashboards = {json.loads(p.read_text())["uid"]: json.loads(p.read_text())
                           for p in self.directory.glob("*.json")}

    def test_complete_hierarchy_and_stable_overview_bookmark(self):
        self.assertEqual(set(self.dashboards), {
            "workstation-overview", "workstation-gpus", "workstation-host",
            "workstation-workloads", "workstation-power", "workstation-monitoring",
            "workstation-kubernetes", "workstation-pods"})
        overview = self.dashboards["workstation-overview"]
        self.assertLessEqual(len(overview["panels"]), 12)
        self.assertLessEqual(max(p["gridPos"]["y"] + p["gridPos"]["h"]
                                 for p in overview["panels"]), 24)

    def test_navigation_preserves_time_and_variable_context(self):
        for dashboard in self.dashboards.values():
            with self.subTest(dashboard=dashboard["uid"]):
                self.assertEqual({link["url"] for link in dashboard["links"]},
                                 {"/d/" + uid for uid in self.dashboards})
                self.assertTrue(all(link["keepTime"] and link["includeVars"]
                                    for link in dashboard["links"]))
        panels = self.dashboards["workstation-overview"]["panels"]
        gpu_links = [link for panel in panels
                     for link in panel.get("fieldConfig", {}).get("defaults", {}).get("links", [])]
        self.assertEqual(len(gpu_links), 2)
        self.assertTrue(all("var-gpu=${__field.labels.pci_bdf}" in link["url"]
                            and "${__url_time_range}" in link["url"] for link in gpu_links))

    def test_panels_have_unique_ids_and_nonoverlapping_grid(self):
        for dashboard in self.dashboards.values():
            panels = dashboard["panels"]
            self.assertEqual(len({p["id"] for p in panels}), len(panels))
            for index, left in enumerate(panels):
                a = left["gridPos"]
                self.assertTrue(0 <= a["x"] < 24 and 0 < a["w"] <= 24 - a["x"])
                self.assertTrue(a["y"] >= 0 and a["h"] > 0)
                for right in panels[index + 1:]:
                    b = right["gridPos"]
                    overlap = (a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"]
                               and a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"])
                    self.assertFalse(overlap, (dashboard["uid"], left["title"], right["title"]))

    def test_queries_use_declared_variables_and_thirty_second_refresh(self):
        for dashboard in self.dashboards.values():
            declared = {v["name"] for v in dashboard["templating"]["list"]}
            self.assertEqual(dashboard["refresh"], "30s")
            for panel in dashboard["panels"]:
                for target in panel.get("targets", []):
                    variables = set(re.findall(r"\$\{([a-z]+):regex\}", target.get("expr", "")))
                    self.assertTrue(variables <= declared)
        gpu = self.dashboards["workstation-gpus"]["templating"]["list"][0]
        self.assertEqual(gpu["name"], "gpu")
        self.assertIn("pci_bdf", gpu["definition"])
        self.assertTrue(gpu["includeAll"] and gpu["multi"])

    def test_missing_sensors_are_not_zero_and_energy_scope_is_preserved(self):
        for dashboard in self.dashboards.values():
            for panel in dashboard["panels"]:
                for target in panel.get("targets", []):
                    expr = target.get("expr", "")
                    if "workstation_gpu_" in expr or "workstation_psu_" in expr:
                        self.assertNotIn("or vector(0)", expr)
        power = self.dashboards["workstation-power"]
        panels = {p["id"]: p for p in power["panels"]}
        for panel_id in (105, 106, 107, 108):
            expr = panels[panel_id]["targets"][0]["expr"]
            self.assertIn("<= 90", expr)
            self.assertIn("- time() <= 10", expr)
        self.assertIn("NOT energy for the selected dashboard range", panels[107]["description"])
        self.assertIn("energy_covered_seconds_total", panels[108]["targets"][0]["expr"])

    def test_kubernetes_usage_and_budgets_are_distinct(self):
        panels = self.dashboards["workstation-workloads"]["panels"]
        memory = next(p for p in panels if p["title"] == "Container memory · actual working set")
        self.assertIn("container_memory_working_set_bytes", memory["targets"][0]["expr"])
        self.assertIn('job="kubelet-cadvisor"', memory["targets"][0]["expr"])
        budget = next(p for p in panels if p["title"] == "Memory requests and limits")
        self.assertEqual(len(budget["targets"]), 2)
        self.assertIn("resource_requests", budget["targets"][0]["expr"])
        self.assertIn("resource_limits", budget["targets"][1]["expr"])
        queue = next(p for p in panels if p["title"] == "SGLang queued requests")
        self.assertEqual(queue["targets"][0]["expr"], 'sglang:num_queue_reqs{job="sglang",priority=""}')

    def test_metrics_profile_has_no_absent_log_or_trace_datasource(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / "observability"
            shutil.copytree(ROOT / "infrastructure/observability", stage)
            telemetry.configure_profile(stage, "metrics")
            projected = [json.loads(p.read_text()) for p in (stage / "dashboards").glob("*.json")]
            self.assertEqual({d["uid"] for d in projected}, set(self.dashboards))
            for dashboard in projected:
                for panel in dashboard["panels"]:
                    self.assertNotIn(panel.get("datasource", {}).get("uid"), {"loki", "tempo"})
            monitoring = next(d for d in projected if d["uid"] == "workstation-monitoring")
            self.assertTrue(any(p["title"] == "Logs and traces are disabled" for p in monitoring["panels"]))
        logs = [p for p in self.dashboards["workstation-monitoring"]["panels"] if p["type"] == "logs"]
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["targets"][0]["expr"], '{job=~"workstation-install|workstation-kernel"}')

    def test_packaged_dashboards_match_kustomize_assets(self):
        import yaml
        source = ROOT / "infrastructure/observability/kustomization.yaml"
        generator = next(g for g in yaml.safe_load(source.read_text())["configMapGenerator"]
                         if g["name"] == "workstation-dashboard")
        self.assertEqual({value.split("=", 1)[1] for value in generator["files"]},
                         {"dashboards/" + p.name for p in self.directory.glob("*.json")})
        for manifest in ("bridge-runtime.files", "source.files"):
            names = (ROOT / "infrastructure/packages/bootstrap" / manifest).read_text().splitlines()
            for path in self.directory.glob("*.json"):
                self.assertEqual(names.count(str(path.relative_to(ROOT))), 1)

    def test_cluster_views_distinguish_active_pods_history_and_budgets(self):
        cluster = self.dashboards["workstation-kubernetes"]
        panels = {p["title"]: p for p in cluster["panels"]}
        not_ready = panels["Active Pods not Ready"]["targets"][0]["expr"]
        self.assertIn('phase=~"Pending|Running|Unknown"', not_ready)
        self.assertIn("and on(namespace,pod)", not_ready)
        self.assertNotIn("or vector(0)", not_ready)
        self.assertIn("Succeeded|Failed", panels["Retained terminal Pods · history"]["targets"][0]["expr"])
        self.assertIn("not enforced disk quotas", panels["PersistentVolumeClaim requested capacity"]["description"])
        self.assertIn("not free VRAM", panels["GPU allocation slots · node"]["description"])
        replicas = panels["Deployment replicas · desired and available"]["targets"][0]["expr"]
        self.assertIn("kube_deployment_spec_replicas", replicas)
        self.assertIn("kube_deployment_status_replicas_available", replicas)
        pods = self.dashboards["workstation-pods"]
        phase = next(v for v in pods["templating"]["list"] if v["name"] == "phase")
        self.assertEqual(set(phase["current"]["value"]), {"Pending", "Running", "Unknown"})
        for panel in pods["panels"]:
            for target in panel.get("targets", []):
                expr = target["expr"]
                if "pod=" in expr:
                    self.assertIn("${namespace:regex}", expr)
                    self.assertIn("${pod:regex}", expr)
                    self.assertIn("${phase:regex}", expr)

    def test_gpu_sleeping_sensor_gaps_are_explained_without_zero_activity(self):
        gpu = self.dashboards["workstation-gpus"]
        intro = next(p for p in gpu["panels"] if p["type"] == "text")
        self.assertIn("runtime-suspended", intro["options"]["content"])
        status = next(p for p in gpu["panels"] if p["title"] == "Activity / clock sensor availability")
        expr = status["targets"][0]["expr"]
        self.assertIn("workstation_gpu_sensor_available", expr)
        self.assertIn("<= 90", expr)
        self.assertIn("- time() <= 10", expr)
        self.assertNotIn("or vector(0)", expr)
        options = status["fieldConfig"]["defaults"]["mappings"][0]["options"]
        self.assertIn("may be asleep", options["0"]["text"])
        self.assertEqual(options["1"]["text"], "Readable")
        self.assertEqual(status["fieldConfig"]["defaults"]["noValue"], "Telemetry unavailable")
        for uid in ("workstation-overview", "workstation-power"):
            text = " ".join(p.get("options", {}).get("content", "") for p in self.dashboards[uid]["panels"])
            self.assertIn("runtime-suspended", text)

    def test_cluster_collectors_are_explicit_and_bounded(self):
        import yaml
        docs = list(yaml.safe_load_all((ROOT / "infrastructure/observability/host-metrics.yaml").read_text()))
        deployment = next(d for d in docs if d["kind"] == "Deployment")
        args = deployment["spec"]["template"]["spec"]["containers"][0]["args"]
        namespaces = next(a.split("=", 1)[1].split(",") for a in args if a.startswith("--namespaces="))
        self.assertEqual(set(namespaces), {"default", "ai-home-lab", "workstation-observability",
                                         "workstation-observability-host", "kube-system", "kube-amd-gpu",
                                         "kube-node-lease", "kube-public"})
        allowed = next(a.split("=", 1)[1].split(",") for a in args if a.startswith("--metric-allowlist="))
        self.assertEqual(len(allowed), 16)
        self.assertTrue(all(a.startswith("^") and a.endswith("$") for a in allowed))
        self.assertTrue(any("kube_deployment_spec_replicas" in a for a in allowed))
        self.assertTrue(any("kube_pod_container_status_waiting_reason" in a for a in allowed))
        self.assertIn("--resources=pods,nodes,deployments,persistentvolumeclaims", args)
        cgroup = (ROOT / "infrastructure/observability/kubelet/config.alloy").read_text()
        for namespace in namespaces:
            self.assertIn(namespace, cgroup)
        self.assertIn('sample_limit = 30000', cgroup)
        self.assertIn('body_size_limit = "16MiB"', cgroup)


if __name__ == "__main__":
    unittest.main()
