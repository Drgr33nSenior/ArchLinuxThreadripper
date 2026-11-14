"""Native sandbox render, capacity, network-policy union and group boundaries."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib/workstation"))
import sandbox
import service

module = importlib.util.spec_from_file_location("sandbox_control", ROOT / "infrastructure/ai-stack/sandbox/control.py")
control = importlib.util.module_from_spec(module)
module.loader.exec_module(control)
CONFIG = {"image": "registry.example.test/sandbox@sha256:" + "a" * 64,
          "parents_group_id": "parents-fixture", "family_group_id": "family-fixture",
          "api_server_cidr": "192.168.77.9/32", "api_server_port": 6443}
SITE = {"context": "fixture", "node": "fixture", "node_address": "192.168.77.9",
        "management_cidr": "192.168.77.0/24", "client_cidrs": ["192.168.77.0/24"],
        "webui_host": "chat.fixture.test", "api_host": "api.fixture.test", "tls_secret": "fixture-tls"}


class RenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.objects = sandbox.render(CONFIG, ROOT)
        cls.telemetry = service.kustomize(ROOT / "infrastructure/observability")

    def test_render_flag_off_and_on_and_shared_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "site.json").write_text(json.dumps(SITE))
            (root / "sandbox.json").write_text(json.dumps(CONFIG))
            for enabled in (False, True):
                out = root / str(enabled)
                args = SimpleNamespace(site=root / "site.json", output=out, mode="large",
                    experience="assistant-memory", serving_profile=ROOT / "infrastructure/ai-stack/serving/qwen3.8-27b-mxfp4-tp2.json",
                    sandbox_config=root / "sandbox.json" if enabled else None)
                service.render(args)
                workloads = service.load_objects((out / "workloads.yaml").read_text())
                webui = next(o for o in workloads if o["kind"] == "Deployment" and o["metadata"]["name"] == "open-webui")
                env = {e["name"]: e.get("value") for e in webui["spec"]["template"]["spec"]["containers"][0]["env"]}
                self.assertEqual(bool(json.loads(env["TERMINAL_SERVER_CONNECTIONS"])), enabled)
                self.assertIn("TERMINAL_SERVER_CONNECTIONS", env["WEBUI_PINNED_ENV"].split(","))
                self.assertEqual(bool((out / "sandbox.yaml").read_text()), enabled)
                if enabled:
                    args.mode, args.output = "shared", root / "shared"
                    with self.assertRaisesRegex(ValueError, "shared capacity"):
                        service.render(args)
                    self.assertFalse(args.output.exists())

    def test_pool_fits_large_assistant_and_memory_with_full_telemetry(self):
        resources = {"schema_version": 1, "status": "offline-plan-not-applied", "allocatable_logical_cpus": 42,
                     "gpu_count": 2, "memory": {"total_mib": 63621, "allocatable_mib": 51200}}
        for experience in ("assistant", "assistant-memory"):
            objects = service.kustomize(ROOT / "apps/overlays" / experience)
            contract = service.serving_contract(ROOT / "infrastructure/ai-stack/serving/qwen3.8-27b-mxfp4-tp2.json",
                None, None, service.DEFAULT_OUTPUT_TOKENS, mode="large")
            objects, _, _ = service.apply_contract(objects, contract, "large", experience)
            result = service.capacity(resources, objects + self.objects, {"reserve_mib": 6656}, self.telemetry, "large")
            self.assertTrue(result["fits"], result)
            pool = next(r for r in result["rows"] if r["name"] == "assistant-sandbox-pool")
            self.assertEqual((1.75, 4352), (pool["cpu"], pool["memory_mib"]))
            print("sandbox capacity", experience, result["remaining_cpus"], result["remaining_mib"])
            self.assertFalse(service.capacity(resources, objects + self.objects, {"reserve_mib": 6656},
                                             self.telemetry, "large", vm_mib=15000)["fits"])

    def test_security_idle_no_credentials_or_gpu_in_workspace(self):
        policy = sandbox.policy(CONFIG)
        spec = policy["podTemplate"]["spec"]
        self.assertFalse(spec["automountServiceAccountToken"])
        self.assertFalse(spec["enableServiceLinks"])
        self.assertEqual("", policy["storage"])
        self.assertEqual((15, 120), (policy["idle_timeout_minutes"], policy["max_lifetime_minutes"]))
        self.assertTrue(all("emptyDir" in v for v in spec["volumes"]))
        self.assertFalse(any(k in spec for k in ("hostNetwork", "hostPID", "hostIPC", "resourceClaims")))
        security = policy["container_security_context"]
        self.assertTrue(security["readOnlyRootFilesystem"])
        self.assertTrue(security["runAsNonRoot"])
        self.assertFalse(security["allowPrivilegeEscalation"])
        self.assertEqual(["ALL"], security["capabilities"]["drop"])
        self.assertEqual("RuntimeDefault", security["seccompProfile"]["type"])
        self.assertEqual(spec["containers"][0]["resources"]["requests"], spec["containers"][0]["resources"]["limits"])
        self.assertEqual(0, next(o for o in self.objects if o["kind"] == "Deployment")["spec"]["replicas"])
        role = next(o for o in self.objects if o["kind"] == "Role")
        self.assertEqual(["pods", "services", "secrets"], role["rules"][0]["resources"])

    def test_network_union_workspace_has_no_egress_even_with_existing_dns_policy(self):
        objects = self.objects + service.kustomize(ROOT / "apps/overlays/assistant-memory") + self.telemetry
        def matching(namespace, labels, direction):
            return [o["spec"] for o in objects if o["kind"] == "NetworkPolicy"
                and o["metadata"].get("namespace") == namespace and direction in o["spec"]["policyTypes"]
                and all(labels.get(k) == v for k, v in o["spec"]["podSelector"].get("matchLabels", {}).items())]
        rules = matching(sandbox.NAMESPACE, {sandbox.LABEL: "assistant-workspace"}, "Egress")
        self.assertTrue(rules)
        self.assertEqual([], [rule for policy in rules for rule in policy.get("egress", [])])
        # An empty rule {} would allow the LAN, cluster, DNS and Internet; reject it.
        ingress = matching(sandbox.NAMESPACE, {sandbox.LABEL: "assistant-workspace"}, "Ingress")
        self.assertEqual([{"from": [sandbox.peer(sandbox.NAMESPACE, sandbox.CONTROL)],
                           "ports": [{"port": 8000, "protocol": "TCP"}]}],
                         [rule for policy in ingress for rule in policy.get("ingress", [])])

    def test_exact_access_grants_no_family_by_default_and_config_validation(self):
        for enabled in (False, True):
            objects = [{"kind": "Deployment", "metadata": {"name": "open-webui"},
                        "spec": {"template": {"spec": {"containers": [{}]}}}}]
            sandbox.configure_webui(objects, {**CONFIG, "family_enabled": enabled})
            connection = json.loads(objects[0]["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"])[0]
            self.assertEqual("session", connection["auth_type"])
            self.assertFalse(connection["config"]["contexts"]["automation"])
            self.assertEqual(["parents-fixture"] + (["family-fixture"] if enabled else []),
                             [g["principal_id"] for g in connection["config"]["access_grants"]])
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            for change in ({"image": "example:latest"}, {"api_server_cidr": "192.168.0.0/16"},
                           {"family_enabled": "false"}, {"parents_group_id": "family-fixture"}, {"egress": "all"}):
                path.write_text(json.dumps({**CONFIG, **change}))
                with self.assertRaises(ValueError):
                    sandbox.read_config(path)


class GateTests(unittest.IsolatedAsyncioTestCase):
    async def test_identity_forgery_roles_group_revocation_and_no_content_metrics(self):
        forwarded = []
        role, groups, uid, failure, malformed = "user", [{"id": "parents-fixture"}], "alice", False, False
        async def identity(request):
            if failure:
                return httpx.Response(503)
            if malformed:
                return httpx.Response(200, json=[])
            return httpx.Response(200, json={"id": uid, "role": role} if request.url.path.endswith("auths/") else groups)
        async def upstream(scope, receive, send):
            forwarded.append(scope)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(identity)) as verifier:
            gate = control.Gate(upstream, CONFIG, b'{"paths":{}}', verifier)
            with patch.dict(os.environ, {"TERMINALS_OPEN_WEBUI_URL": "http://fixture"}):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gate), base_url="http://gate") as client:
                    headers = {"Authorization": "Bearer fixture", "X-User-Id": "alice", "X-Terminal-Context-Id": "forged"}
                    self.assertEqual(200, (await client.post("/p/sandbox/execute", headers=headers, content="PRIVATE-CODE")).status_code)
                    self.assertNotIn(b"x-terminal-context-id", dict(forwarded[0]["headers"]))
                    for name, value in (("Authorization", "Bearer other-fixture"), ("X-User-Id", "bob")):
                        duplicate = list(headers.items()) + [(name, value)]
                        self.assertEqual(403, (await client.post("/p/sandbox/execute", headers=duplicate)).status_code)
                    malformed = True
                    self.assertEqual(403, (await client.post("/p/sandbox/execute", headers=headers)).status_code)
                    malformed = False
                    uid = "bob"
                    self.assertEqual(403, (await client.post("/p/sandbox/execute", headers=headers)).status_code)
                    uid, groups = "alice", [{"id": "parents-fixture"}, {"id": "family-fixture"}]
                    self.assertEqual(403, (await client.post("/p/sandbox/execute", headers=headers)).status_code)
                    role = "admin"
                    self.assertEqual(200, (await client.post("/p/sandbox/execute", headers=headers)).status_code)
                    role, groups = "pending", []
                    self.assertEqual(403, (await client.post("/p/sandbox/execute", headers=headers)).status_code)
                    failure = True
                    self.assertEqual(403, (await client.get("/p/sandbox/files/cwd", headers=headers)).status_code)
                    for path in ("/execute", "/api/v1/policies", "/p/other/execute"):
                        self.assertEqual(403, (await client.post(path, headers=headers)).status_code)
                    self.assertEqual(200, (await client.get("/p/sandbox/openapi.json")).status_code)
                    metrics = (await client.get("/metrics")).text
                    self.assertIn('group="Parents"} 1', metrics)
                    self.assertIn('group="Admin"} 1', metrics)
                    self.assertNotIn("alice", metrics)
                    self.assertNotIn("PRIVATE-CODE", metrics)
                    self.assertEqual(2, len(forwarded))


if __name__ == "__main__":
    unittest.main()
