"""Family key onboarding: decisions against a WebUI database fixture, a fake gateway admin API and a fake
Kubernetes API that grants only what the rendered Role grants; and the rendered CronJob, identity and flows."""
import base64
import http.server
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "apps/base/shared-api"), str(ROOT / "lib/workstation")]
import boundary
import onboarding
import service

spec = importlib.util.spec_from_file_location("key_onboarding", ROOT / "infrastructure/service/key_onboarding.py")
job = importlib.util.module_from_spec(spec)
spec.loader.exec_module(job)

NOW = 1_800_000_000
DAY = 86400
MASTER = "sk-master-" + "m" * 40
SERVICE_KEY = "service-" + "s" * 40
TOKEN = "account-" + "t" * 40
PARENTS, FAMILY = "parents-group", "family-group"
MODELS = sorted(service.MODELS)
SANDBOX = {"image": "registry.example.test/sandbox@sha256:" + "a" * 64,
           "egress_image": "registry.example.test/sandbox-egress@sha256:" + "b" * 64,
           "parents_group_id": PARENTS, "family_group_id": FAMILY, "family_enabled": True,
           "group_resolution": "most-permissive", "egress_service_ips": {"family": "10.43.0.201", "parents": "10.43.0.202"},
           "api_server_cidr": "192.168.77.9/32", "api_server_port": 6443}
SITE = {"context": "fixture", "node": "fixture", "node_address": "192.168.77.9",
        "management_cidr": "192.168.77.0/24", "client_cidrs": ["192.168.77.0/24"],
        "webui_host": "chat.fixture.test", "api_host": "api.fixture.test", "tls_secret": "fixture-tls"}
PROFILE = ROOT / "infrastructure/ai-stack/serving/qwen3.8-27b-mxfp4-tp2.json"
MOTHER, CHILD, ADMIN = "11111111-aaaa-4aaa-8aaa-000000000001", "22222222-bbbb-4bbb-8bbb-000000000002", \
    "33333333-cccc-4ccc-8ccc-000000000003"


def encoded(document):
    return base64.b64encode(json.dumps(document).encode()).decode()


class Fakes(http.server.BaseHTTPRequestHandler):
    """One server: the LiteLLM admin routes the job uses, and the two Secrets and one ConfigMap of the Role."""
    state = None

    def log_message(self, *args):
        pass

    def answer(self, status, value=None):
        body = json.dumps(value if value is not None else {"error": "refused"}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def kubernetes(self, body):
        state = self.state
        kind, name = self.path.split("/")[-2:]
        state.kube_calls.append((self.command, kind, name))
        verbs = state.verbs.get((kind, name), ())
        if self.headers["Authorization"] != "Bearer " + TOKEN or self.command not in verbs \
                or not self.path.startswith("/api/v1/namespaces/ai-home-lab/"):
            return self.answer(403)
        current = state.objects[(kind, name)]
        if self.command == "PUT":
            state.conflict_at -= 1
            if state.conflict_at == 0 or body["metadata"]["resourceVersion"] != current["metadata"]["resourceVersion"]:
                return self.answer(409)
            body["metadata"]["resourceVersion"] = str(int(current["metadata"]["resourceVersion"]) + 1)
            state.objects[(kind, name)] = current = body
        self.answer(200, current)

    def gateway(self, body):
        state = self.state
        state.gateway_calls.append((self.path, {name: value for name, value in body.items() if name != "keys"}))
        if self.headers["Authorization"] != "Bearer " + MASTER:
            return self.answer(401)
        if self.path == "/user/new":
            if body["user_id"] in state.users:
                return self.answer(409)
            state.users.add(body["user_id"])
            return self.answer(200, {"user_id": body["user_id"]})
        if self.path == "/key/generate":
            if body["user_id"] in state.refuse_generate or any(k["key_alias"] == body["key_alias"] for k in state.keys.values()):
                return self.answer(400)
            state.serial += 1
            key = f"sk-fixture-{state.serial:04d}-" + "k" * 24
            expires = state.now + int(body["duration"].removesuffix("d")) * DAY
            state.keys[key] = dict(body, expires=expires)
            return self.answer(200, {"key": key, "user_id": body["user_id"], "key_alias": body["key_alias"],
                                     "expires": job.datetime.datetime.fromtimestamp(
                                         expires, job.datetime.timezone.utc).isoformat().replace("+00:00", "Z")})
        if self.path == "/key/delete":
            found = [key for key, record in state.keys.items()
                     if key in body.get("keys", []) or record["key_alias"] in body.get("key_aliases", [])]
            if not found or state.refuse_delete:
                return self.answer(500 if state.refuse_delete else 404)
            for key in found:
                del state.keys[key]
            return self.answer(200, {"deleted_keys": len(found)})
        self.answer(404)

    def do_GET(self):
        self.kubernetes(None)

    def do_PUT(self):
        self.kubernetes(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))

    def do_POST(self):
        self.gateway(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))


