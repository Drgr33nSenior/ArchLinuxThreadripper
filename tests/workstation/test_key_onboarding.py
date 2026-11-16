"""Family key onboarding: decisions against a WebUI database fixture, a fake gateway admin API and a fake
Kubernetes API that grants only what the rendered Role grants; and the rendered CronJob, identity and flows."""
import base64
import http.client
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


def uid(number):
    return f"{number:08d}-0000-4000-8000-000000000000"


def encoded(document):
    return base64.b64encode(json.dumps(document).encode()).decode()


class Fakes(http.server.BaseHTTPRequestHandler):
    """One server: the LiteLLM routes the job uses, and the two Secrets and one ConfigMap of the Role."""
    state = None

    def log_message(self, *args):
        pass

    def answer(self, status, value=None, headers=()):
        body = json.dumps(value if value is not None else {"error": "refused"}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, header in headers:
            self.send_header(name, header)
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
            state.put_count[name] = state.put_count.get(name, 0) + 1
            if (name, state.put_count[name]) in state.refuse_put \
                    or body["metadata"]["resourceVersion"] != current["metadata"]["resourceVersion"]:
                return self.answer(409)
            body["metadata"]["resourceVersion"] = str(int(current["metadata"]["resourceVersion"]) + 1)
            state.objects[(kind, name)] = current = body
            if kind == "secrets":
                state.events.append(("put", name, json.loads(base64.b64decode(next(iter(body["data"].values()))))))
        self.answer(200, current)

    def gateway(self, body):
        state = self.state
        state.gateway_calls.append((self.path, {name: value for name, value in body.items() if name != "keys"}))
        if self.path == "/redirect":
            return self.answer(302, headers=[("Location", "/v1/models")])
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
            state.events.append(("issue", key))
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
                state.events.append(("delete", key))
            return self.answer(200, {"deleted_keys": len(found)})
        self.answer(404)

    def do_GET(self):
        if self.path == "/redirect":
            return self.answer(302, headers=[("Location", "/v1/models")])
        if self.path == "/v1/models":      # the gateway's cheapest authenticated call, with a user's own key
            self.state.probes += 1
            known = self.headers["Authorization"].removeprefix("Bearer ") in self.state.keys
            return self.answer(self.state.models_status or (200 if known else 401), {"data": []})
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
        self.webui.executescript("""
            CREATE TABLE "user" (id TEXT PRIMARY KEY, email TEXT, role TEXT, name TEXT);
            CREATE TABLE "group" (id TEXT PRIMARY KEY, name TEXT, parent_group_id TEXT);
            CREATE TABLE group_member (id TEXT PRIMARY KEY, group_id TEXT NOT NULL, user_id TEXT NOT NULL);""")
        self.webui.executemany('INSERT INTO "group" (id, name) VALUES (?, ?)', [(PARENTS, "Parents"), (FAMILY, "Family")])
        self.webui.commit()
        (self.root / "master-key").write_text(MASTER + "\n")
        self.state = Fakes.state = SimpleNamespace(
            now=NOW, users=set(), keys={}, serial=0, gateway_calls=[], kube_calls=[], events=[], probes=0, models_status=None,
            refuse_generate=set(), refuse_delete=False, refuse_put=set(), put_count={}, objects={}, verbs={})
        self.publish({"service_key": SERVICE_KEY, "users": {}})
        self.state.objects[("configmaps", job.STATUS_CONFIGMAP)] = {"metadata": {"resourceVersion": "1"}, "data": {}}
        self.configure(self.mode)
        self.outputs = []

    def configure(self, mode, homes=(), **settings):
        sandbox = {**SANDBOX, "users": [{"webui_user_id": user} for user in homes]}
        self.config = {**onboarding.job_config(mode, sandbox, MODELS, **settings), "gateway": self.url, "database": str(self.database)}
        (self.root / "config.json").write_text(json.dumps(self.config))
        # Exactly the rendered Role, so a run that needs more is refused here as it would be in the cluster.
        role = next(o for o in self.bundle(mode) if o["kind"] == "Role")
        verbs = {"get": "GET", "update": "PUT"}
        self.state.verbs = {(rule["resources"][0], name): {verbs[v] for v in rule["verbs"]}
                            for rule in role["rules"] for name in rule["resourceNames"]}

    @staticmethod
    def bundle(mode):
        webui = {"kind": "Deployment", "metadata": {"name": "open-webui"}, "spec": {"template": {"spec": {
            "nodeSelector": {"kubernetes.io/os": "linux"},
            "containers": [{"image": "registry.example.test/open-webui@sha256:" + "c" * 64,
                            "env": [{"name": "ENABLE_SIGNUP", "value": "false"}]}],
            "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "webui-fixture-data"}}]}}}}
        claim = {"kind": "PersistentVolumeClaim", "metadata": {"name": "webui-fixture-data"},
                 "spec": {"accessModes": ["ReadWriteOnce"]}}
        return onboarding.render(mode, {**SANDBOX, "users": []}, [webui, claim], MODELS, ROOT)

    def publish(self, mapping, images=None):
        """The owner's two Secrets, as `service credentials` and `browser-bind` leave them."""
        version = str(int(self.state.objects.get(("secrets", job.MAPPING_SECRET), {"metadata": {"resourceVersion": "0"}})
                          ["metadata"]["resourceVersion"]) + 1)
        derived = {"service_key": mapping["service_key"], "users": {
            user: {"expires_at": entry["expires_at"], "images": entry.get("images", False)}
            for user, entry in mapping["users"].items() if isinstance(entry, dict) and "expires_at" in entry}} \
            if isinstance(mapping, dict) and {"service_key", "users"} <= set(mapping) and isinstance(mapping["users"], dict) else {}
        for name, key, document in ((job.MAPPING_SECRET, job.MAPPING_KEY, mapping),
                                    (job.IMAGE_SECRET, job.IMAGE_KEY, derived if images is None else images)):
            self.state.objects[("secrets", name)] = {"kind": "Secret", "metadata": {"name": name, "resourceVersion": version},
                                                     "data": {key: encoded(document)}}
        self.state.events.append(("put", job.MAPPING_SECRET, mapping))      # what the gateway reads from now on

    def person(self, user, *groups, role="user"):
        self.webui.execute('INSERT OR REPLACE INTO "user" (id, email, role, name) VALUES (?, ?, ?, ?)',
                           (user, user + "@fixture.test", role, "Fixture"))
        self.webui.execute("DELETE FROM group_member WHERE user_id = ?", (user,))
        for group in groups:
            self.webui.execute("INSERT INTO group_member VALUES (?, ?, ?)", (group + user, group, user))
        self.webui.commit()

    def secret(self, name=None):
        return self.state.objects[("secrets", name or job.MAPPING_SECRET)]

    def mapping(self):
        return json.loads(base64.b64decode(self.secret()["data"][job.MAPPING_KEY]))

    def images(self):
        return json.loads(base64.b64decode(self.secret(job.IMAGE_SECRET)["data"][job.IMAGE_KEY]))

    def manual(self, days=60, images=False, alias=None, **extra):
        """A key the owner issued by hand, known to the gateway."""
        self.state.serial += 1
        key = f"sk-manual-{self.state.serial:04d}-" + "h" * 24
        self.state.keys[key] = {"key_alias": alias or f"browser-{self.state.serial}", "user_id": "manual"}
        return {"key": key, "expires_at": self.state.now + days * DAY, "images": images, **extra}

    def run_job(self, expect=0):
        output = io.StringIO()
        with mock.patch.object(job.time, "sleep"):
            code = job.main(self.root / "config.json", self.root / "master-key",
                            kube=job.Kube("ai-home-lab", base=self.url, token=TOKEN), now=self.state.now, output=output)
        self.outputs.append(output.getvalue())
        self.assertEqual(expect, code, output.getvalue())
        return json.loads(output.getvalue())

    def puts(self, kind=None):
        return [call for call in self.state.kube_calls if call[0] == "PUT" and kind in (None, call[1])]

    def posts(self):
        return [path for path, _ in self.state.gateway_calls]

    def refuse(self, name, nth):
        """Refuse the nth replace of a Secret from now on, as a concurrent change would."""
        self.state.refuse_put = {(name, self.state.put_count.get(name, 0) + nth)}

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
        # Whatever happened: a key is deleted at the gateway only after a published mapping stopped naming it,
        # and the image permissions never grant what the mapping published before them does not.
        named, pictures = set(), {}
        for event in self.state.events:
            if event[0] == "put" and event[1] == job.MAPPING_SECRET and isinstance(event[2], dict) \
                    and isinstance(event[2].get("users"), dict):
                entries = {user: entry for user, entry in event[2]["users"].items() if isinstance(entry, dict)}
                named = {entry.get("key") for entry in entries.values()}
                pictures = {user: entry.get("images") is True for user, entry in entries.items()}
            elif event[0] == "put" and event[1] == job.IMAGE_SECRET:
                for user, entry in event[2]["users"].items():
                    self.assertTrue(not entry["images"] or pictures.get(user), "pictures granted before the mapping")
            elif event[0] == "delete":
                self.assertNotIn(event[1], named, "a key was deleted while the published mapping still named it")


class ReconcileTests(Fixture):
    def test_new_members_get_a_key_for_their_exact_id_with_their_groups_policy(self):
        self.person(MOTHER, PARENTS, FAMILY)   # both groups: the most permissive wins
        self.person(CHILD, FAMILY)
        status = self.run_job()
        users = self.mapping()["users"]
        self.assertEqual({"service_key", "users"}, set(self.mapping()))
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
        self.assertEqual(("ok", 2, 2, 0, 0), (status["result"], status["counts"]["members"], status["counts"]["provision"],
                                              status["counts"]["failed"], status["counts"]["sweep_pending"]))
        self.assertEqual([{"user": "11111111", "action": "provision", "tier": "parents", "state": "done"},
                          {"user": "22222222", "action": "provision", "tier": "family", "state": "done"}], status["changes"])
        self.assertEqual((90, "checked"), (status["earliest_expiry_days"], status["liveness"]))
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

    def test_a_settled_state_changes_nothing_at_the_gateway_and_writes_no_secret(self):
        self.person(MOTHER, PARENTS)
        self.run_job()
        before = (self.mapping(), self.images(), dict(self.state.keys))
        self.state.gateway_calls.clear()
        self.state.kube_calls.clear()
        self.state.probes = 0
        status = self.run_job()
        self.assertEqual(before, (self.mapping(), self.images(), self.state.keys))
        # One question per mapped key (does the gateway still accept it?), nothing else.
        self.assertEqual(([], 1), (self.state.gateway_calls, self.state.probes))
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
        self.assertEqual({(first[MOTHER]["key"], MOTHER), (first[CHILD]["key"], CHILD)},
                         {(item["key"], item["user"]) for item in renewed["retired"]})
        self.assertEqual({"service_key", "users"}, set(self.images()))
        self.assertEqual((2, 0, "ok"), (status["counts"]["retired_pending"], status["counts"]["sweep_pending"], status["result"]))
        self.state.now += self.config["retire_grace_seconds"] - 5
        self.run_job()
        self.assertEqual(4, len(self.state.keys))   # still inside the grace
        self.state.now += 6
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
        removed, deleted, pending = uid(4), uid(5), uid(6)
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
        self.assertEqual((3, 0, 0, "ok"), (status["counts"]["revoke"], status["counts"]["retired_pending"],
                                           status["counts"]["sweep_pending"], status["result"]))
        self.assertEqual({"revoke"}, {change["action"] for change in status["changes"]})
        self.assertEqual({"service_key", "users"}, set(self.mapping()))

    def test_the_last_member_leaving_is_revoked_too(self):
        self.configure("live", admin_policy="unmanaged")
        self.person(ADMIN, role="admin")
        self.person(CHILD, FAMILY)
        self.run_job()
        self.person(CHILD)          # both groups are empty now; the groups and the user table are still there
        status = self.run_job()
        self.assertEqual(("ok", 1, 0), (status["result"], status["counts"]["revoke"], status["counts"]["members"]))
        self.assertEqual(({}, {}, {}), (self.mapping()["users"], self.images()["users"], self.state.keys))

    def test_removal_also_deletes_keys_the_mapping_never_named_and_keys_in_their_grace(self):
        self.person(MOTHER, PARENTS)
        self.person(CHILD, FAMILY)
        self.run_job()
        mother = self.mapping()["users"][MOTHER]["key"]
        # A renewal leaves the previous key valid for ten minutes; the account is removed inside them.
        self.state.now += 61 * DAY
        self.run_job()
        self.assertEqual(4, len(self.state.keys))
        self.state.now += 60
        self.person(CHILD)
        status = self.run_job()
        self.assertEqual(("ok", 1), (status["result"], status["counts"]["retired_pending"]))   # the mother's, in its grace
        self.assertEqual({mother, self.mapping()["users"][MOTHER]["key"]}, set(self.state.keys))
        # A key issued for an account whose mapping write then failed, and the account is removed before the retry.
        other = uid(7)
        self.person(other, FAMILY)
        self.refuse(job.MAPPING_SECRET, 2)
        self.assertEqual("kubernetes_conflict", self.run_job(expect=1)["code"])
        orphan = [key for key, record in self.state.keys.items() if record["user_id"] == other]
        self.assertEqual((1, [other]), (len(orphan), self.mapping()["sweep"]))
        self.assertNotIn(other, self.mapping()["users"])
        self.person(other)
        self.state.now += job.CLAIM_SECONDS
        status = self.run_job()
        self.assertEqual(("ok", 0, []), (status["result"], status["counts"]["sweep_pending"], status["changes"]))
        self.assertNotIn(orphan[0], self.state.keys)
        self.assertNotIn("sweep", self.mapping())

    def test_a_revocation_the_gateway_refuses_is_kept_and_retried(self):
        self.person(MOTHER, PARENTS)
        self.person(CHILD, FAMILY)
        self.run_job()
        key = self.mapping()["users"][CHILD]["key"]
        self.person(CHILD)
        self.state.refuse_delete = True
        status = self.run_job(expect=1)
        # Already out of the mapping, so the gateway refuses the user; the key deletion is still owed.
        self.assertEqual(("partial", 1, 1), (status["result"], status["counts"]["retired_pending"], status["counts"]["sweep_pending"]))
        self.assertNotIn(CHILD, self.mapping()["users"])
        self.assertNotIn(CHILD, self.images()["users"])
        self.assertIn(key, self.state.keys)
        self.assertNotIn(job.CLAIM, self.secret()["metadata"]["annotations"])
        self.state.refuse_delete = False
        status = self.run_job()
        self.assertEqual((0, 0), (status["counts"]["retired_pending"], status["counts"]["sweep_pending"]))
        self.assertNotIn(key, self.state.keys)
        # Deleting a key that is already gone (404) settles too.
        self.publish({**self.mapping(), "retired": [{"key": "sk-manual-gone-" + "g" * 24, "after": NOW - 1}]})
        self.assertEqual(("ok", 0), (lambda s: (s["result"], s["counts"]["retired_pending"]))(self.run_job()))

    def test_the_administrator_gets_the_parents_policy_and_renewal_without_any_group(self):
        bound = self.manual(days=10, images=False)
        self.publish({"service_key": SERVICE_KEY, "users": {ADMIN: bound}})
        self.person(ADMIN, role="admin")
        status = self.run_job()
        entry = self.mapping()["users"][ADMIN]
        self.assertEqual(("renew", "parents", True, NOW + 90 * DAY, 0),
                         (status["changes"][0]["action"], entry["policy"], entry["images"], entry["expires_at"],
                          status["counts"]["unmanaged"]))
        self.assertEqual(onboarding.POLICIES["parents"]["rpm"], self.state.keys[entry["key"]]["rpm_limit"])
        # And a key where there was none.
        other = uid(8)
        self.person(other, role="admin")
        self.run_job()
        self.assertEqual("parents", self.mapping()["users"][other]["policy"])

    def test_with_admin_policy_unmanaged_the_administrator_is_left_alone_unless_in_a_group(self):
        self.configure("live", admin_policy="unmanaged")
        bound = self.manual(days=10, images=True)
        self.publish({"service_key": SERVICE_KEY, "users": {ADMIN: bound}})
        self.person(ADMIN, role="admin")
        self.person(uid(8), role="admin")       # no key, no group: none is issued
        self.person(MOTHER, PARENTS)
        status = self.run_job()
        self.assertEqual({ADMIN: bound, MOTHER: self.mapping()["users"][MOTHER]}, self.mapping()["users"])
        self.assertIn(bound["key"], self.state.keys)
        # Its hand-bound key is inside the renewal window and nothing will renew it: say so.
        self.assertEqual((1, 1, 1), (status["counts"]["unmanaged"], status["unmanaged_expiring"], status["counts"]["members"]))
        self.assertEqual({MOTHER}, self.state.users)
        # In a group, even Family only, the administrator is managed, with pictures (Admin and Parents).
        self.person(ADMIN, FAMILY, role="admin")
        status = self.run_job()
        entry = self.mapping()["users"][ADMIN]
        self.assertEqual(("renew", True, "parents", 0), (status["changes"][0]["action"], entry["images"], entry["policy"],
                                                         status["unmanaged_expiring"]))

    def test_group_changes_move_the_picture_permission_and_the_limits(self):
        self.person(CHILD, FAMILY)
        self.run_job()
        family_key = self.mapping()["users"][CHILD]["key"]
        self.person(CHILD, FAMILY, PARENTS)
        status = self.run_job()
        entry = self.mapping()["users"][CHILD]
        self.assertEqual("policy", status["changes"][0]["action"])
        self.assertEqual((True, "parents", 60), (entry["images"], entry["policy"], self.state.keys[entry["key"]]["rpm_limit"]))
        self.assertNotEqual(family_key, entry["key"])
        self.assertTrue(self.images()["users"][CHILD]["images"])
        self.person(CHILD, FAMILY)
        self.run_job()
        entry = self.mapping()["users"][CHILD]
        self.assertEqual((False, "family", 30), (entry["images"], entry["policy"], self.state.keys[entry["key"]]["rpm_limit"]))
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

    def test_a_child_never_keeps_pictures_because_a_key_could_not_be_issued(self):
        # A hand-bound --images key inside the renewal window, and a parent moved to Family only.
        demoted = uid(9)
        self.person(MOTHER, PARENTS)
        self.person(demoted, PARENTS)
        self.run_job()
        self.assertTrue(self.images()["users"][demoted]["images"])
        child = self.manual(days=10, images=True)
        self.publish({**self.mapping(), "users": {**self.mapping()["users"], CHILD: child}})
        self.assertTrue(self.images()["users"][CHILD]["images"])
        self.person(CHILD, FAMILY)
        self.person(demoted, FAMILY)
        self.state.refuse_generate = {CHILD, demoted}
        for _ in range(2):
            status = self.run_job(expect=1)
            self.assertEqual(("partial", 2), (status["result"], status["counts"]["failed"]))
            for user in (CHILD, demoted):
                self.assertIs(False, self.mapping()["users"][user]["images"])
                self.assertIs(False, self.images()["users"][user]["images"])
            self.assertEqual(child["key"], self.mapping()["users"][CHILD]["key"])    # chat keeps working meanwhile
            self.assertTrue(self.images()["users"][MOTHER]["images"])
            self.state.now += 300

    def test_pictures_are_denied_before_the_mapping_changes_and_granted_only_after_it(self):
        gone, demoted, promoted = uid(4), uid(5), uid(6)
        self.person(MOTHER, PARENTS)
        self.person(gone, PARENTS)
        self.person(demoted, PARENTS)
        self.person(promoted, FAMILY)
        self.run_job()
        self.person(gone)
        self.person(demoted, FAMILY)
        self.person(promoted, PARENTS)
        self.state.events.clear()
        # The first replace of the mapping fails, as on a concurrent change: the denials are already published.
        self.refuse(job.MAPPING_SECRET, 1)
        self.assertEqual("kubernetes_conflict", self.run_job(expect=1)["code"])
        self.assertEqual([("put", job.IMAGE_SECRET)], [event[:2] for event in self.state.events])
        pictures = {user: entry["images"] for user, entry in self.images()["users"].items()}
        self.assertEqual({MOTHER: True, demoted: False, promoted: False}, pictures)
        self.assertTrue(self.mapping()["users"][gone]["images"])      # the mapping itself did not change
        # The next run completes; the grant appears in the image permissions only after the mapping carries it.
        self.state.events.clear()
        self.assertEqual("ok", self.run_job()["result"])
        self.assertEqual({MOTHER: True, demoted: False, promoted: True},
                         {user: entry["images"] for user, entry in self.images()["users"].items()})
        puts = [event for event in self.state.events if event[0] == "put"]
        granted = next(i for i, e in enumerate(puts) if e[1] == job.IMAGE_SECRET and e[2]["users"][promoted]["images"])
        carried = next(i for i, e in enumerate(puts) if e[1] == job.MAPPING_SECRET
                       and e[2]["users"].get(promoted, {}).get("images") is True)
        self.assertLess(carried, granted)

    def test_a_failed_image_write_leaves_pictures_denied_not_granted(self):
        self.person(MOTHER, PARENTS)
        self.person(CHILD, FAMILY)
        self.run_job()
        self.person(CHILD, PARENTS)
        self.refuse(job.IMAGE_SECRET, 1)        # the grant itself fails after the mapping was published
        self.assertEqual("kubernetes_conflict", self.run_job(expect=1)["code"])
        self.assertEqual((True, False), (self.mapping()["users"][CHILD]["images"], self.images()["users"][CHILD]["images"]))
        self.state.now += job.CLAIM_SECONDS
        self.run_job()
        self.assertTrue(self.images()["users"][CHILD]["images"])

    def test_an_unusable_entry_never_carries_pictures(self):
        entry = {"key": "short", "expires_at": NOW + 60 * DAY, "images": True}
        mapping = {"service_key": SERVICE_KEY, "users": {CHILD: entry, MOTHER: {"key": "sk-" + "v" * 30, "expires_at": NOW + 5,
                                                                              "images": "yes"}}}
        self.assertEqual({MOTHER: {"expires_at": NOW + 5, "images": False}}, job.image_document(mapping)["users"])
        self.publish(mapping)
        self.person(CHILD, FAMILY)
        self.state.refuse_generate = {CHILD}
        self.run_job(expect=1)
        self.assertIs(False, self.mapping()["users"][CHILD]["images"])
        self.assertNotIn(CHILD, self.images()["users"])

    def test_malformed_entries_are_replaced_or_removed_and_malformed_ids_get_nothing(self):
        stale = self.manual()
        self.publish({"service_key": SERVICE_KEY, "users": {
            MOTHER: "not-an-entry", CHILD: {"key": "short", "expires_at": "tomorrow"},
            "77777777-gone": {"key": 7}, "88888888-gone": stale}})
        self.person(MOTHER, PARENTS)
        self.person(CHILD, FAMILY)
        for malformed in ("bad id/../x", "x" * 36, MOTHER.upper(), "44444444-not-a-uuid"):
            self.person(malformed, FAMILY)
        status = self.run_job()
        users = self.mapping()["users"]
        self.assertEqual({MOTHER, CHILD}, set(users))
        self.assertTrue(all(job.valid_entry(entry) for entry in users.values()))
        self.assertEqual((2, 2, 4), (status["counts"]["provision"], status["counts"]["revoke"],
                                     status["counts"]["malformed_members"]))
        self.assertNotIn(stale["key"], self.state.keys)
        self.assertEqual({MOTHER, CHILD}, self.state.users)
        # No alias is ever built from an id that is not a UUID.
        self.assertTrue(all(alias.startswith(("webui-" + MOTHER, "webui-" + CHILD)) for path, body in self.state.gateway_calls
                            for alias in body.get("key_aliases", [])))

    def test_an_unreadable_mapping_is_never_replaced(self):
        self.person(MOTHER, PARENTS)
        good = {"service_key": SERVICE_KEY, "users": {}}
        for broken in ([], {"users": {}}, {"service_key": SERVICE_KEY, "users": []}, {"service_key": "short", "users": {}},
                       {**good, "retired": [{"key": 1}]}, {**good, "retired": [{"key": "not-a-key", "after": 0}]},
                       {**good, "retired": [{"key": "sk-" + "r" * 30}]}, {**good, "retired": [{"key": "sk-" + "r" * 30, "after": 0, "user": 3}]},
                       {**good, "sweep": ["not-a-uuid"]}, {**good, "sweep": MOTHER}):
            self.publish(broken)
            status = self.run_job(expect=1)
            self.assertEqual(("stopped", "mapping_invalid"), (status["result"], status["code"]))
            self.assertEqual([], self.state.gateway_calls)
            self.assertEqual([("PUT", "configmaps", job.STATUS_CONFIGMAP)], self.puts()[-1:])
            self.assertEqual([], self.puts("secrets"))
            self.assertEqual(broken, self.mapping())
        self.secret()["data"] = {job.MAPPING_KEY: "not base64 json"}
        self.assertEqual("mapping_invalid", self.run_job(expect=1)["code"])
        self.publish(good)

    def test_image_permissions_that_drifted_are_restored_from_the_mapping(self):
        self.person(MOTHER, PARENTS)
        self.person(CHILD, FAMILY)
        self.run_job()
        expected = {MOTHER: {"expires_at": NOW + 90 * DAY, "images": True}, CHILD: {"expires_at": NOW + 90 * DAY, "images": False}}
        for drifted in ({"service_key": SERVICE_KEY, "users": {}}, "unreadable",
                        {"service_key": SERVICE_KEY, "users": {CHILD: {"expires_at": NOW + 900 * DAY, "images": True},
                                                               uid(4): {"expires_at": NOW + DAY, "images": True}}}):
            self.publish(self.mapping(), images=drifted)
            self.run_job()
            self.assertEqual(expected, self.images()["users"])

    def test_a_read_that_looks_broken_changes_nothing(self):
        bound = self.manual()
        self.publish({"service_key": SERVICE_KEY, "users": {MOTHER: bound}})
        cases = {"no_users": lambda: None,
                 "group_missing": lambda: (self.person(MOTHER), self.webui.execute('DELETE FROM "group" WHERE id = ?', (FAMILY,)),
                                           self.webui.commit()),
                 "webui_db_unavailable": lambda: (self.webui.execute("DROP TABLE group_member"), self.webui.commit())}
        for code, change in cases.items():
            change()
            status = self.run_job(expect=1)
            self.assertEqual(("stopped", code), (status["result"], status["code"]))
            self.assertEqual({MOTHER: bound}, self.mapping()["users"])
            self.assertIn(bound["key"], self.state.keys)
            self.assertEqual(([], []), (self.state.gateway_calls, self.puts("secrets")))

    def test_more_members_than_the_bound_stops_before_any_key(self):
        for number in range(onboarding.MAX_USERS + 1):
            self.person(uid(100 + number), FAMILY)
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
        self.assertEqual(2, len(self.state.keys))

    def test_a_mapped_key_the_gateway_no_longer_accepts_is_replaced(self):
        self.person(MOTHER, PARENTS)
        self.person(CHILD, FAMILY)
        self.run_job()
        dead = self.mapping()["users"][CHILD]["key"]
        del self.state.keys[dead]           # deleted at the gateway behind the mapping's back (a stale mapping republished)
        status = self.run_job()
        self.assertEqual(("ok", 1, [{"user": "22222222", "action": "dead", "tier": "family", "state": "done"}]),
                         (status["result"], status["counts"]["dead"], status["changes"]))
        replaced = self.mapping()["users"][CHILD]["key"]
        self.assertNotEqual(dead, replaced)
        self.assertIn(replaced, self.state.keys)
        # While it cannot be replaced, the run fails, which is what the alert watches.
        del self.state.keys[replaced]
        self.state.refuse_generate = {CHILD}
        self.assertEqual(("partial", 1), (lambda s: (s["result"], s["counts"]["failed"]))(self.run_job(expect=1)))
        self.state.refuse_generate = set()
        self.state.now += 300
        self.run_job()
        # A gateway that does not answer the question (maintenance, restart) proves nothing: no key is replaced.
        before = self.mapping()
        self.state.models_status = 503
        self.state.keys.clear()
        status = self.run_job()
        self.assertEqual((0, before), (status["counts"]["dead"], self.mapping()))

    def test_a_second_run_leaves_the_gateway_alone_while_another_holds_the_claim(self):
        self.person(MOTHER, PARENTS)
        self.person(CHILD, PARENTS)
        self.run_job()
        self.person(CHILD, FAMILY)          # a demotion is waiting
        self.person(uid(4), FAMILY)         # and a new member
        self.secret()["metadata"]["annotations"] = {job.CLAIM: str(NOW - 30), "kept": "yes"}
        self.state.gateway_calls.clear()
        self.state.kube_calls.clear()
        before = dict(self.state.keys)
        status = self.run_job(expect=1)
        self.assertEqual(("stopped", "another_run_active"), (status["result"], status["code"]))
        self.assertEqual(([], before), (self.state.gateway_calls, self.state.keys))
        # The mark does not hold up a denial: the image permissions already refuse the demoted account.
        self.assertEqual([("PUT", "secrets", job.IMAGE_SECRET)], self.puts("secrets"))
        self.assertIs(False, self.images()["users"][CHILD]["images"])
        # A run that died keeps its mark. The next schedule, even a few seconds early, is not held up by it.
        cron = next(o for o in self.bundle("live") if o["kind"] == "CronJob")
        self.assertLess(cron["spec"]["jobTemplate"]["spec"]["activeDeadlineSeconds"], job.CLAIM_SECONDS)
        self.assertLess(job.CLAIM_SECONDS + 15, 300)
        self.assertEqual("*/5 * * * *", cron["spec"]["schedule"])
        self.secret()["metadata"]["annotations"][job.CLAIM] = str(NOW)
        self.state.now = NOW + 298
        self.assertEqual("ok", self.run_job()["result"])
        self.assertEqual({MOTHER, CHILD, uid(4)}, set(self.mapping()["users"]))
        self.assertEqual({"kept": "yes"}, self.secret()["metadata"]["annotations"])

    def test_the_claim_is_taken_before_any_gateway_change_and_released_with_the_last_write(self):
        self.person(MOTHER, PARENTS)
        self.refuse(job.MAPPING_SECRET, 1)   # the owner changed the Secret between this run's read and its claim
        status = self.run_job(expect=1)
        self.assertEqual(("kubernetes_conflict", [], {}), (status["code"], self.state.gateway_calls, self.state.keys))
        self.run_job()
        self.assertNotIn(job.CLAIM, self.secret()["metadata"].get("annotations", {}))

    def test_a_concurrent_secret_change_is_refused_and_the_orphaned_key_is_cleaned_up(self):
        self.person(MOTHER, PARENTS)
        self.refuse(job.MAPPING_SECRET, 2)   # after the claim and the key, at the publish
        status = self.run_job(expect=1)
        self.assertEqual(("stopped", "kubernetes_conflict", {}), (status["result"], status["code"], self.mapping()["users"]))
        orphan = set(self.state.keys)
        self.assertEqual(1, len(orphan))    # issued, but no mapping names it and nobody holds its value
        self.state.now += job.CLAIM_SECONDS  # the stopped run left its mark
        self.assertEqual("ok", self.run_job()["result"])
        self.assertEqual({self.mapping()["users"][MOTHER]["key"]}, set(self.state.keys))
        self.assertFalse(orphan & set(self.state.keys))
        self.assertEqual({"service_key", "users"}, set(self.mapping()))

    def test_a_wrong_master_key_changes_nothing(self):
        self.person(MOTHER, PARENTS)
        (self.root / "master-key").write_text("sk-wrong-" + "w" * 40)
        status = self.run_job(expect=1)
        self.assertEqual(("partial", 1, {}), (status["result"], status["counts"]["failed"], self.mapping()["users"]))
        # Only the claim and its release reached the Secret; no key and no picture permission appeared.
        self.assertEqual(({}, {}), (self.secret()["metadata"]["annotations"], self.state.keys))
        self.assertEqual([], [call for call in self.puts("secrets") if call[2] == job.IMAGE_SECRET])
        (self.root / "master-key").write_text(MASTER)

    def test_a_gateway_answer_for_another_user_or_expiry_is_not_bound(self):
        self.person(MOTHER, PARENTS)
        reconciler = job.Reconciler(self.config, None, None, now=NOW)
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

    def test_the_database_is_opened_read_only_and_never_created_or_written(self):
        # A missing file in a writable directory: a read-write open would create it.
        (self.root / "empty").mkdir()
        with mock.patch.object(job.time, "sleep") as slept, self.assertRaises(job.Stop) as stopped:
            job.read_webui(self.root / "empty/webui.db")
        self.assertEqual(("webui_db_unavailable", 4), (stopped.exception.code, slept.call_count))
        self.assertEqual([], list((self.root / "empty").iterdir()))
        # The connection itself refuses writes before the first query.
        statements, connect = [], sqlite3.connect

        class Recorded:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, statement, *args):
                if statement.startswith("SELECT"):
                    with self.assertRaises(sqlite3.OperationalError):
                        self.connection.execute('DELETE FROM "user"')
                statements.append(statement)
                return self.connection.execute(statement, *args)

            def close(self):
                self.connection.close()
        Recorded.assertRaises = self.assertRaises
        self.person(MOTHER, PARENTS)
        with mock.patch.object(job.sqlite3, "connect", lambda *args, **kwargs: Recorded(connect(*args, **kwargs))):
            self.assertEqual({MOTHER: "user"}, job.read_webui(self.database)["users"])
        self.assertEqual(3, sum(statement.startswith("SELECT") for statement in statements))
        self.assertLess(statements.index("PRAGMA query_only = ON"), min(i for i, s in enumerate(statements) if s.startswith("SELECT")))
        self.assertEqual(MOTHER, self.webui.execute('SELECT id FROM "user"').fetchone()[0])

    def test_a_credential_never_follows_a_redirect_and_transport_errors_are_a_plain_failure(self):
        opener = job._opener()
        # urllib would follow both of these, and send the Authorization header along.
        self.assertEqual((302, None), job._call(opener, "POST", self.url + "/redirect", MASTER, {"keys": ["sk-" + "x" * 30]}))
        self.assertEqual((302, None), job._call(opener, "GET", self.url + "/redirect", MASTER))
        self.assertEqual((["/redirect"], 0), (self.posts(), self.state.probes))
        self.assertIsNone(job.Gateway(self.url + "/redirect#", MASTER).accepts("sk-" + "x" * 30))
        for error in (http.client.RemoteDisconnected("sk-in-the-error-text"), http.client.IncompleteRead(b"sk-partial"),
                      OSError("sk-socket"), ValueError("sk-json")):
            with mock.patch.object(opener, "open", side_effect=error):
                self.assertEqual((0, None), job._call(opener, "POST", self.url + "/key/generate", MASTER, {}))

    def test_an_unexpected_error_leaves_only_its_class_name(self):
        self.person(MOTHER, PARENTS)
        secret = "sk-in-an-exception-" + "e" * 24
        with mock.patch.object(job, "read_webui", side_effect=RuntimeError(f"could not use {secret} for {MOTHER}")):
            status = self.run_job(expect=1)
        self.assertEqual(("stopped", "internal_error", "RuntimeError"), (status["result"], status["code"], status["exception"]))
        self.assertEqual({"schema", "mode", "result", "code", "exception"}, set(status))
        published = self.state.objects[("configmaps", job.STATUS_CONFIGMAP)]["data"][job.STATUS_KEY]
        for text in (published, self.outputs[-1]):
            self.assertNotIn("could not use", text)
            self.assertNotIn(secret, text)

    def test_hand_provisioning_cannot_take_an_onboarding_alias(self):
        master = self.root / "master-key"
        master.chmod(0o600)
        request = {"user_id": MOTHER, "client": f"webui-{MOTHER[:8]}-a", "models": MODELS[:1], "rpm": 1, "tpm": 1,
                   "concurrency": 1, "days": 1}
        (self.root / "request.json").write_text(json.dumps(request))
        args = SimpleNamespace(master_file=master, request=self.root / "request.json", output=self.root / "out",
                               base_url="http://127.0.0.1:9", existing_user=False)
        with mock.patch.object(service, "admin_request") as sent, self.assertRaisesRegex(ValueError, "reserved for onboarding"):
            service.provision(args)
        sent.assert_not_called()
        self.assertFalse((self.root / "out").exists())


class DryRunTests(Fixture):
    mode = "dry-run"

    def test_dry_run_reports_the_plan_and_writes_nothing_anywhere(self):
        expiring, leaving = self.manual(days=5), self.manual()
        self.publish({"service_key": SERVICE_KEY, "users": {MOTHER: expiring, uid(99999999): leaving}})
        self.person(MOTHER, PARENTS)
        self.person(CHILD, FAMILY)
        before = repr(self.state.objects), dict(self.state.keys)
        # No master key is mounted in this mode.
        (self.root / "master-key").unlink()
        status = self.run_job()
        self.assertEqual(before, (repr(self.state.objects), self.state.keys))
        self.assertEqual(([], [], 0), (self.state.gateway_calls, self.puts(), self.state.probes))
        self.assertEqual({("GET", "secrets", job.MAPPING_SECRET), ("GET", "secrets", job.IMAGE_SECRET)}, set(self.state.kube_calls))
        self.assertEqual([{"user": "11111111", "action": "renew", "tier": "parents", "state": "planned"},
                          {"user": "22222222", "action": "provision", "tier": "family", "state": "planned"},
                          {"user": "99999999", "action": "revoke", "tier": None, "state": "planned"}], status["changes"])
        self.assertEqual(("dry-run", "ok", ["11111111", "22222222"], "not_checked"),
                         (status["mode"], status["result"], status["homes_needed"], status["liveness"]))

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

    def test_the_site_file_sets_the_policies_and_the_administrator_rule(self):
        self.assertEqual({**service.KEY_LIMITS, "days": 90}, onboarding.BOUNDS)
        # Defaults: parents at the adult bounds; family lower, without pictures, and two requests at once
        # (Open WebUI's follow-up, title and tag calls share the key with the next message).
        self.assertEqual({"parents": {"rpm": 60, "tpm": 2000000, "concurrency": 2, "days": 90, "images": True},
                          "family": {"rpm": 30, "tpm": 1000000, "concurrency": 2, "days": 90, "images": False}},
                         onboarding.policies({}))
        site = self.root / "site-policies.json"
        site.write_text(json.dumps({**SITE, "key_onboarding": "live", "admin_policy": "unmanaged",
                                    "key_policies": {"family": {"rpm": 10, "concurrency": 1, "days": 45}, "parents": {"tpm": 500000}}}))
        output = self.root / "render-policies"
        service.render(SimpleNamespace(site=site, output=output, mode="shared", experience="assistant-memory",
                                       serving_profile=PROFILE, sandbox_config=self.root / "sandbox.json"))
        rendered = next(o for o in service.load_objects((output / "onboarding.yaml").read_text())
                        if o["kind"] == "ConfigMap" and o["metadata"]["name"] == "key-onboarding")
        path = self.root / "config-policies.json"
        path.write_text(rendered["data"]["config.json"])
        config = job.read_config(path)
        self.assertEqual("unmanaged", config["admin_policy"])
        self.assertEqual({"rpm": 10, "tpm": 1000000, "concurrency": 1, "days": 45, "images": False},
                         {k: v for k, v in config["policies"]["family"].items() if k != "models"})
        self.assertEqual({"rpm": 60, "tpm": 500000, "concurrency": 2, "days": 90, "images": True},
                         {k: v for k, v in config["policies"]["parents"].items() if k != "models"})
        self.assertEqual("parents", json.loads(self.named("live", "ConfigMap")["data"]["config.json"])["admin_policy"])
        # A wrong value is refused at the site file, whether or not onboarding is on.
        for wrong in ({"key_policies": {"children": {}}}, {"key_policies": {"family": {"models": ["x"]}}},
                      {"key_policies": {"family": {"rpm": 61}}}, {"key_policies": {"family": {"tpm": 0}}},
                      {"key_policies": {"parents": {"concurrency": 3}}}, {"key_policies": {"parents": {"days": 91}}},
                      {"key_policies": {"parents": {"days": 30}}}, {"key_policies": {"family": {"images": "no"}}},
                      {"key_policies": {"family": {"rpm": 1.5}}}, {"key_policies": []}, {"key_policies": {"family": []}},
                      {"admin_policy": "family"}):
            site.write_text(json.dumps({**SITE, **wrong}))
            with self.assertRaises(ValueError, msg=wrong):
                service.site_values(site)

    def test_finished_jobs_are_removed_so_that_failed_pods_mean_recent_failures(self):
        spec = self.named("live", "CronJob")["spec"]["jobTemplate"]["spec"]
        self.assertEqual(1200, spec["ttlSecondsAfterFinished"])
        rules = service.yaml.safe_load((ROOT / "infrastructure/service/alerts.yaml").read_text())["groups"][0]["rules"]
        alert = next(rule for rule in rules if rule["alert"] == "WorkstationKeyOnboardingFailing")
        self.assertIn('pod=~"key-onboarding-.*"', alert["expr"])
        self.assertIn('phase="Failed"', alert["expr"])
        # The metric the rule reads is one kube-state-metrics is allowed to export here.
        self.assertIn("^kube_pod_status_phase$", (ROOT / "infrastructure/observability/host-metrics.yaml").read_text())

    def test_a_claim_that_cannot_be_shared_or_an_unpinned_image_is_refused(self):
        webui = {"kind": "Deployment", "metadata": {"name": "open-webui"}, "spec": {"template": {"spec": {
            "containers": [{"image": "registry.example.test/open-webui@sha256:" + "c" * 64,
                            "env": [{"name": "ENABLE_SIGNUP", "value": "false"}]}],
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
        # Membership issues keys by itself, so open sign-up is refused.
        webui["spec"]["template"]["spec"]["containers"][0]["image"] = "registry.example.test/open-webui@sha256:" + "c" * 64
        self.assertTrue(onboarding.render("dry-run", sandbox, [webui, claim], MODELS, ROOT))
        for env in ([{"name": "ENABLE_SIGNUP", "value": "true"}], []):
            webui["spec"]["template"]["spec"]["containers"][0]["env"] = env
            with self.assertRaisesRegex(ValueError, "sign-up closed"):
                onboarding.render("dry-run", sandbox, [webui, claim], MODELS, ROOT)
        self.assertEqual([], onboarding.render("off", sandbox, [webui, claim], MODELS, ROOT))


if __name__ == "__main__":
    unittest.main()
