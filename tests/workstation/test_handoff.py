"""Task handoff contracts with a deterministic Open WebUI ACL fixture.

No cluster, model or browser: these check scope, bounds, observed-verification
status, optimistic concurrency, storage and the knowledge allowlist entry.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "apps/base/shared-api"), str(ROOT / "lib/workstation")]
from assistant_metrics import METRICS
import handoff
from service_tools import ServiceTools
import service_backup


HANDOFF = {"goal": "Restore the backup capture path", "completion_criteria": ["capture passes quick_check"],
           "repository": "ArchLinuxThreadripper", "branch": "feat/backup", "revision": "f387f1e",
           "constraints": ["no live restore"], "decisions": ["keep SQLite"], "changed_files": ["lib/workstation/service_backup.py"],
           "verification": {"status": "not_run", "observed": "tests not run yet"},
           "outstanding": ["write the restore note"], "blockers": [], "next_action": "Run the backup tests",
           "evidence": ["docs/MEMORY.md"]}


class HandoffTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        mapping = self.root / "mapping.json"
        mapping.write_text(json.dumps({"service_key": "s" * 32, "users": {}}))
        self.now = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)
        self.users = {"Bearer alice-token": {"id": "alice", "role": "user"},
                      "Bearer sk-alice-key": {"id": "alice", "role": "user"},
                      "Bearer bob-token": {"id": "bob", "role": "user"},
                      "Bearer admin-token": {"id": "root", "role": "admin"}}
        # Knowledge collections readable per user: the only project authority.
        self.readable = {"alice": [{"id": "kb-shared", "name": "Shared"}, {"id": "kb-alice", "name": "Alice"}],
                         "bob": [{"id": "kb-shared", "name": "Shared"}]}
        self.webui_calls = []

        async def upstream(request):
            self.webui_calls.append(request)
            user = self.users.get(request.headers.get("authorization"))
            if request.url.path == "/api/v1/users/user/status":
                return httpx.Response(200 if user else 401, json=user or {})
            if request.url.path == "/api/v1/knowledge/" and user:
                items = self.readable.get(user["id"], [])
                page = int(request.url.params.get("page", "1"))
                return httpx.Response(200, json={"items": items[(page - 1) * 30:page * 30], "total": len(items)})
            raise AssertionError("unexpected upstream " + str(request.url))
        self.upstream = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        self.addAsyncCleanup(self.upstream.aclose)
        self.store = handoff.HandoffStore(self.root / "state", clock=lambda: self.now)
        self.tools = ServiceTools(self.upstream, "http://webui", "http://swarm", "http://gateway", mapping,
                                  self.root / "state", public_webui="https://chat.test", handoffs=self.store)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.tools.app), base_url="http://tools")
        self.addAsyncCleanup(self.client.aclose)

    async def call(self, name, arguments, token="alice-token"):
        response = await self.client.post("/mcp", headers={"Authorization": "Bearer " + token}, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
        if response.status_code != 200:
            return response.status_code, None, None
        result = response.json()["result"]
        return response.status_code, result["isError"], json.loads(result["content"][0]["text"])

    async def save(self, base, document=HANDOFF, project="kb-shared", task="backup-fix", token="alice-token"):
        return await self.call("save_task_handoff", {"project": project, "task": task, "base_revision": base,
                                                     "handoff": document}, token)

    async def test_tools_are_listed_only_when_enabled_with_closed_schemas(self):
        response = await self.client.post("/mcp", headers={"Authorization": "Bearer alice-token"},
                                          json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tools = {t["name"]: t for t in response.json()["result"]["tools"]}
        self.assertTrue({"save_task_handoff", "load_task_handoff", "list_task_handoffs"} <= set(tools))
        for name in handoff.NAMES:
            self.assertFalse(tools[name]["inputSchema"]["additionalProperties"])
        self.assertTrue(tools["load_task_handoff"]["annotations"]["readOnlyHint"])
        self.assertFalse(tools["save_task_handoff"]["annotations"]["destructiveHint"])
        self.tools.handoffs = None
        response = await self.client.post("/mcp", headers={"Authorization": "Bearer alice-token"},
                                          json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertFalse(handoff.NAMES & {t["name"] for t in response.json()["result"]["tools"]})
        self.assertEqual(404, (await self.call("load_task_handoff", {"project": "kb-shared", "task": "x"}))[0])

    async def test_save_load_round_trip_with_revision_and_recheck_notice(self):
        status, error, value = await self.save(0)
        self.assertEqual((200, False), (status, error))
        self.assertEqual((1, 0, "2026-11-10T09:00:00+00:00"), (value["revision"], value["previous_revision"], value["updated_at"]))
        _, error, value = await self.call("load_task_handoff", {"project": "kb-shared", "task": "backup-fix"})
        self.assertFalse(error)
        self.assertEqual({"id": "kb-shared", "name": "Shared"}, value["project"])
        self.assertEqual(1, value["revision"])
        self.assertEqual("not_run", value["handoff"]["verification"]["status"])  # never upgraded to a pass
        self.assertEqual(HANDOFF["changed_files"], value["handoff"]["changed_files"])
        self.assertIn("recheck the Git", value["notice"])
        self.assertEqual("user-recorded-state", value["content_trust"])
        _, error, value = await self.call("load_task_handoff", {"project": "kb-shared", "task": "other-task"})
        self.assertEqual((False, True, None), (error, value["no_handoff"], value["handoff"]))

    async def test_stale_revision_is_a_visible_conflict_and_never_overwrites(self):
        await self.save(0)
        self.now += timedelta(minutes=5)
        _, error, value = await self.save(1, {**HANDOFF, "next_action": "second writer"})
        self.assertEqual((False, 2), (error, value["revision"]))
        for stale in (1, 0, 7):
            _, error, value = await self.save(stale, {**HANDOFF, "next_action": "stale writer"})
            self.assertTrue(error)
            self.assertEqual(("handoff_conflict", 2, "2026-11-10T09:05:00+00:00"),
                             (value["error"], value["current_revision"], value["updated_at"]))
        _, _, value = await self.call("load_task_handoff", {"project": "kb-shared", "task": "backup-fix"})
        self.assertEqual(("second writer", 2), (value["handoff"]["next_action"], value["revision"]))
        _, error, value = await self.save(1, task="new-task")  # a missing task is revision 0
        self.assertEqual(("handoff_conflict", 0, None), (value["error"], value["current_revision"], value["updated_at"]))
        self.assertRegex(METRICS.render(), r'(?m)^workstation_assistant_handoff_operations_total\{[^}]*outcome="conflict"\} [1-9]')

    async def test_verification_is_observed_status_never_inferred(self):
        no_verification = {k: v for k, v in HANDOFF.items() if k != "verification"}
        _, error, _ = await self.save(0, no_verification, task="unknown-result")
        self.assertFalse(error)
        _, _, value = await self.call("load_task_handoff", {"project": "kb-shared", "task": "unknown-result"})
        self.assertEqual({"status": "unknown"}, value["handoff"]["verification"])
        for verification, code in (({"status": "passed"}, "verification_observed_required"),
                                   ({"status": "partial"}, "verification_observed_required"),
                                   ({"status": "pass", "observed": "ok"}, "invalid_verification"),
                                   ({"status": "passed", "observed": "ok", "observed_at": "2026-11-10T09:00:00"}, "invalid_verification"),
                                   ({"status": "passed", "observed": "ok", "inferred": True}, "invalid_verification"),
                                   ("passed", "invalid_verification")):
            _, error, value = await self.save(0, {**HANDOFF, "verification": verification}, task="claims")
            self.assertEqual((True, code), (error, value["error"]), verification)
        passed = {"status": "passed", "observed": "pytest: 42 passed", "observed_at": "2026-11-10T08:55:00+00:00",
                  "evidence": "ci run 17"}
        _, error, _ = await self.save(0, {**HANDOFF, "verification": passed}, task="claims")
        self.assertFalse(error)

    async def test_character_limits_per_field_list_and_total(self):
        for document, code in (({**HANDOFF, "goal": "x" * 601}, "invalid_goal"),
                               ({**HANDOFF, "goal": "  "}, "invalid_goal"),
                               ({**HANDOFF, "goal": "bell\x07"}, "invalid_goal"),
                               ({**HANDOFF, "changed_files": ["f"] * 21}, "invalid_changed_files"),
                               ({**HANDOFF, "constraints": ["x" * 241]}, "invalid_constraints"),
                               ({**HANDOFF, "constraints": "one string"}, "invalid_constraints"),
                               ({**HANDOFF, "transcript": "whole chat"}, "invalid_handoff"),
                               ({k: v for k, v in HANDOFF.items() if k != "goal"}, "invalid_handoff"),
                               ({**HANDOFF, "goal": "g" * 600, "next_action": "n" * 400,
                                 "changed_files": ["f" * 200] * 20}, "handoff_too_large")):
            _, error, value = await self.save(0, document)
            self.assertEqual((True, code), (error, value["error"]))
        for task in ("Backup", "-x", "a" * 65, "a/b", "", 3):
            _, error, value = await self.save(0, task=task)
            self.assertEqual((True, "invalid_task"), (error, value["error"]), task)
        _, error, value = await self.call("save_task_handoff", {"project": "kb-shared", "task": "t", "handoff": HANDOFF})
        self.assertEqual((True, "invalid_tool_arguments"), (error, value["error"]))
        _, error, value = await self.save(-1)
        self.assertEqual((True, "invalid_base_revision"), (error, value["error"]))
        self.assertFalse(self.store.database.exists())  # nothing stored by refused saves

    async def test_scope_is_user_and_currently_authorised_project(self):
        await self.save(0, project="kb-alice")
        await self.save(0, project="kb-shared")
        # Another user cannot read the project or Alice's handoff in a shared project.
        _, error, value = await self.call("load_task_handoff", {"project": "kb-alice", "task": "backup-fix"}, "bob-token")
        self.assertEqual((True, "project_not_authorized"), (error, value["error"]))
        _, error, value = await self.save(0, project="kb-alice", token="bob-token")
        self.assertEqual((True, "project_not_authorized"), (error, value["error"]))
        _, error, value = await self.call("load_task_handoff", {"project": "kb-shared", "task": "backup-fix"}, "bob-token")
        self.assertEqual((False, True), (error, value["no_handoff"]))
        _, _, value = await self.call("list_task_handoffs", {}, "bob-token")
        self.assertEqual(([], [{"id": "kb-shared", "name": "Shared"}]), (value["handoffs"], value["projects"]))
        # Alice lists both; after her access to kb-alice is revoked it disappears.
        _, _, value = await self.call("list_task_handoffs", {})
        self.assertEqual({"kb-alice", "kb-shared"}, {h["project"] for h in value["handoffs"]})
        self.assertEqual("not_run", value["handoffs"][0]["verification_status"])
        self.readable["alice"] = [{"id": "kb-shared", "name": "Shared"}]
        _, _, value = await self.call("list_task_handoffs", {})
        self.assertEqual({"kb-shared"}, {h["project"] for h in value["handoffs"]})
        _, error, value = await self.call("list_task_handoffs", {"project": "kb-alice"})
        self.assertEqual((True, "project_not_authorized"), (error, value["error"]))
        _, _, value = await self.call("list_task_handoffs", {"project": "kb-shared"})
        self.assertNotIn("projects", value)
        # Administrator credentials are refused, as for every other tool.
        self.assertEqual(403, (await self.call("list_task_handoffs", {}, "admin-token"))[0])
        # The API-key path reaches the same rows (owner derives from the user id).
        _, _, value = await self.call("load_task_handoff", {"project": "kb-shared", "task": "backup-fix"}, "sk-alice-key")
        self.assertEqual(1, value["revision"])
        self.assertRegex(METRICS.render(), r'(?m)^workstation_assistant_handoff_operations_total\{client="mcp",op="load",outcome="ok"\} [1-9]')

    async def test_project_listing_pages_and_identity_never_selects_scope(self):
        self.readable["alice"] = [{"id": "kb-%02d" % n, "name": "Project %d" % n} for n in range(45)]
        _, error, value = await self.save(0, project="kb-44")
        self.assertFalse(error)
        pages = [r.url.params.get("page") for r in self.webui_calls if r.url.path == "/api/v1/knowledge/"]
        self.assertEqual(["1", "2"], pages)
        self.assertTrue(all(r.headers["authorization"] == "Bearer alice-token" for r in self.webui_calls))
        with sqlite3.connect(self.store.database) as db:
            owners = {row[0] for row in db.execute("SELECT owner FROM handoffs")}
        self.assertEqual({"h_" + hashlib.sha256(b"workstation-handoff:alice").hexdigest()[:32]}, owners)
        self.assertNotIn(b"alice", self.store.database.read_bytes())

    async def test_per_user_row_limit_and_unsafe_store(self):
        limit = handoff.MAX_HANDOFFS_PER_USER
        handoff.MAX_HANDOFFS_PER_USER = 2
        self.addCleanup(setattr, handoff, "MAX_HANDOFFS_PER_USER", limit)
        for task in ("a", "b"):
            self.assertFalse((await self.save(0, task=task))[1])
        _, error, value = await self.save(0, task="c")
        self.assertEqual((True, "handoff_limit_reached"), (error, value["error"]))
        self.assertFalse((await self.save(1, task="a"))[1])  # updating an existing task is allowed
        self.store.database.unlink()
        self.store.database.symlink_to(self.root / "elsewhere.sqlite3")
        _, error, value = await self.call("load_task_handoff", {"project": "kb-shared", "task": "a"})
        self.assertEqual((True, "handoff_store_unavailable"), (error, value["error"]))


class DeploymentTests(unittest.TestCase):
    def test_backup_stops_the_writer_before_copying_the_state_claim(self):
        # service-tools is scaled to zero (Pod exit awaited) before the
        # service-tools-state archive, so the closed SQLite file is consistent.
        self.assertIn("service-tools", service_backup.WRITERS)
        self.assertIn("service-tools-state", service_backup.CLAIMS)
        claims = [d for d in yaml.safe_load_all((ROOT / "apps/base/shared-api/storage.yaml").read_text())
                  if d["kind"] == "PersistentVolumeClaim" and d["metadata"]["name"] == "service-tools-state"]
        self.assertEqual("irreplaceable", claims[0]["metadata"]["labels"]["workstation.ai/backup"])
        self.assertEqual(Path("/state/task-handoffs.sqlite3"), handoff.HandoffStore("/state").database)

    def test_api_key_allowlist_admits_only_the_knowledge_listing(self):
        webui = yaml.safe_load((ROOT / "apps/overlays/service/webui.yaml").read_text())
        env = {e["name"]: e.get("value") for e in webui["spec"]["template"]["spec"]["containers"][0]["env"]}
        allowed = env["API_KEYS_ALLOWED_ENDPOINTS"].split(",")

        def admitted(path):  # pinned Open WebUI 0.12.5 utils/auth.py rule
            return any(path == entry or path.startswith(entry + "/") for entry in allowed)
        self.assertTrue(admitted("/api/v1/knowledge/"))
        for path in ("/api/v1/knowledge/create", "/api/v1/knowledge/kb-1", "/api/v1/knowledge/kb-1/update",
                     "/api/v1/knowledge/kb-1/file/remove", "/api/v1/knowledge/kb-1/reset", "/api/v1/knowledge/search"):
            self.assertFalse(admitted(path), path)


if __name__ == "__main__":
    unittest.main()
