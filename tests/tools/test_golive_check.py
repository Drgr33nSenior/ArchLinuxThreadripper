"""tools/sandbox/golive_check.py: diff parsing, allowlists and static bundle checks (fixtures only)."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools/sandbox/golive_check.py"
sys.path.insert(0, str(TOOL.parent))
import golive_check as check

IMAGE = "registry.example.invalid/assistant-sandbox@sha256:" + "a" * 64
EGRESS = "registry.example.invalid/sandbox-egress@sha256:" + "b" * 64
CONFIG = {"image": IMAGE, "egress_image": EGRESS, "parents_group_id": "gp", "family_group_id": "gf",
          "family_enabled": True, "group_resolution": "most-permissive",
          "egress_service_ips": {"family": "10.43.0.201", "parents": "10.43.0.202"},
          "api_server_cidr": "10.0.30.11/32", "api_server_port": 6443, "family_dns": ["1.1.1.3", "1.0.0.3"]}
SITE = {"NAS_ADDRESS": "10.0.30.10", "WORKSTATION_LAN_ADDRESS": "10.0.1.40"}


def quiet(function, *args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        result = function(*args)
    return result, out.getvalue()


def write(directory, name, value, dump=json.dumps):
    path = Path(directory) / name
    path.write_text(value if isinstance(value, str) else dump(value))
    return path


def deployment(name, namespace="assistant-sandbox", image=IMAGE, replicas=0, env=None):
    return {"kind": "Deployment", "metadata": {"name": name, "namespace": namespace},
            "spec": {"replicas": replicas, "template": {"spec": {
                "securityContext": {"fsGroup": 2002},
                "containers": [{"name": name, "image": image, "env": env or [],
                                "securityContext": {"runAsUser": 2002, "runAsGroup": 2002}}]}}}}


def bundle_objects():
    ids = {"runAsUser": 2002, "runAsGroup": 2002}
    policy = {"pod_security_context": {**ids, "fsGroup": 2002}, "container_security_context": ids}
    return [
        {"kind": "Namespace", "metadata": {"name": "assistant-sandbox",
                                           "labels": {"pod-security.kubernetes.io/enforce": "restricted"}}},
        deployment("assistant-terminals"), deployment("sandbox-egress-family", image=EGRESS),
        deployment("sandbox-egress-parents", image=EGRESS),
        {"kind": "Service", "metadata": {"name": "sandbox-egress-family", "namespace": "assistant-sandbox"},
         "spec": {"clusterIP": "10.43.0.201"}},
        {"kind": "ConfigMap", "metadata": {"name": "sandbox-config", "namespace": "assistant-sandbox"},
         "data": {"policies.json": json.dumps({"family": policy, "parents": policy})}},
        {"kind": "NetworkPolicy", "metadata": {"name": "webui-sandbox", "namespace": "ai-home-lab"}},
    ]


class DiffTests(unittest.TestCase):
    def test_named_lists_compare_by_name(self):
        old = check.keyed({"env": [{"name": "A", "value": "1"}, {"name": "B", "value": "2"}]})
        new = check.keyed({"env": [{"name": "B", "value": "2"}, {"name": "A", "value": "3"}]})
        self.assertEqual([("env", "[A]", "value")], check.paths(old, new))

    def test_clean_drops_server_fields_and_the_last_applied_annotation(self):
        obj = {"kind": "X", "status": {"a": 1}, "metadata": {"name": "n", "uid": "u", "resourceVersion": "1",
                                                             "annotations": {check.LAST_APPLIED: "{}"}}}
        self.assertEqual({"kind": "X", "metadata": {"name": "n"}}, check.clean(obj))

    def test_kdiff_reports_added_changed_and_unchanged_objects(self):
        live_dep = deployment("open-webui", "ai-home-lab", env=[{"name": "TERMINAL_SERVER_CONNECTIONS", "value": "[]"}])
        merged_dep = deployment("open-webui", "ai-home-lab", env=[{"name": "TERMINAL_SERVER_CONNECTIONS", "value": "[1]"}])
        merged_dep["metadata"]["resourceVersion"] = "9"
        same = {"kind": "Service", "metadata": {"name": "s", "namespace": "ai-home-lab"}}
        with tempfile.TemporaryDirectory() as directory:
            live, merged = Path(directory) / "live", Path(directory) / "merged"
            live.mkdir(), merged.mkdir()
            write(live, "dep", live_dep, yaml.safe_dump), write(merged, "dep", merged_dep, yaml.safe_dump)
            write(live, "svc", same, yaml.safe_dump), write(merged, "svc", same, yaml.safe_dump)
            write(live, "new", ""), write(merged, "new", {"kind": "PersistentVolume", "metadata": {"name": "p"}},
                                          yaml.safe_dump)
            out = Path(directory) / "diff.json"
            with mock.patch.dict(os.environ, {"GOLIVE_DIFF_OUT": str(out)}):
                self.assertEqual(1, check.kdiff(live, merged))
            report = json.loads(out.read_text())
        self.assertEqual([
            {"kind": "Deployment", "namespace": "ai-home-lab", "name": "open-webui", "state": "changed",
             "paths": ["spec.template.spec.containers.[open-webui].env.[TERMINAL_SERVER_CONNECTIONS].value"]},
            {"kind": "PersistentVolume", "namespace": "", "name": "p", "state": "added"}], report)


WEBUI_DIFF = {"kind": "Deployment", "namespace": "ai-home-lab", "name": "open-webui", "state": "changed",
              "paths": ["spec.template.spec.containers.[open-webui].env.[TERMINAL_SERVER_CONNECTIONS].value"]}


class AllowTests(unittest.TestCase):
    def allow(self, report, profile, names=()):
        with tempfile.TemporaryDirectory() as directory:
            return quiet(check.allow, write(directory, "diff.json", report), profile, list(names))

    def test_webui_allows_only_the_named_env_values(self):
        self.assertEqual(0, self.allow([WEBUI_DIFF], "webui", ["TERMINAL_SERVER_CONNECTIONS"])[0])
        self.assertEqual(1, self.allow([WEBUI_DIFF], "webui", [])[0])
        image = {**WEBUI_DIFF, "paths": ["spec.template.spec.containers.[open-webui].image"]}
        result, out = self.allow([image], "webui", ["TERMINAL_SERVER_CONNECTIONS"])
        self.assertEqual(1, result)
        self.assertIn("REFUSED", out)
        self.assertEqual(1, self.allow([{**WEBUI_DIFF, "name": "litellm"}], "webui", ["TERMINAL_SERVER_CONNECTIONS"])[0])
        self.assertEqual(0, self.allow("", "webui")[0])

    def test_sandbox_allows_its_namespace_and_the_two_foreign_policies_once(self):
        ok = [{"kind": "Deployment", "namespace": "assistant-sandbox", "name": "x", "state": "added"},
              {"kind": "Namespace", "namespace": "", "name": "assistant-sandbox", "state": "added"},
              {"kind": "NetworkPolicy", "namespace": "ai-home-lab", "name": "webui-sandbox", "state": "added"}]
        self.assertEqual(0, self.allow(ok, "sandbox")[0])
        changed = [{**ok[2], "state": "changed", "paths": ["spec"]}]
        self.assertEqual(1, self.allow(changed, "sandbox")[0])
        self.assertEqual(1, self.allow([{**ok[0], "namespace": "ai-home-lab"}], "sandbox")[0])
        self.assertEqual(1, self.allow([{**ok[0], "state": "removed"}], "sandbox")[0])

    def test_homes_are_added_once_never_changed(self):
        pv = {"kind": "PersistentVolume", "namespace": "", "name": "sandbox-home-kid", "state": "added"}
        pvc = {"kind": "PersistentVolumeClaim", "namespace": "assistant-sandbox", "name": "sandbox-home-kid",
               "state": "added"}
        self.assertEqual(0, self.allow([pv, pvc], "homes")[0])
        self.assertEqual(1, self.allow([{**pv, "state": "changed", "paths": ["spec"]}], "homes")[0])
        self.assertEqual(1, self.allow([{**pv, "name": "other"}], "homes")[0])

    def test_an_unknown_profile_stops(self):
        with self.assertRaises(SystemExit):
            self.allow([WEBUI_DIFF], "other")


class RenderTests(unittest.TestCase):
    def bundle(self, objects, evidence=None, users=2):
        evidence = evidence or {"mode": "shared", "sandbox": {"enabled": True, "family_enabled": True,
                                                              "group_resolution": "most-permissive",
                                                              "persistent_homes": users}}
        with tempfile.TemporaryDirectory() as directory:
            return quiet(check.bundle, write(directory, "sandbox.yaml", objects, yaml.safe_dump_all),
                         write(directory, "evidence.json", evidence), write(directory, "sandbox.json", CONFIG),
                         write(directory, "users.json", {"users": [{}] * users}))

    def test_a_good_bundle_passes(self):
        result, out = self.bundle(bundle_objects())
        self.assertEqual(0, result, out)

    def test_bundle_problems_are_refused(self):
        objects = bundle_objects()
        objects[1]["spec"]["replicas"] = 1
        objects[2]["spec"]["template"]["spec"]["containers"][0]["image"] = "registry.example.invalid/x@sha256:" + "c" * 64
        objects[1]["spec"]["template"]["spec"]["containers"][0]["securityContext"]["runAsUser"] = 1000
        objects[4]["spec"]["clusterIP"] = "10.43.0.9"
        objects.append({"kind": "Secret", "metadata": {"name": "s", "namespace": "kube-system"}})
        result, out = self.bundle(objects, users=1)
        self.assertEqual(1, result)
        for text in ("not rendered at zero replicas", "image that is not in sandbox.json", "UID/GID/fsGroup 2002",
                     "address differs", "unexpected object Secret kube-system/s"):
            self.assertIn(text, out)
        result, out = self.bundle(bundle_objects(), users=3)
        self.assertIn("persistent_homes", out)

    def test_namespace_and_pick(self):
        with tempfile.TemporaryDirectory() as directory:
            sandbox = write(directory, "sandbox.yaml", bundle_objects(), yaml.safe_dump_all)
            out = Path(directory) / "ns.yaml"
            self.assertEqual(0, check.namespace(sandbox, out))
            self.assertEqual("Namespace", yaml.safe_load(out.read_text())["kind"])
            apply = write(directory, "apply.json", {"items": [deployment("open-webui", "ai-home-lab", replicas=1)]})
            picked = Path(directory) / "webui.json"
            self.assertEqual(0, quiet(check.pick, apply, "open-webui", picked)[0])
            self.assertEqual("open-webui", json.loads(picked.read_text())["metadata"]["name"])
            with self.assertRaises(SystemExit):
                quiet(check.pick, apply, "missing", picked)

    def test_capacity(self):
        plan = {"status": "ok", "fits": True, "mode": "shared", "required_gpus": 2, "remaining_cpus": 4,
                "remaining_mib": 9000, "remaining_mib_after_telemetry_reserve": 7000,
                "limits": {"cpu": 40, "memory_mib": 90000, "over_node_cpus": 0, "over_node_mib": 0},
                "rows": [{"name": "platform"}, {"name": "assistant-sandbox-pool", "cpu": 1, "memory_mib": 512,
                                                 "limit_cpu": 4, "limit_mib": 8192}]}
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(0, quiet(check.capacity, write(directory, "c.json", plan))[0])
            self.assertEqual(1, quiet(check.capacity, write(directory, "c.json", {**plan, "fits": False}))[0])
            no_pool = {**plan, "rows": [{"name": "platform"}]}
            self.assertEqual(1, quiet(check.capacity, write(directory, "c.json", no_pool))[0])
            no_pods = {**plan, "rows": plan["rows"][1:]}
            self.assertEqual(1, quiet(check.capacity, write(directory, "c.json", no_pods))[0])

    def test_webui_connection(self):
        connection = [{"id": "family-sandbox", "name": "Private coding workspace",
                       "url": "http://assistant-terminals.assistant-sandbox.svc:3000",
                       "config": {"access_grants": [{"principal_id": "gp"}, {"principal_id": "gf"}]}}]
        dep = deployment("open-webui", "ai-home-lab", env=[{"name": "TERMINAL_SERVER_CONNECTIONS",
                                                            "value": json.dumps(connection)}])
        with tempfile.TemporaryDirectory() as directory:
            config = write(directory, "sandbox.json", CONFIG)
            self.assertEqual(0, quiet(check.webui, write(directory, "d.json", dep), config)[0])
            config = write(directory, "sandbox.json", {**CONFIG, "family_enabled": False})
            self.assertEqual(1, quiet(check.webui, write(directory, "d.json", dep), config)[0])


class ProbeTests(unittest.TestCase):
    def probe(self, group, other=None):
        with tempfile.TemporaryDirectory() as directory:
            _, out = quiet(check.probe, group, write(directory, "sandbox.json", CONFIG), other)
        return json.loads(out)

    def test_the_spec_uses_the_site_addresses(self):
        with mock.patch.dict(os.environ, SITE):
            family, parents = self.probe("family", "10.42.0.99"), self.probe("parents")
        self.assertEqual(["10.0.30.10", 2049], family["closed"]["NAS"])
        self.assertEqual(["10.0.1.40", 22], family["closed"]["LAN"])
        self.assertEqual(["10.0.30.11", 6443], family["closed"]["node API"])
        self.assertEqual(["10.43.0.202", 4750], family["closed"]["parents proxy"])
        self.assertEqual(["10.42.0.99", 8000], family["closed"]["other user's workspace"])
        self.assertEqual({"own proxy": ["10.43.0.201", 4750]}, family["open"])
        self.assertIn("10.0.1.40.nip.io:22", family["denied"])
        self.assertIn("nudity.testcategory.com", family["denied"])
        self.assertIn("nudity.testcategory.com", parents["allowed"])
        self.assertEqual({"own proxy": ["10.43.0.202", 4750]}, parents["open"])

    def test_missing_site_addresses_stop(self):
        for name in SITE:
            with self.subTest(name), mock.patch.dict(os.environ, {**SITE, name: ""}), self.assertRaises(SystemExit):
                self.probe("family")


class CommandTests(unittest.TestCase):
    def test_usage(self):
        for args, code in ((["--help"], 0), ([], 2), (["unknown"], 2)):
            result = subprocess.run([sys.executable, str(TOOL), *args], check=False, capture_output=True, text=True)
            self.assertEqual(code, result.returncode)
            self.assertIn("Usage: golive_check.py", result.stdout)


if __name__ == "__main__":
    unittest.main()