class Fixture(unittest.TestCase):
    mode = "live"

    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fakes)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "data").mkdir()
        self.database = self.root / "data/webui.db"
        # Open WebUI's layout and journal mode, with its connection left open as the running pod has.
        self.webui = sqlite3.connect(self.database)
        self.addCleanup(self.webui.close)
        self.webui.execute("PRAGMA journal_mode=WAL")
        self.webui.executescript('''
            CREATE TABLE "user" (id TEXT PRIMARY KEY, email TEXT, role TEXT, name TEXT);
            CREATE TABLE "group" (id TEXT PRIMARY KEY, name TEXT, parent_group_id TEXT);
            CREATE TABLE group_member (id TEXT PRIMARY KEY, group_id TEXT NOT NULL, user_id TEXT NOT NULL);''')
        self.webui.executemany('INSERT INTO "group" (id, name) VALUES (?, ?)', [(PARENTS, "Parents"), (FAMILY, "Family")])
        self.webui.commit()
        (self.root / "master-key").write_text(MASTER + "\n")
        self.state = Fakes.state = SimpleNamespace(
            now=NOW, users=set(), keys={}, serial=0, gateway_calls=[], kube_calls=[], refuse_generate=set(),
            refuse_delete=False, conflict_at=-1, objects={}, verbs={})
        self.publish({"service_key": SERVICE_KEY, "users": {}})
        self.state.objects[("configmaps", job.STATUS_CONFIGMAP)] = {"metadata": {"resourceVersion": "1"}, "data": {}}
        self.configure(self.mode)
        self.outputs = []

    def configure(self, mode, homes=()):
        sandbox = {**SANDBOX, "users": [{"webui_user_id": user} for user in homes]}
        config = {**onboarding.job_config(mode, sandbox, MODELS), "gateway": self.url, "database": str(self.database)}
        (self.root / "config.json").write_text(json.dumps(config))
        # Exactly the rendered Role, so a run that needs more is refused here as it would be in the cluster.
        role = next(o for o in self.bundle(mode) if o["kind"] == "Role")
        verbs = {"get": "GET", "update": "PUT"}
        self.state.verbs = {(rule["resources"][0], name): {verbs[v] for v in rule["verbs"]}
                            for rule in role["rules"] for name in rule["resourceNames"]}

    @staticmethod
    def bundle(mode):
        webui = {"kind": "Deployment", "metadata": {"name": "open-webui"}, "spec": {"template": {"spec": {
            "nodeSelector": {"kubernetes.io/os": "linux"},
            "containers": [{"image": "registry.example.test/open-webui@sha256:" + "c" * 64}],
            "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "webui-fixture-data"}}]}}}}
        claim = {"kind": "PersistentVolumeClaim", "metadata": {"name": "webui-fixture-data"},
                 "spec": {"accessModes": ["ReadWriteOnce"]}}
        return onboarding.render(mode, {**SANDBOX, "users": []}, [webui, claim], MODELS, ROOT)

    def publish(self, mapping, images=None):
        """The owner's two Secrets, as `service credentials` and `browser-bind` leave them."""
        version = str(int(self.state.objects.get(("secrets", job.MAPPING_SECRET), {"metadata": {"resourceVersion": "0"}})
                          ["metadata"]["resourceVersion"]) + 1)
        derived = job.image_document(mapping) if isinstance(mapping, dict) and {"service_key", "users"} <= set(mapping) \
            and isinstance(mapping["users"], dict) else {}
        for name, key, document in ((job.MAPPING_SECRET, job.MAPPING_KEY, mapping),
                                    (job.IMAGE_SECRET, job.IMAGE_KEY, derived if images is None else images)):
            self.state.objects[("secrets", name)] = {"kind": "Secret", "metadata": {"name": name, "resourceVersion": version},
                                                     "data": {key: encoded(document)}}

    def person(self, user, *groups, role="user"):
        self.webui.execute('INSERT OR REPLACE INTO "user" (id, email, role, name) VALUES (?, ?, ?, ?)',
                           (user, user + "@fixture.test", role, "Fixture"))
        self.webui.execute("DELETE FROM group_member WHERE user_id = ?", (user,))
        for group in groups:
            self.webui.execute("INSERT INTO group_member VALUES (?, ?, ?)", (group + user, group, user))
        self.webui.commit()

    def mapping(self):
        return json.loads(base64.b64decode(self.state.objects[("secrets", job.MAPPING_SECRET)]["data"][job.MAPPING_KEY]))

    def images(self):
        return json.loads(base64.b64decode(self.state.objects[("secrets", job.IMAGE_SECRET)]["data"][job.IMAGE_KEY]))

    def manual(self, days=60, images=False, **extra):
        """A key the owner issued by hand, known to the gateway."""
        self.state.serial += 1
        key = f"sk-manual-{self.state.serial:04d}-" + "h" * 24
        self.state.keys[key] = {"key_alias": f"browser-{self.state.serial}", "user_id": "manual"}
        return {"key": key, "expires_at": self.state.now + days * DAY, "images": images, **extra}

    def run_job(self, expect=0):
        output = io.StringIO()
        with mock.patch.object(job.time, "sleep"):
            code = job.main(self.root / "config.json", self.root / "master-key",
                            kube=job.Kube("ai-home-lab", base=self.url, token=TOKEN), now=self.state.now, output=output)
        self.outputs.append(output.getvalue())
        self.assertEqual(expect, code, output.getvalue())
        return json.loads(output.getvalue())

    def puts(self):
        return [call for call in self.state.kube_calls if call[0] == "PUT"]

    def tearDown(self):
        # Whatever happened: nothing a person could use leaves through stdout, the status or a recorded request.
        published = json.dumps(self.state.objects[("configmaps", job.STATUS_CONFIGMAP)])
        secrets = {MASTER, SERVICE_KEY, TOKEN, *self.state.keys}
        for text in (*self.outputs, published, json.dumps(self.state.gateway_calls)):
            for secret in (*secrets, "sk-"):
                self.assertNotIn(secret, text)
        # A person is named by an eight-character prefix only.
        for text in (*self.outputs, published):
            for user in (MOTHER, CHILD, ADMIN):
                self.assertNotIn(user, text)


class ReconcileTests(Fixture):
    def test_new_members_get_a_key_for_their_exact_id_with_their_groups_policy(self):
        self.person(MOTHER, PARENTS, FAMILY)   # both groups: the most permissive wins
        self.person(CHILD, FAMILY)
        status = self.run_job()
        users = self.mapping()["users"]
        self.assertEqual({MOTHER, CHILD}, set(users))
        self.assertEqual({MOTHER, CHILD}, self.state.users)
        for user, tier in ((MOTHER, "parents"), (CHILD, "family")):
            entry, policy = users[user], onboarding.POLICIES[tier]
            issued = self.state.keys[entry["key"]]
            self.assertEqual((user, f"webui-{user}-a", MODELS, "90d", policy["rpm"], policy["tpm"], policy["concurrency"]),
                             (issued["user_id"], issued["key_alias"], issued["models"], issued["duration"], issued["rpm_limit"],
                              issued["tpm_limit"], issued["max_parallel_requests"]))
            self.assertEqual((NOW + 90 * DAY, policy["images"], tier), (entry["expires_at"], entry["images"], entry["policy"]))
        self.assertEqual({"service_key": SERVICE_KEY, "users": {
            MOTHER: {"expires_at": NOW + 90 * DAY, "images": True},
            CHILD: {"expires_at": NOW + 90 * DAY, "images": False}}}, self.images())
        self.assertEqual(("ok", 2, 2, 0), (status["result"], status["counts"]["members"], status["counts"]["provision"],
                                           status["counts"]["failed"]))
        self.assertEqual([{"user": "11111111", "action": "provision", "tier": "parents", "state": "done"},
                          {"user": "22222222", "action": "provision", "tier": "family", "state": "done"}], status["changes"])
        self.assertEqual(90, status["earliest_expiry_days"])
        self.assertTrue(status["image_permissions_in_step"])
        published = json.loads(self.state.objects[("configmaps", job.STATUS_CONFIGMAP)]["data"][job.STATUS_KEY])
        self.assertEqual(status, published)

    def test_the_gateway_accepts_what_the_job_published(self):
        self.person(MOTHER, PARENTS)
        self.person(CHILD, FAMILY)
        self.run_job()
        credentials = self.mapping()
        for user, images in ((MOTHER, True), (CHILD, False)):
            headers = {b"authorization": ("Bearer " + SERVICE_KEY).encode(), b"x-openwebui-user-id": user.encode()}
            self.assertEqual((credentials["users"][user]["key"], user),
                             boundary.browser_key(headers, "/webui/v1/chat/completions", credentials, now=NOW))
            if images:
                boundary.browser_key(headers, "/v1/images/generations", self.images(), now=NOW, need_virtual_key=False)
            else:
                with self.assertRaises(boundary.Refusal) as refused:
                    boundary.browser_key(headers, "/v1/images/generations", self.images(), now=NOW, need_virtual_key=False)
                self.assertEqual("image_permission_denied", refused.exception.code)

    def test_a_settled_state_makes_no_gateway_call_and_no_write(self):
        self.person(MOTHER, PARENTS)
        self.run_job()
        before = (self.mapping(), self.images(), dict(self.state.keys))
        self.state.gateway_calls.clear()
        self.state.kube_calls.clear()
        status = self.run_job()
        self.assertEqual(before, (self.mapping(), self.images(), self.state.keys))
        self.assertEqual([], self.state.gateway_calls)
        self.assertEqual([("PUT", "configmaps", job.STATUS_CONFIGMAP)], self.puts())
        self.assertEqual((1, []), (status["counts"]["keep"], status["changes"]))

    def test_a_key_is_renewed_inside_the_window_and_the_old_one_revoked_after_the_grace(self):
        self.person(MOTHER, PARENTS)
        self.person(CHILD, FAMILY)
        self.run_job()
        first = self.mapping()["users"]
        self.state.now = NOW + 60 * DAY - 60        # 30 days and a minute remain: outside the window
        self.assertEqual([], self.run_job()["changes"])
        self.state.now = NOW + 60 * DAY + 60        # under 30 days remain
        status = self.run_job()
        self.assertEqual(["renew", "renew"], [change["action"] for change in status["changes"]])
        renewed = self.mapping()
        for user in (MOTHER, CHILD):
            entry = renewed["users"][user]
            self.assertNotEqual(first[user]["key"], entry["key"])
            self.assertEqual((f"webui-{user}-b", self.state.now + 90 * DAY), (entry["alias"], entry["expires_at"]))
            # New key first, mapping second; the old key still answers while the gateway may hold the old mapping.
            self.assertIn(first[user]["key"], self.state.keys)
        self.assertEqual({first[MOTHER]["key"], first[CHILD]["key"]}, {item["key"] for item in renewed["retired"]})
        self.assertNotIn("retired", self.images())
        self.assertEqual(2, status["counts"]["retired_pending"])
        self.state.now += job.json.loads((self.root / "config.json").read_text())["retire_grace_seconds"] + 1
        status = self.run_job()
        self.assertEqual({renewed["users"][MOTHER]["key"], renewed["users"][CHILD]["key"]}, set(self.state.keys))
        self.assertNotIn("retired", self.mapping())
        self.assertEqual((0, []), (status["counts"]["retired_pending"], status["changes"]))
        # The next renewal returns to the first alias, which is free again.
        self.state.now += 61 * DAY
        self.run_job()
        self.assertEqual(f"webui-{MOTHER}-a", self.mapping()["users"][MOTHER]["alias"])

    def test_an_expired_key_is_replaced(self):
        self.person(CHILD, FAMILY)
        self.publish({"service_key": SERVICE_KEY, "users": {CHILD: self.manual(days=-1)}})
        self.assertEqual("renew", self.run_job()["changes"][0]["action"])
        self.assertEqual(NOW + 90 * DAY, self.mapping()["users"][CHILD]["expires_at"])

    def test_leaving_both_groups_deletion_and_a_pending_role_revoke_the_key(self):
        removed, deleted, pending = "44444444-removed", "55555555-deleted", "66666666-pending"
        for user in (MOTHER, removed, deleted, pending):
            self.person(user, FAMILY)
        self.run_job()
        keys = {user: entry["key"] for user, entry in self.mapping()["users"].items()}
        self.person(removed)                                      # no group left
        self.webui.execute('DELETE FROM "user" WHERE id = ?', (deleted,))   # the member row is left behind
        self.webui.execute('UPDATE "user" SET role = ? WHERE id = ?', ("pending", pending))
        self.webui.commit()
        status = self.run_job()
        self.assertEqual({MOTHER}, set(self.mapping()["users"]))
        self.assertEqual({MOTHER}, set(self.images()["users"]))
        self.assertEqual({keys[MOTHER]}, set(self.state.keys))
        self.assertEqual((3, 0, "ok"), (status["counts"]["revoke"], status["counts"]["retired_pending"], status["result"]))
        self.assertEqual({"revoke"}, {change["action"] for change in status["changes"]})

    def test_a_revocation_the_gateway_refuses_is_kept_and_retried(self):
        self.person(MOTHER, PARENTS)
        self.person(CHILD, FAMILY)
        self.run_job()
        key = self.mapping()["users"][CHILD]["key"]
        self.person(CHILD)
        self.state.refuse_delete = True
        status = self.run_job(expect=1)
        # Already out of the mapping, so the gateway refuses the user; the key deletion is still owed.
        self.assertEqual(("partial", 1), (status["result"], status["counts"]["retired_pending"]))
        self.assertNotIn(CHILD, self.mapping()["users"])
        self.assertIn(key, self.state.keys)
        self.state.refuse_delete = False
        self.assertEqual(0, self.run_job()["counts"]["retired_pending"])
        self.assertNotIn(key, self.state.keys)
        # Deleting a key that is already gone (404) settles too.
        self.publish({**self.mapping(), "retired": [{"key": "sk-manual-gone-" + "g" * 24, "after": NOW - 1}]})
        self.assertEqual(("ok", 0), (lambda s: (s["result"], s["counts"]["retired_pending"]))(self.run_job()))

    def test_the_administrator_is_left_alone_unless_in_a_group(self):
        bound = self.manual(days=10, images=True)
        self.publish({"service_key": SERVICE_KEY, "users": {ADMIN: bound}})
        self.person(ADMIN, role="admin")
        self.person(MOTHER, PARENTS)
        status = self.run_job()
        self.assertEqual(bound, self.mapping()["users"][ADMIN])
        self.assertIn(bound["key"], self.state.keys)
        # Its hand-bound key is inside the renewal window and nothing will renew it: say so.
        self.assertEqual((1, 1, 1), (status["counts"]["unmanaged"], status["unmanaged_expiring"], status["counts"]["members"]))
        self.assertNotIn(ADMIN, self.state.users)
        # In a group, even Family only, the administrator is managed, with pictures (Admin and Parents).
        self.person(ADMIN, FAMILY, role="admin")
        status = self.run_job()
        entry = self.mapping()["users"][ADMIN]
        self.assertEqual(("renew", True, "parents", 0), (status["changes"][0]["action"], entry["images"], entry["policy"],
                                                         status["unmanaged_expiring"]))

    def test_an_administrator_without_a_key_and_without_a_group_gets_none(self):
        self.person(ADMIN, role="admin")
        self.person(MOTHER, PARENTS)
        self.run_job()
        self.assertEqual({MOTHER}, set(self.mapping()["users"]))

    def test_group_changes_move_the_picture_permission_and_the_limits(self):
        self.person(CHILD, FAMILY)
        self.run_job()
        family_key = self.mapping()["users"][CHILD]["key"]
        self.person(CHILD, FAMILY, PARENTS)
        status = self.run_job()
        entry = self.mapping()["users"][CHILD]
        self.assertEqual("policy", status["changes"][0]["action"])
        self.assertEqual((True, "parents", 2), (entry["images"], entry["policy"],
                                                self.state.keys[entry["key"]]["max_parallel_requests"]))
        self.assertNotEqual(family_key, entry["key"])
        self.assertTrue(self.images()["users"][CHILD]["images"])
        self.person(CHILD, FAMILY)
        self.run_job()
        entry = self.mapping()["users"][CHILD]
        self.assertEqual((False, "family", 1), (entry["images"], entry["policy"],
                                                self.state.keys[entry["key"]]["max_parallel_requests"]))
        self.assertFalse(self.images()["users"][CHILD]["images"])

    def test_a_hand_bound_key_keeps_its_key_and_follows_the_group_for_pictures(self):
        mother, child = self.manual(days=60, images=False), self.manual(days=60, images=True)
        self.publish({"service_key": SERVICE_KEY, "users": {MOTHER: mother, CHILD: child}})
        self.person(MOTHER, PARENTS)
        self.person(CHILD, FAMILY)
        status = self.run_job()
        users = self.mapping()["users"]
        self.assertEqual((mother["key"], True, child["key"], False),
                         (users[MOTHER]["key"], users[MOTHER]["images"], users[CHILD]["key"], users[CHILD]["images"]))
        self.assertEqual({"images"}, {change["action"] for change in status["changes"]})
        self.assertEqual([], self.state.gateway_calls)
        self.assertEqual({MOTHER: True, CHILD: False}, {u: e["images"] for u, e in self.images()["users"].items()})

    def test_malformed_entries_are_replaced_or_removed_and_malformed_ids_get_nothing(self):
        stale = self.manual()
        self.publish({"service_key": SERVICE_KEY, "users": {
            MOTHER: "not-an-entry", CHILD: {"key": "short", "expires_at": "tomorrow"},
            "77777777-gone": {"key": 7}, "88888888-gone": stale}})
        self.person(MOTHER, PARENTS)
        self.person(CHILD, FAMILY)
        self.person("bad id/../x", FAMILY)
        self.person("x" * 65, FAMILY)
        status = self.run_job()
        users = self.mapping()["users"]
        self.assertEqual({MOTHER, CHILD}, set(users))
        self.assertTrue(all(job.valid_entry(entry) for entry in users.values()))
        self.assertEqual((2, 2, 2), (status["counts"]["provision"], status["counts"]["revoke"],
                                     status["counts"]["malformed_members"]))
        self.assertNotIn(stale["key"], self.state.keys)
        self.assertEqual({MOTHER, CHILD}, self.state.users)

    def test_an_unreadable_mapping_is_never_replaced(self):
        self.person(MOTHER, PARENTS)
        for broken in ([], {"users": {}}, {"service_key": SERVICE_KEY, "users": []}, {"service_key": "short", "users": {}},
                       {"service_key": SERVICE_KEY, "users": {}, "retired": [{"key": 1}]}):
            self.publish(broken)
            status = self.run_job(expect=1)
            self.assertEqual(("stopped", "mapping_invalid"), (status["result"], status["code"]))
            self.assertEqual([], self.state.gateway_calls)
            self.assertEqual([("PUT", "configmaps", job.STATUS_CONFIGMAP)], self.puts()[-1:])
            self.assertNotIn(("PUT", "secrets", job.MAPPING_SECRET), self.puts())
            self.assertEqual(broken, self.mapping())
        self.state.objects[("secrets", job.MAPPING_SECRET)]["data"] = {job.MAPPING_KEY: "not base64 json"}
        self.assertEqual("mapping_invalid", self.run_job(expect=1)["code"])
        self.publish({"service_key": SERVICE_KEY, "users": {}})

    def test_image_permissions_that_drifted_are_restored_from_the_mapping(self):
        self.person(MOTHER, PARENTS)
        self.run_job()
        for drifted in ({"service_key": SERVICE_KEY, "users": {}}, "unreadable"):
            self.publish(self.mapping(), images=drifted)
            self.run_job()
            self.assertEqual({MOTHER: {"expires_at": NOW + 90 * DAY, "images": True}}, self.images()["users"])

    def test_a_read_that_looks_broken_changes_nothing(self):
        bound = self.manual()
        self.publish({"service_key": SERVICE_KEY, "users": {MOTHER: bound}})
        cases = {"no_users": lambda: None,
                 "no_eligible_members": lambda: self.person(MOTHER),
                 "group_missing": lambda: (self.webui.execute('DELETE FROM "group" WHERE id = ?', (FAMILY,)), self.webui.commit()),
                 "webui_db_unavailable": lambda: (self.webui.execute("DROP TABLE group_member"), self.webui.commit())}
        for code, change in cases.items():
            change()
            status = self.run_job(expect=1)
            self.assertEqual(("stopped", code), (status["result"], status["code"]))
            self.assertEqual({MOTHER: bound}, self.mapping()["users"])
            self.assertIn(bound["key"], self.state.keys)
            self.assertEqual([], self.state.gateway_calls)

    def test_more_members_than_the_bound_stops_before_any_key(self):
        for number in range(onboarding.MAX_USERS + 1):
            self.person(f"member-{number:02d}", FAMILY)
        self.assertEqual("too_many_users", self.run_job(expect=1)["code"])
        self.assertEqual(({}, []), (self.state.keys, self.state.gateway_calls))

    def test_one_refused_key_does_not_stop_the_others_and_the_next_run_completes(self):
        self.person(MOTHER, PARENTS)
        self.person(CHILD, FAMILY)
        self.state.refuse_generate = {CHILD}
        status = self.run_job(expect=1)
        self.assertEqual(("partial", 1, {MOTHER}), (status["result"], status["counts"]["failed"], set(self.mapping()["users"])))
        self.assertEqual(["done", "failed"], [change["state"] for change in status["changes"]])
        self.state.refuse_generate = set()
        self.assertEqual("ok", self.run_job()["result"])
        self.assertEqual({MOTHER, CHILD}, set(self.mapping()["users"]))

    def test_a_second_run_stops_while_another_holds_the_claim_and_proceeds_once_it_is_stale(self):
        self.person(MOTHER, PARENTS)
        secret = self.state.objects[("secrets", job.MAPPING_SECRET)]
        secret["metadata"]["annotations"] = {job.CLAIM: str(NOW - 30), "kept": "yes"}
        status = self.run_job(expect=1)
        self.assertEqual(("stopped", "another_run_active"), (status["result"], status["code"]))
        self.assertEqual(([], {}, []), (self.state.gateway_calls, self.state.keys,
                                        [call for call in self.puts() if call[1] == "secrets"]))
        # A run that died keeps its mark; it is ignored once older than any run can be.
        cron = next(o for o in self.bundle("live") if o["kind"] == "CronJob")
        self.assertLess(cron["spec"]["jobTemplate"]["spec"]["activeDeadlineSeconds"], job.CLAIM_SECONDS)
        secret["metadata"]["annotations"][job.CLAIM] = str(NOW - job.CLAIM_SECONDS)
        self.assertEqual("ok", self.run_job()["result"])
        self.assertEqual({MOTHER}, set(self.mapping()["users"]))
        self.assertEqual({"kept": "yes"}, self.state.objects[("secrets", job.MAPPING_SECRET)]["metadata"]["annotations"])

    def test_the_claim_is_taken_before_any_gateway_change_and_released_with_the_last_write(self):
        self.person(MOTHER, PARENTS)
        self.state.conflict_at = 1           # the owner changed the Secret between this run's read and its claim
        status = self.run_job(expect=1)
        self.assertEqual(("kubernetes_conflict", [], {}), (status["code"], self.state.gateway_calls, self.state.keys))
        self.run_job()
        self.assertNotIn(job.CLAIM, self.state.objects[("secrets", job.MAPPING_SECRET)]["metadata"].get("annotations", {}))

    def test_a_concurrent_secret_change_is_refused_and_the_orphaned_key_is_cleaned_up(self):
        self.person(MOTHER, PARENTS)
        self.state.conflict_at = 2           # after the claim and the key, at the publish
        status = self.run_job(expect=1)
        self.assertEqual(("stopped", "kubernetes_conflict", {}), (status["result"], status["code"], self.mapping()["users"]))
        orphan = set(self.state.keys)
        self.assertEqual(1, len(orphan))    # issued, but no mapping names it and nobody holds its value
        self.state.now += job.CLAIM_SECONDS  # the stopped run left its mark
        self.assertEqual("ok", self.run_job()["result"])
        self.assertEqual({self.mapping()["users"][MOTHER]["key"]}, set(self.state.keys))
        self.assertFalse(orphan & set(self.state.keys))

    def test_a_wrong_master_key_changes_nothing(self):
        self.person(MOTHER, PARENTS)
        (self.root / "master-key").write_text("sk-wrong-" + "w" * 40)
        status = self.run_job(expect=1)
        self.assertEqual(("partial", 1, {}), (status["result"], status["counts"]["failed"], self.mapping()["users"]))
        # Only the claim and its release reached the Secret; the documents are as they were.
        secret = self.state.objects[("secrets", job.MAPPING_SECRET)]
        self.assertEqual(({"service_key": SERVICE_KEY, "users": {}}, {}), (self.mapping(), secret["metadata"]["annotations"]))
        self.assertNotIn(("PUT", "secrets", job.IMAGE_SECRET), self.puts())
        (self.root / "master-key").write_text(MASTER)

    def test_a_gateway_answer_for_another_user_or_expiry_is_not_bound(self):
        self.person(MOTHER, PARENTS)
        reconciler = job.Reconciler(json.loads((self.root / "config.json").read_text()), None, None, now=NOW)
        good = {"key": "sk-fixture-9999-" + "k" * 24, "user_id": MOTHER, "expires": "2027-02-11T02:40:00+00:00"}
        for answer in ({**good, "user_id": CHILD}, {**good, "key": "plain"}, {**good, "expires": "2027-02-11T02:40:00"},
                       {**good, "expires": "2030-01-01T00:00:00Z"}, {**good, "expires": "2020-01-01T00:00:00Z"},
                       {**good, "expires": None}, None, []):
            reconciler.gateway = SimpleNamespace(call=lambda path, payload, answer=answer: (200, answer))
            self.assertIsNone(reconciler.issue(MOTHER, "parents", None))
        reconciler.gateway = SimpleNamespace(call=lambda path, payload: (200, good))
        self.assertEqual(good["key"], reconciler.issue(MOTHER, "parents", None)["key"])

    def test_members_without_a_home_are_named_by_prefix(self):
        self.configure("live", homes=[MOTHER])
        self.person(MOTHER, PARENTS)
        self.person(CHILD, FAMILY)
        self.assertEqual(["22222222"], self.run_job()["homes_needed"])

    def test_the_database_is_read_through_a_read_only_directory_while_open_webui_writes(self):
        self.person(MOTHER, PARENTS)
        self.webui.execute("BEGIN IMMEDIATE")           # a write in progress; committed data stays readable
        self.webui.execute('INSERT INTO "user" (id, role) VALUES (?, ?)', ("uncommitted", "user"))
        modes = {path: path.stat().st_mode for path in (self.database.parent, *self.database.parent.iterdir())}
        for path in modes:
            path.chmod(0o555 if path.is_dir() else 0o444)
        try:
            read = job.read_webui(self.database)
        finally:
            for path, mode in modes.items():
                path.chmod(mode)
            self.webui.rollback()
        self.assertEqual(({MOTHER: "user"}, {PARENTS, FAMILY}, {PARENTS: {MOTHER}}), (read["users"], read["groups"], read["members"]))
        self.assertEqual(sorted(modes), sorted((self.database.parent, *self.database.parent.iterdir())))
        with mock.patch.object(job.time, "sleep") as slept, self.assertRaises(job.Stop) as stopped:
            job.read_webui(self.root / "absent/webui.db")
        self.assertEqual(("webui_db_unavailable", 4), (stopped.exception.code, slept.call_count))
        self.assertFalse((self.root / "absent").exists())


class DryRunTests(Fixture):
    mode = "dry-run"

    def test_dry_run_reports_the_plan_and_writes_nothing_anywhere(self):
        expiring, leaving = self.manual(days=5), self.manual()
        self.publish({"service_key": SERVICE_KEY, "users": {MOTHER: expiring, "99999999-left": leaving}})
        self.person(MOTHER, PARENTS)
        self.person(CHILD, FAMILY)
        before = repr(self.state.objects), dict(self.state.keys)
        # No master key is mounted in this mode.
        (self.root / "master-key").unlink()
        status = self.run_job()
        self.assertEqual(before, (repr(self.state.objects), self.state.keys))
        self.assertEqual(([], []), (self.state.gateway_calls, self.puts()))
        self.assertEqual({("GET", "secrets", job.MAPPING_SECRET), ("GET", "secrets", job.IMAGE_SECRET)}, set(self.state.kube_calls))
        self.assertEqual([{"user": "11111111", "action": "renew", "tier": "parents", "state": "planned"},
                          {"user": "22222222", "action": "provision", "tier": "family", "state": "planned"},
                          {"user": "99999999", "action": "revoke", "tier": None, "state": "planned"}], status["changes"])
        self.assertEqual(("dry-run", "ok", ["11111111", "22222222"]), (status["mode"], status["result"], status["homes_needed"]))

    def test_the_dry_run_role_cannot_write_even_if_the_job_tried(self):
        kube = job.Kube("ai-home-lab", base=self.url, token=TOKEN)
        secret = kube.get("secrets", job.MAPPING_SECRET)
        with self.assertRaises(job.Stop) as refused:
            kube.put("secrets", job.MAPPING_SECRET, secret)
        self.assertEqual("kubernetes_write_failed", refused.exception.code)
        with self.assertRaises(job.Stop):
            kube.get("configmaps", job.STATUS_CONFIGMAP)
        with self.assertRaises(job.Stop):
            kube.get("secrets", "gateway-credentials")


class RenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.root = Path(cls.directory.name)
        (cls.root / "sandbox.json").write_text(json.dumps(SANDBOX))
        cls.rendered = {}
        for mode in (None, "off", "dry-run", "live"):
            site = cls.root / f"site-{mode}.json"
            site.write_text(json.dumps({**SITE, **({"key_onboarding": mode} if mode else {})}))
            cls.rendered[mode] = cls.root / f"render-{mode}"
            service.render(SimpleNamespace(site=site, output=cls.rendered[mode], mode="shared", experience="assistant-memory",
                                           serving_profile=PROFILE, sandbox_config=cls.root / "sandbox.json"))

    def objects(self, mode, name="onboarding.yaml"):
        return service.load_objects((self.rendered[mode] / name).read_text())

    def named(self, mode, kind, name=onboarding.NAME):
        return next(o for o in self.objects(mode) if o["kind"] == kind and o["metadata"]["name"] == name)

    def test_off_is_the_default_and_renders_nothing(self):
        for mode in (None, "off"):
            self.assertEqual("", (self.rendered[mode] / "onboarding.yaml").read_text())
            evidence = json.loads((self.rendered[mode] / "evidence.json").read_text())
            self.assertEqual({"mode": "off", "qualified": False}, evidence["key_onboarding"])
            self.assertIn("onboarding.yaml", evidence["files"])
        # The flag never changes what a mode switch applies.
        texts = {(self.rendered[mode] / "workloads.yaml").read_text() for mode in self.rendered}
        self.assertEqual(1, len(texts))
        self.assertNotIn("key-onboarding", texts.pop())

    def test_the_flag_needs_the_sandbox_config_and_a_known_value(self):
        site = self.root / "site-live.json"
        with self.assertRaisesRegex(ValueError, "needs --sandbox-config"):
            service.render(SimpleNamespace(site=site, output=self.root / "refused", mode="shared", experience="assistant-memory",
                                           serving_profile=PROFILE, sandbox_config=None))
        (self.root / "site-bad.json").write_text(json.dumps({**SITE, "key_onboarding": "on"}))
        with self.assertRaisesRegex(ValueError, "off, dry-run or live"):
            service.site_values(self.root / "site-bad.json")
        self.assertFalse((self.root / "refused").exists())

    def test_live_identity_is_two_named_secrets_and_one_status_configmap(self):
        self.assertEqual([
            {"apiGroups": [""], "resources": ["secrets"], "verbs": ["get", "update"],
             "resourceNames": ["gateway-browser-keys", "gateway-image-permissions"]},
            {"apiGroups": [""], "resources": ["configmaps"], "verbs": ["get", "update"],
             "resourceNames": ["key-onboarding-status"]}], self.named("live", "Role")["rules"])
        binding = self.named("live", "RoleBinding")
        self.assertEqual(({"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "key-onboarding"},
                          [{"kind": "ServiceAccount", "name": "key-onboarding", "namespace": "ai-home-lab"}]),
                         (binding["roleRef"], binding["subjects"]))
        self.assertIs(False, self.named("live", "ServiceAccount")["automountServiceAccountToken"])
        kinds = sorted((o["kind"], o["metadata"]["name"], o["metadata"]["namespace"]) for o in self.objects("live"))
        self.assertEqual([("ConfigMap", "key-onboarding", "ai-home-lab"), ("ConfigMap", "key-onboarding-status", "ai-home-lab"),
                          ("CronJob", "key-onboarding", "ai-home-lab"), ("NetworkPolicy", "key-onboarding", "ai-home-lab"),
                          ("NetworkPolicy", "litellm-key-onboarding-ingress", "ai-home-lab"),
                          ("Role", "key-onboarding", "ai-home-lab"), ("RoleBinding", "key-onboarding", "ai-home-lab"),
                          ("ServiceAccount", "key-onboarding", "ai-home-lab")], kinds)
        # No cluster-wide grant, no wildcard, no list/watch/create/delete, nothing unnamed.
        text = (self.rendered["live"] / "onboarding.yaml").read_text()
        for word in ("ClusterRole", "'*'", "- list", "- watch", "- create", "- delete", "- patch"):
            self.assertNotIn(word, text)

    def test_dry_run_has_no_admin_credential_no_write_and_no_gateway_flow(self):
        self.assertEqual([{"apiGroups": [""], "resources": ["secrets"], "verbs": ["get"],
                           "resourceNames": ["gateway-browser-keys", "gateway-image-permissions"]}],
                         self.named("dry-run", "Role")["rules"])
        pod = self.named("dry-run", "CronJob")["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        self.assertEqual({"runtime", "webui"}, {volume["name"] for volume in pod["volumes"]})
        self.assertNotIn("gateway-credentials", (self.rendered["dry-run"] / "onboarding.yaml").read_text())
        self.assertEqual({"ConfigMap", "CronJob", "NetworkPolicy", "Role", "RoleBinding", "ServiceAccount"},
                         {o["kind"] for o in self.objects("dry-run")})
        self.assertEqual(["key-onboarding"] * 2, [o["metadata"]["name"] for o in self.objects("dry-run")
                                                  if o["kind"] in ("NetworkPolicy", "ConfigMap")])
        policy = self.named("dry-run", "NetworkPolicy")["spec"]
        self.assertEqual([{"to": [{"ipBlock": {"cidr": "192.168.77.9/32"}}], "ports": [{"protocol": "TCP", "port": 6443}]}],
                         policy["egress"])

    def test_the_job_runs_restricted_with_only_the_master_key_and_a_read_only_database(self):
        cron = self.named("live", "CronJob")["spec"]
        job_spec = cron["jobTemplate"]["spec"]
        pod = job_spec["template"]["spec"]
        container, = pod["containers"]
        self.assertEqual(("*/5 * * * *", "Forbid", 0, "Never"),
                         (cron["schedule"], cron["concurrencyPolicy"], job_spec["backoffLimit"], pod["restartPolicy"]))
        self.assertLess(job_spec["activeDeadlineSeconds"], 300)
        workloads = self.objects("live", "workloads.yaml")
        webui = next(o for o in workloads if o["kind"] == "Deployment" and o["metadata"]["name"] == "open-webui")
        webui_pod = webui["spec"]["template"]["spec"]
        self.assertEqual(webui_pod["containers"][0]["image"], container["image"])
        self.assertRegex(container["image"], r"@sha256:[a-f0-9]{64}$")
        self.assertEqual(["python", "-I", "-S", "/runtime/key_onboarding.py"], container["command"])
        self.assertEqual({"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
                         container["securityContext"])
        self.assertEqual((True, 1000, {"type": "RuntimeDefault"}), (pod["securityContext"]["runAsNonRoot"],
                         pod["securityContext"]["runAsUser"], pod["securityContext"]["seccompProfile"]))
        self.assertEqual(webui_pod["securityContext"]["runAsUser"], pod["securityContext"]["runAsUser"])
        volumes = {volume["name"]: volume for volume in pod["volumes"]}
        claim = next(v["persistentVolumeClaim"]["claimName"] for v in webui_pod["volumes"] if v["name"] == "data")
        self.assertEqual({"claimName": claim, "readOnly": True}, volumes["webui"]["persistentVolumeClaim"])
        self.assertEqual({"secretName": "gateway-credentials", "defaultMode": 0o440,
                          "items": [{"key": "master-key", "path": "master-key"}]}, volumes["master"]["secret"])
        self.assertTrue(all(mount["readOnly"] for mount in container["volumeMounts"]))
        self.assertNotIn("env", container)
        self.assertEqual("key-onboarding", pod["serviceAccountName"])
        # No other workload gains the master key, the token or the claim through this feature.
        for obj in workloads:
            if obj["kind"] == "Deployment" and obj["metadata"]["name"] != "litellm":
                self.assertNotIn("gateway-credentials", json.dumps(obj))

    def test_flows_are_the_api_server_and_the_gateway_only(self):
        policy = self.named("live", "NetworkPolicy")["spec"]
        self.assertEqual(({"matchLabels": {"app.kubernetes.io/name": "key-onboarding"}}, ["Ingress", "Egress"], []),
                         (policy["podSelector"], policy["policyTypes"], policy["ingress"]))
        self.assertEqual([
            {"to": [{"ipBlock": {"cidr": "192.168.77.9/32"}}], "ports": [{"protocol": "TCP", "port": 6443}]},
            {"to": [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "litellm"}}}],
             "ports": [{"protocol": "TCP", "port": 4000}]}], policy["egress"])
        gateway = self.named("live", "NetworkPolicy", "litellm-key-onboarding-ingress")["spec"]
        self.assertEqual(({"matchLabels": {"app.kubernetes.io/name": "litellm"}}, ["Ingress"],
                          [{"from": [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "key-onboarding"}}}],
                            "ports": [{"protocol": "TCP", "port": 4000}]}]),
                         (gateway["podSelector"], gateway["policyTypes"], gateway["ingress"]))
        labels = self.named("live", "CronJob")["spec"]["jobTemplate"]["spec"]["template"]["metadata"]["labels"]
        self.assertEqual({"app.kubernetes.io/name": "key-onboarding"}, labels)

    def test_the_rendered_config_is_what_the_job_accepts_and_within_the_key_bounds(self):
        data = self.named("live", "ConfigMap")["data"]
        self.assertEqual((ROOT / "infrastructure/service/key_onboarding.py").read_text(), data["key_onboarding.py"])
        path = self.root / "config.json"
        path.write_text(data["config.json"])
        config = job.read_config(path)
        self.assertEqual(("live", PARENTS, FAMILY, ["parents", "family"], []),
                         (config["mode"], config["parents_group_id"], config["family_group_id"], config["home_tiers"],
                          config["home_user_ids"]))
        for tier, policy in config["policies"].items():
            self.assertLessEqual(set(policy["models"]), service.MODELS)
            for name, bound in (*service.KEY_LIMITS.items(), ("days", 90)):
                self.assertTrue(1 <= policy[name] <= bound, (tier, name))
            self.assertLess(config["renew_days"], policy["days"])
        self.assertEqual((True, False), (config["policies"]["parents"]["images"], config["policies"]["family"]["images"]))
        self.assertNotIn("sk-", data["config.json"])
        for broken in ({**config, "mode": "on"}, {**config, "extra": 1}, {**config, "family_group_id": PARENTS},
                       {**config, "renew_days": 90}, {k: v for k, v in config.items() if k != "policies"}):
            path.write_text(json.dumps(broken))
            with self.assertRaises(job.Stop):
                job.read_config(path)

    def test_a_claim_that_cannot_be_shared_or_an_unpinned_image_is_refused(self):
        webui = {"kind": "Deployment", "metadata": {"name": "open-webui"}, "spec": {"template": {"spec": {
            "containers": [{"image": "registry.example.test/open-webui@sha256:" + "c" * 64}],
            "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "data"}}]}}}}
        claim = {"kind": "PersistentVolumeClaim", "metadata": {"name": "data"}, "spec": {"accessModes": ["ReadWriteOncePod"]}}
        sandbox = {**SANDBOX, "users": []}
        with self.assertRaisesRegex(ValueError, "ReadWriteOnce"):
            onboarding.render("live", sandbox, [webui, claim], MODELS, ROOT)
        claim["spec"]["accessModes"] = ["ReadWriteOnce"]
        webui["spec"]["template"]["spec"]["containers"][0]["image"] = "registry.example.test/open-webui:latest"
        with self.assertRaisesRegex(ValueError, "pinned by digest"):
            onboarding.render("live", sandbox, [webui, claim], MODELS, ROOT)
        with self.assertRaisesRegex(ValueError, "off, dry-run or live"):
            onboarding.render("on", sandbox, [webui, claim], MODELS, ROOT)


if __name__ == "__main__":
    unittest.main()
