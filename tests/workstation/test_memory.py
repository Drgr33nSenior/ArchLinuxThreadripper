"""Graphiti memory integration tests with deterministic fakes.

These cover identity binding, tool exposure, the service's group scoping and
the rendered deployment. They make no model, embedding or FalkorDB calls and
are not semantic memory qualification.
"""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "apps/base/shared-api"), str(ROOT / "infrastructure/graphiti"), str(ROOT / "lib/workstation")]
from service_tools import ServiceTools
import memory_service
import service_backup


class ToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "state").mkdir()
        mapping = self.root / "mapping.json"
        mapping.write_text(json.dumps({"service_key": "s" * 32, "users": {}}))
        key = self.root / "service-key"
        key.write_text("k" * 40)
        self.memory_calls = []
        users = {"Bearer alice-token": {"id": "alice", "role": "user"},
                 "Bearer bob-token": {"id": "bob", "role": "user"},
                 "Bearer admin-token": {"id": "root", "role": "admin"}}

        async def upstream(request):
            if request.url.path == "/api/v1/auths/":
                user = users.get(request.headers.get("authorization"))
                return httpx.Response(200 if user else 401, json=user or {})
            if request.url.host == "memory":
                self.memory_calls.append((request.url.path, json.loads(request.content), dict(request.headers)))
                return httpx.Response(200, json={"facts": [], "no_memory": True})
            raise AssertionError("unexpected upstream " + str(request.url))
        self.upstream = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        self.key = key
        self.mapping = mapping

    async def asyncTearDown(self):
        await self.upstream.aclose()
        self.directory.cleanup()

    def tools(self, memory=None, write=False):
        service = ServiceTools(self.upstream, "http://webui", "http://swarm", "http://gateway", self.mapping,
                               self.root / "state", public_webui="https://chat.test", memory=memory,
                               memory_key_file=self.key, memory_write=write)
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=service.app), base_url="http://tools")

    async def call(self, client, token, method, params=None):
        return await client.post("/mcp", headers={"Authorization": "Bearer " + token},
                                 json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})

    async def names(self, client):
        response = await self.call(client, "alice-token", "tools/list")
        return [tool["name"] for tool in response.json()["result"]["tools"]]

    async def test_memory_is_off_by_default_and_read_write_are_separate(self):
        async with self.tools() as client:
            self.assertNotIn("recall_memory", await self.names(client))
            response = await self.call(client, "alice-token", "tools/call", {"name": "recall_memory", "arguments": {"query": "x"}})
            self.assertEqual(404, response.status_code)
        async with self.tools(memory="http://memory:8000") as client:
            self.assertEqual(["retrieve_project_passage", "read_project_source", "recall_memory"], await self.names(client))
            response = await self.call(client, "alice-token", "tools/call", {"name": "remember", "arguments": {"text": "x"}})
            self.assertEqual(404, response.status_code)
        async with self.tools(memory="http://memory:8000", write=True) as client:
            self.assertEqual({"recall_memory", "remember", "forget_memory"} - set(await self.names(client)), set())
        self.assertEqual([], self.memory_calls)

    async def test_group_comes_only_from_authenticated_identity(self):
        async with self.tools(memory="http://memory:8000", write=True) as client:
            for token in ("alice-token", "bob-token"):
                response = await self.call(client, token, "tools/call", {"name": "recall_memory", "arguments": {"query": "pets"}})
                self.assertEqual(200, response.status_code)
            alice, bob = (call[1]["group"] for call in self.memory_calls)
            self.assertNotEqual(alice, bob)
            self.assertRegex(alice, r"^u_[a-f0-9]{32}$")
            self.assertEqual(alice, ServiceTools.memory_group("alice"))
            # A caller cannot name a group, and admin tokens are refused.
            response = await self.call(client, "alice-token", "tools/call",
                                       {"name": "recall_memory", "arguments": {"query": "x", "group": bob}})
            self.assertEqual(400, response.status_code)
            response = await self.call(client, "admin-token", "tools/call", {"name": "recall_memory", "arguments": {"query": "x"}})
            self.assertEqual(403, response.status_code)
            await self.call(client, "alice-token", "tools/call", {"name": "remember", "arguments": {"text": "Alice's cat is Tom"}})
        path, body, headers = self.memory_calls[-1]
        self.assertEqual(("/remember", "explicit user request"), (path, body["source"]))
        self.assertEqual("Bearer " + "k" * 40, headers["authorization"])  # service key, never the user token
        self.assertEqual(2 + 1, len(self.memory_calls))


class FakeEdge:
    def __init__(self, uuid, fact, group, invalid=None):
        self.uuid, self.fact, self.group_id = uuid, fact, group
        self.valid_at, self.invalid_at, self.expired_at = datetime(2026, 11, 1, tzinfo=timezone.utc), invalid, None
        self.episodes = ["e-" + uuid]


