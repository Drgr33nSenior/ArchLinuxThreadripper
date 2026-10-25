"""Software boundaries with deterministic backends; never a GPU/DR qualification."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "apps/base/shared-api"), str(ROOT / "lib/workstation")]
import boundary
import service_backup
import service


async def request(app, path, body=None, headers=None, method="POST"):
    queue = asyncio.Queue()
    await queue.put({"type": "http.request", "body": json.dumps(body).encode(), "more_body": False})
    messages = []
    async def send(message):
        messages.append(message)
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": b"", "method": method,
             "headers": list((headers or {}).items()), "client": ("127.0.0.1", 1), "server": ("test", 80)}
    await app(scope, queue.get, send)
    return messages


class BoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.control = self.root / "control"
        self.control.write_text("c" * 32)
        self.mapping = self.root / "mapping"
        self.mapping.write_text(json.dumps({"service_key": "s" * 32, "users": {
            "alice": {"key": "sk-" + "a" * 32, "expires_at": 9999999999, "images": True},
            "expired": {"key": "sk-" + "e" * 32, "expires_at": 1}}}))
        self.seen = []
        async def backend(scope, receive, send):
            self.seen.append((scope, json.loads((await receive())["body"])))
            await boundary.reply(send, 200, {"choices": [{"message": {"tool_calls": [{"id": "call-1"}]}}],
                                             "usage": {"completion_tokens": 1}})
        self.gateway = boundary.GatewayBoundary(backend, self.mapping, self.control)
        self.gateway.state = {"phase": "ready", "model": "Qwen3.8-27B-FP8", "images": False}
        self.chat = {"model": "Qwen3.8-27B-FP8", "messages": [{"role": "user", "content": "fixture"}]}

    async def test_model_identity_and_output_limits(self):
        for data, expected in (({**self.chat, "model": "other"}, 404),
                               ({**self.chat, "model": "Qwen3.5-9B"}, 503),
                               ({**self.chat, "max_tokens": 4097}, 400),
                               ({**self.chat, "api_base": "https://unexpected.invalid"}, 400)):
            result = await request(self.gateway, "/v1/chat/completions", data)
            self.assertEqual(expected, result[0]["status"])
        self.assertEqual([], self.seen)

    async def test_browser_identity_translation_removes_untrusted_fields(self):
        headers = {b"authorization": b"Bearer " + b"s" * 32, b"x-openwebui-user-id": b"alice",
                   b"cookie": b"private", b"x-openwebui-user-email": b"private"}
        result = await request(self.gateway, "/webui/v1/chat/completions", {**self.chat, "user": "mallory"}, headers)
        self.assertEqual(200, result[0]["status"])
        scope, body = self.seen[0]
        self.assertEqual(b"Bearer sk-" + b"a" * 32, dict(scope["headers"])[b"authorization"])
        self.assertNotIn("user", body)
        self.assertNotIn(b"cookie", dict(scope["headers"]))
        self.assertEqual(4096, body["max_tokens"])
        self.assertIn(b'"tool_calls"', result[-1]["body"])

    async def test_identity_spoof_expired_and_unknown_browser_are_denied(self):
        for path, auth, uid, expected in (("/v1/chat/completions", b"s" * 32, b"alice", 400),
            ("/webui/v1/chat/completions", b"bad", b"alice", 401),
            ("/webui/v1/chat/completions", b"s" * 32, b"expired", 403),
            ("/webui/v1/chat/completions", b"s" * 32, b"other", 403)):
            result = await request(self.gateway, path, self.chat,
                {b"authorization": b"Bearer " + auth, b"x-openwebui-user-id": uid})
            self.assertEqual(expected, result[0]["status"])
        self.assertEqual([], self.seen)

    async def test_drain_cancel_and_restart_never_fabricate_ready(self):
        result = await request(self.gateway, "/_service/control", {"phase": "ready", "model": "Qwen3.5-9B", "cancel": True},
                               {b"authorization": b"Bearer " + b"c" * 32})
        self.assertEqual(400, result[0]["status"])
        self.assertEqual("Qwen3.8-27B-FP8", self.gateway.state["model"])
        await request(self.gateway, "/_service/control", {"phase": "draining"},
                      {b"authorization": b"Bearer " + b"c" * 32})
        self.assertEqual(503, (await request(self.gateway, "/v1/chat/completions", self.chat))[0]["status"])
        restarted = boundary.GatewayBoundary(self.gateway.app, self.mapping, self.control)
        self.assertEqual("unavailable", restarted.state["phase"])

    async def test_deadline_releases_slot_and_stream_has_no_fake_done(self):
        async def slow(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"partial", "more_body": True})
            await asyncio.sleep(60)
        self.gateway.app = slow
        self.gateway.request_timeout = 0.01
        result = await request(self.gateway, "/v1/chat/completions", self.chat)
        self.assertEqual(1, len([m for m in result if m["type"] == "http.response.start"]))
        self.assertNotIn(b"[DONE]", b"".join(m.get("body", b"") for m in result))
        self.assertFalse(self.gateway.active)

    async def test_active_cancellation_is_bounded(self):
        entered = asyncio.Event()
        async def slow(scope, receive, send):
            entered.set()
            await asyncio.sleep(60)
        self.gateway.app = slow
        task = asyncio.create_task(request(self.gateway, "/v1/chat/completions", self.chat))
        await entered.wait()
        await request(self.gateway, "/_service/control", {"phase": "draining", "cancel": True},
                      {b"authorization": b"Bearer " + b"c" * 32})
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.gateway.active)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.capture = self.root / "capture"
        self.capture.mkdir()
        with sqlite3.connect(self.capture / "state.db") as database:
            database.execute("CREATE TABLE fixture (id INTEGER)")
            database.execute("INSERT INTO fixture VALUES (7)")
        for name in ("server-token", "server.tar", "config.tar", "litellm.dump", "roles.sql",
                     "webui-rag-qwen3-data.tar", "creative-models.tar", "service-tools-state.tar",
                     "models.json", "rag-SHA256SUMS"):
            (self.capture / name).write_text("synthetic fixture; no real credentials")
        record = {"schema_version": 1, "status": "consistent-capture", "database": "k3s-sqlite",
                  "claims": sorted(service_backup.CLAIMS),
                  "files": {p.name: service_backup.digest(p) for p in self.capture.iterdir()}}
        (self.capture / "manifest.json").write_text(json.dumps(record))

    def test_isolated_staging_checks_database_and_token_pair(self):
        output = self.root / "restore"
        service_backup.restore_stage(self.capture, output)
        with sqlite3.connect(output / "state.db") as db:
            self.assertEqual((7,), db.execute("SELECT id FROM fixture").fetchone())
        with self.assertRaises(ValueError):
            service_backup.restore_stage(self.capture, output)
        (self.capture / "server-token").write_text("different synthetic token")
        with self.assertRaisesRegex(ValueError, "pairing"):
            service_backup.validate_capture(self.capture)

    def test_active_targets_and_incomplete_capture_refused(self):
        for path in ("/etc/restore-test", "/var/lib/rancher/k3s/restore", "/var/lib/kubelet/restore", "/var"):
            with self.assertRaises(ValueError):
                service_backup.restore_stage(self.capture, path)
        (self.capture / "manifest.json").write_text('{"status":"partial"}')
        with self.assertRaises(ValueError):
            service_backup.validate_capture(self.capture)

    def test_failed_scale_preserves_original_identity_before_mutation(self):
        capture = object.__new__(service_backup.Capture)
        capture.kube = ["fixture-kubectl"]
        capture.get = lambda *args: {"metadata": {"uid": "original", "resourceVersion": "7"},
            "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "webui"}}}}
        def failed_run(argv, **kwargs):
            journal = json.loads((self.root / "journal.json").read_text())
            self.assertEqual({"uid": "original", "replicas": 1}, journal["deployments"]["open-webui"])
            self.assertIn("--resource-version=7", argv)
            raise ValueError("fixture mutation failed")
        with patch.object(service_backup, "run", failed_run), self.assertRaisesRegex(ValueError, "fixture"):
            capture.scale_zero("open-webui", {}, self.root)

    def test_quiescence_orders_writers_dump_database_then_cri(self):
        capture = object.__new__(service_backup.Capture)
        capture.baseline = False
        capture.kube = ["fixture-kubectl"]
        events = []
        capture.get = lambda *args: {"items": [{"metadata": {"name": "pg"}, "status": {"phase": "Running"}}]}
        capture.scale_zero = lambda name, *args: events.append(name)
        capture.stopped = lambda: events.append("all-pods-and-cri-stopped")
        def dump(argv, output, **kwargs):
            events.append(argv[argv.index("--") + 1])
            output.write(b"synthetic database dump")
        with patch.object(service_backup, "run", dump):
            capture.quiesce_apps(self.root, {})
        self.assertEqual(["open-webui", "service-tools", "litellm", "pg_dump", "pg_dumpall",
                          "gateway-postgres", "all-pods-and-cri-stopped"], events)

    def test_failed_database_dump_never_claims_quiescence(self):
        capture = object.__new__(service_backup.Capture)
        capture.baseline = False
        capture.kube = ["fixture-kubectl"]
        events = []
        capture.get = lambda *args: {"items": [{"metadata": {"name": "pg"}, "status": {"phase": "Running"}}]}
        capture.scale_zero = lambda name, *args: events.append(name)
        capture.stopped = lambda: self.fail("capture must not continue after dump failure")
        with patch.object(service_backup, "run", side_effect=ValueError("dump failed")), self.assertRaises(ValueError):
            capture.quiesce_apps(self.root, {})
        self.assertNotIn("gateway-postgres", events)

    def test_baseline_cannot_omit_existing_gateway_database(self):
        capture = object.__new__(service_backup.Capture)
        capture.baseline = True
        capture.get = lambda *args: {"items": [{"metadata": {"name": "gateway-postgres"}}]}
        with self.assertRaisesRegex(ValueError, "pre-service"):
            capture.quiesce_apps(self.root, {})

    def test_model_inventory_preserves_hashes_and_rejects_changed_revision(self):
        model = self.root / "model"
        model.mkdir()
        (model / "model.safetensors").write_bytes(b"synthetic weights")
        path = self.root / "model-inventory.json"
        service_backup.inventory_model("Qwen3.5-9B", model, path)
        record = service_backup.model_inventory(path)
        self.assertEqual(service_backup.digest(model / "model.safetensors"),
                         record["files"]["model.safetensors"]["sha256"])
        record["revision"] = "incorrect-revision"
        path.write_text(json.dumps(record))
        with self.assertRaises(ValueError):
            service_backup.model_inventory(path)


class ResourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.large = service.kustomize(ROOT / "apps/overlays/service")
        cls.split = service.kustomize(ROOT / "apps/overlays/service-split")
        cls.telemetry = service.kustomize(ROOT / "infrastructure/observability")

    def test_observed_128_firmware_reservations_and_over_budget(self):
        resources = {"schema_version": 1, "status": "offline-plan-not-applied", "allocatable_logical_cpus": 42,
                     "gpu_count": 2, "memory": {"total_mib": 130900, "allocatable_mib": 112468, "dimms": None}}
        result = service.capacity(resources, self.large, {"reserve_mib": 6144}, self.telemetry, "large")
        self.assertTrue(result["fits"])
        self.assertEqual(130900, result["observed_total_mib"])
        self.assertIsNone(result["observed_dimms"])
        self.assertFalse(service.capacity(resources, self.large, {"reserve_mib": 6144}, self.telemetry,
                                         "large", vm_mib=100000)["fits"])
        split = service.capacity(resources, self.split, {"reserve_mib": 6144}, self.telemetry, "split")
        self.assertFalse(split["fits"])
        self.assertLess(split["remaining_cpus"], 0)

    def test_private_ingress_has_no_native_admin_route(self):
        ingress = next(o for o in self.large if o["kind"] == "Ingress" and o["metadata"]["name"] == "shared-model-api")
        paths = ingress["spec"]["rules"][0]["http"]["paths"]
        self.assertTrue(all(p["pathType"] == "Exact" for p in paths))
        self.assertFalse(any(p["path"].startswith(("/key", "/user", "/webui", "/_service/control")) for p in paths))

    def test_rendered_policy_union_allows_required_flows_and_denies_lateral_access(self):
        # Evaluate both directions and the union of ALL rendered policies. This
        # catches an inherited broad allow even when the new policy is narrow.
        policies = [o for o in self.large if o["kind"] == "NetworkPolicy"]
        def pod(name, namespace="ai-home-lab"):
            return namespace, {"app.kubernetes.io/name": name}
        def matches(selector, labels):
            self.assertFalse(selector.get("matchExpressions"), "extend evaluator for selector expressions")
            return all(labels.get(k) == v for k, v in selector.get("matchLabels", {}).items())
        def peer_matches(peer, other, namespace):
            if "ipBlock" in peer:
                self.fail("address policy needs explicit pod address evidence")
            ns, labels = other
            ns_match = (matches(peer["namespaceSelector"], {"kubernetes.io/metadata.name": ns})
                        if "namespaceSelector" in peer else ns == namespace)
            return ns_match and matches(peer.get("podSelector", {}), labels)
        def direction_allowed(subject, other, port, direction):
            namespace, labels = subject
            selected = [p for p in policies if p["metadata"]["namespace"] == namespace and
                        direction.capitalize() in p["spec"]["policyTypes"] and
                        matches(p["spec"]["podSelector"], labels)]
            if not selected:
                return True
            peer_field = "from" if direction == "ingress" else "to"
            return any((not rule.get("ports") or any(p.get("protocol", "TCP") == "TCP" and
                       p["port"] == port for p in rule["ports"])) and
                       (peer_field not in rule or any(peer_matches(p, other, namespace) for p in rule[peer_field]))
                       for policy in selected for rule in policy["spec"].get(direction, []))
        def allowed(source, target, port):
            return direction_allowed(source, target, port, "egress") and direction_allowed(target, source, port, "ingress")
        for source, target, port in (("open-webui", "litellm", 4000), ("litellm", "sglang", 30000),
            ("litellm", "gateway-postgres", 5432), ("service-tools", "open-webui", 8080),
            ("open-webui", "service-tools", 8000), ("service-tools", "swarmui", 7801)):
            self.assertTrue(allowed(pod(source), pod(target), port), (source, target))
        for target, port in (("open-webui", 8080), ("litellm", 4000), ("service-tools", 8000)):
            self.assertTrue(allowed(pod("traefik", "kube-system"), pod(target), port))
        for source in (pod("unrelated"), pod("unrelated", "unrelated-namespace")):
            for target, port in (("litellm", 4000), ("gateway-postgres", 5432), ("sglang", 30000),
                                 ("open-webui", 8080), ("service-tools", 8000), ("swarmui", 7801)):
                self.assertFalse(allowed(source, pod(target), port), (source, target))
        self.assertFalse(allowed(pod("open-webui"), pod("sglang"), 30000))
        self.assertFalse(allowed(pod("service-tools"), pod("gateway-postgres"), 5432))

    def test_site_renderer_keeps_secrets_out_and_refuses_public_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            site = {"environment": "tst", "context": "fixture", "node": "fixture", "node_address": "192.168.77.9",
                "management_cidr": "192.168.77.0/24", "client_cidrs": ["192.168.77.0/24"],
                "webui_host": "chat.fixture.test", "api_host": "api.fixture.test", "tls_secret": "fixture-tls"}
            path = root / "site.json"
            path.write_text(json.dumps(site))
            service.render(SimpleNamespace(site=path, output=root / "render", mode="large"))
            objects = service.load_objects((root / "render/workloads.yaml").read_text())
            self.assertTrue(all(o["spec"]["storageClassName"] == "workstation-retain"
                                for o in objects if o["kind"] == "PersistentVolumeClaim"))
            self.assertFalse(any(o["kind"] == "Secret" for o in objects))
            tools = next(o for o in objects if o["kind"] == "Deployment" and o["metadata"]["name"] == "service-tools")
            secret_names = {v["secret"]["secretName"] for v in tools["spec"]["template"]["spec"]["volumes"] if "secret" in v}
            self.assertEqual({"gateway-image-permissions"}, secret_names)
            existing = root / "claims.json"
            existing.write_text(json.dumps({"kind": "List", "items": [{"kind": "PersistentVolumeClaim",
                "metadata": {"name": "webui-rag-qwen3-data", "namespace": "ai-home-lab", "uid": "existing-claim"},
                "spec": {"storageClassName": "local-path"}}]}))
            service.render(SimpleNamespace(site=path, output=root / "existing", mode="large", existing_claims=existing))
            migrated = service.load_objects((root / "existing/workloads.yaml").read_text())
            data = next(o for o in migrated if o["kind"] == "PersistentVolumeClaim" and
                        o["metadata"]["name"] == "webui-rag-qwen3-data")
            self.assertEqual("local-path", data["spec"]["storageClassName"])
            self.assertEqual("existing-claim", json.loads((root / "existing/evidence.json").read_text())[
                "existing_claims"]["webui-rag-qwen3-data"]["uid"])
            site["client_cidrs"] = ["0.0.0.0/0"]
            path.write_text(json.dumps(site))
            with self.assertRaises(ValueError):
                service.site_values(path)


if __name__ == "__main__":
    unittest.main()