class FakeGraphiti:
    """Records calls per group; stands in for a group-bound Graphiti."""
    episodes = {}
    built = []

    def __init__(self, group):
        self.group = group
        self.driver = types.SimpleNamespace(group=group)

    async def build_indices_and_constraints(self):
        FakeGraphiti.built.append(self.group)

    async def search(self, query, group_ids, num_results):
        return [FakeEdge("old", "Cat is Tom", self.group, invalid=datetime(2026, 11, 2, tzinfo=timezone.utc)),
                FakeEdge("new", "Cat is Max", self.group), FakeEdge("leak", "Other user fact", "u_" + "0" * 32)]

    async def add_episode(self, **kwargs):
        await asyncio.sleep(0.05)
        uuid = "11111111-1111-4111-8111-%012d" % len(FakeGraphiti.episodes)
        FakeGraphiti.episodes[uuid] = kwargs["group_id"]
        return types.SimpleNamespace(episode=types.SimpleNamespace(uuid=uuid), edges=[1])

    async def remove_episode(self, uuid):
        FakeGraphiti.episodes.pop(uuid)


class NotFound(Exception):
    pass


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FakeGraphiti.episodes, FakeGraphiti.built = {}, []

        async def get_by_uuid(driver, uuid):
            if uuid not in FakeGraphiti.episodes:
                raise NotFound()
            return types.SimpleNamespace(group_id=FakeGraphiti.episodes[uuid])
        modules = {"graphiti_core": types.ModuleType("graphiti_core"),
                   "graphiti_core.nodes": types.SimpleNamespace(EpisodeType=types.SimpleNamespace(text="text"),
                                                                EpisodicNode=types.SimpleNamespace(get_by_uuid=get_by_uuid)),
                   "graphiti_core.errors": types.SimpleNamespace(NodeNotFoundError=NotFound)}
        self.saved = {name: sys.modules.get(name) for name in modules}
        sys.modules.update(modules)
        self.memory = memory_service.Memory(FakeGraphiti)
        self.alice, self.bob = "u_" + "a" * 32, "u_" + "b" * 32

    def tearDown(self):
        for name, module in self.saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    async def test_recall_refuses_facts_without_the_callers_group(self):
        class Unscoped(FakeGraphiti):
            async def search(self, query, group_ids, num_results):
                edge = FakeEdge("x", "No group recorded", self.group)
                del edge.group_id
                return [edge]
        memory = memory_service.Memory(Unscoped)
        self.assertEqual([], (await memory.recall(self.alice, "cat"))["facts"])

    async def test_recall_keeps_group_scope_and_marks_history(self):
        result = await self.memory.recall(self.alice, "cat")
        self.assertEqual(["new", "old"], [fact["uuid"] for fact in result["facts"]])
        self.assertEqual([True, False], [fact["current"] for fact in result["facts"]])
        self.assertIn("overrides", result["notice"])
        await self.memory.recall(self.alice, "again")
        self.assertEqual([self.alice], FakeGraphiti.built)  # indices built once per group graph
        for group in ("alice", "u_" + "A" * 32, None):
            with self.assertRaises(memory_service.Refused):
                await self.memory.recall(group, "cat")

    async def test_writes_are_serialized_and_deletion_is_group_scoped(self):
        first = asyncio.create_task(self.memory.remember(self.alice, "Cat is Max", "explicit user request"))
        await asyncio.sleep(0.01)
        with self.assertRaises(memory_service.Refused) as busy:
            await self.memory.remember(self.alice, "Second", "explicit user request")
        self.assertEqual(429, busy.exception.status)
        episode = (await first)["episode_uuid"]
        with self.assertRaises(memory_service.Refused) as other:
            await self.memory.forget(self.bob, episode)
        self.assertEqual(404, other.exception.status)
        self.assertIn(episode, FakeGraphiti.episodes)
        self.assertEqual({"deleted_episode": episode}, await self.memory.forget(self.alice, episode))
        with self.assertRaises(memory_service.Refused):
            await self.memory.remember(self.alice, "x", "s", reference_time="2026-11-07T10:00:00")

    def test_http_service_requires_key_and_exact_fields(self):
        server = memory_service.serve(self.memory, "k" * 40, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"

        def post(path, body, key):
            request = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                             headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as error:
                return error.code, json.loads(error.read())
        self.assertEqual(401, post("/recall", {"group": self.alice, "query": "cat"}, "wrong" * 8)[0])
        self.assertEqual(400, post("/recall", {"group": self.alice, "query": "cat", "extra": 1}, "k" * 40)[0])
        status, value = post("/recall", {"group": self.alice, "query": "cat"}, "k" * 40)
        self.assertEqual(200, status)
        self.assertEqual(2, len(value["facts"]))


class DeploymentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        def render(overlay):
            out = subprocess.run(["kubectl", "kustomize", str(ROOT / "apps/overlays" / overlay)],
                                 capture_output=True, check=True, text=True).stdout
            return [o for o in yaml.safe_load_all(out) if o]
        cls.memory = render("assistant-memory")
        cls.plain = render("assistant")

    def find(self, objects, kind, name):
        return next(o for o in objects if o["kind"] == kind and o["metadata"]["name"] == name)

    def test_memory_is_opt_in_inactive_and_pinned(self):
        tools = self.find(self.plain, "Deployment", "service-tools")
        self.assertNotIn("MEMORY_ENABLED", json.dumps(tools))
        for name in ("neo4j", "memory-embedding", "graphiti-memory"):
            deployment = self.find(self.memory, "Deployment", name)
            self.assertEqual(0, deployment["spec"]["replicas"])
            container = deployment["spec"]["template"]["spec"]["containers"][0]
            self.assertTrue("@sha256:" in container["image"] or container["image"].endswith(":UNBUILT"), container["image"])
            self.assertTrue(deployment["spec"]["template"]["spec"]["securityContext"]["runAsNonRoot"])
            self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
        claim = self.find(self.memory, "PersistentVolumeClaim", "graphiti-neo4j")
        self.assertEqual("workstation-retain", claim["spec"]["storageClassName"])
        self.assertEqual("irreplaceable", claim["metadata"]["labels"]["workstation.ai/backup"])
        for obj in self.memory:
            if obj["kind"] in ("Ingress", "Service") and obj["metadata"]["name"] in ("neo4j", "memory-embedding", "graphiti-memory"):
                self.assertNotIn(obj["spec"].get("type"), ("NodePort", "LoadBalancer"))
        embedding = self.find(self.memory, "Deployment", "memory-embedding")["spec"]["template"]["spec"]
        self.assertTrue(embedding["volumes"][0]["persistentVolumeClaim"]["readOnly"])
        neo4j = self.find(self.memory, "Deployment", "neo4j")["spec"]["template"]["spec"]
        environment = {e["name"]: e["value"] for e in neo4j["containers"][0]["env"]}
        self.assertEqual("false", environment["NEO4J_server_http_enabled"])
        self.assertEqual("/run/neo4j/neo4j-auth", environment["NEO4J_AUTH_FILE"])
        self.assertNotIn("NEO4J_AUTH", environment)  # never an inline password
        self.assertIn("-community@sha256:", neo4j["containers"][0]["image"])

    def test_network_policy_allows_only_the_memory_chain(self):
        policy = self.find(self.memory, "NetworkPolicy", "graphiti-memory-flows")["spec"]
        sources = [rule["from"][0]["podSelector"]["matchLabels"]["app.kubernetes.io/name"] for rule in policy["ingress"]]
        targets = sorted(rule["to"][0]["podSelector"]["matchLabels"]["app.kubernetes.io/name"] for rule in policy["egress"])
        self.assertEqual(["service-tools"], sources)
        self.assertEqual(["litellm", "memory-embedding", "neo4j"], targets)
        for name in ("neo4j-flows", "memory-embedding-flows"):
            spec = self.find(self.memory, "NetworkPolicy", name)["spec"]
            self.assertEqual([], spec["egress"])
            self.assertEqual("graphiti-memory", spec["ingress"][0]["from"][0]["podSelector"]["matchLabels"]["app.kubernetes.io/name"])

    def test_lock_is_hashed_and_excludes_embedded_server(self):
        lines = [line for line in (ROOT / "infrastructure/graphiti/requirements-python314.lock").read_text().splitlines()
                 if line and not line.startswith("#")]
        self.assertTrue(lines)
        self.assertTrue(all("--hash=sha256:" in line and "==" in line for line in lines))
        names = {line.split("==")[0].lower() for line in lines}
        self.assertIn("graphiti-core", names)
        self.assertIn("neo4j", names)
        self.assertFalse(names & {"falkordb", "falkordblite", "redis", "psutil", "setuptools", "hiredis"})
        containerfile = (ROOT / "infrastructure/graphiti/Containerfile").read_text()
        self.assertIn("--require-hashes", containerfile)
        self.assertRegex(containerfile, r"FROM \S+@sha256:[a-f0-9]{64}")

    def test_backup_captures_memory_claim_only_when_present(self):
        self.assertIn("graphiti-neo4j", service_backup.OPTIONAL_CLAIMS)
        self.assertNotIn("graphiti-neo4j", service_backup.CLAIMS)
        self.assertEqual(("graphiti-memory", "neo4j"), service_backup.OPTIONAL_CLAIMS["graphiti-neo4j"])

    def test_graph_credential_file_is_strict(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "neo4j-auth"
            path.write_text("neo4j/" + "p" * 24 + "\n")
            self.assertEqual(("neo4j", "p" * 24), memory_service.graph_credentials(path))
            for bad in ("admin/" + "p" * 24, "neo4j/short", "p" * 30):
                path.write_text(bad)
                with self.assertRaises(ValueError):
                    memory_service.graph_credentials(path)


if __name__ == "__main__":
    unittest.main()
