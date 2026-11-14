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
import sandbox_homes
import service

module = importlib.util.spec_from_file_location("sandbox_control", ROOT / "infrastructure/ai-stack/sandbox/control.py")
control = importlib.util.module_from_spec(module)
module.loader.exec_module(control)
CONFIG = {"image": "registry.example.test/sandbox@sha256:" + "a" * 64,
          "egress_image": "registry.example.test/sandbox-egress@sha256:" + "b" * 64,
          "parents_group_id": "parents-fixture", "family_group_id": "family-fixture",
          "family_enabled": True, "group_resolution": "most-permissive",
          "egress_service_ips": {"family": "10.43.0.201", "parents": "10.43.0.202"},
          "api_server_cidr": "192.168.77.9/32", "api_server_port": 6443}
HOMES = {"users": [{"webui_user_id": "alice", "name": "alice", "quota": "20G", "group": "Parents"},
                   {"webui_user_id": "kid", "name": "kid", "quota": "5G", "group": "Family"}]}
SITE = {"context": "fixture", "node": "fixture", "node_address": "192.168.77.9",
        "management_cidr": "192.168.77.0/24", "client_cidrs": ["192.168.77.0/24"],
        "webui_host": "chat.fixture.test", "api_host": "api.fixture.test", "tls_secret": "fixture-tls"}
PROFILE = ROOT / "infrastructure/ai-stack/serving/qwen3.8-27b-mxfp4-tp2.json"
# The node on 14 Nov 2026: 42 CPUs and 52,565,368 Ki allocatable; the cluster's own
# pods (K3s services, GPU operator, NFS driver, host collectors) by their requests.
NODE = {"schema_version": 1, "status": "offline-plan-not-applied", "allocatable_logical_cpus": 42,
        "gpu_count": 2, "memory": {"total_mib": 63621, "allocatable_mib": 51333}}


def node_pod(namespace, cpu, memory, phase="Running"):
    return {"metadata": {"namespace": namespace}, "status": {"phase": phase},
            "spec": {"containers": [{"resources": {"requests": {"cpu": cpu, "memory": memory}}}]}}


NODE_PODS = {"kind": "PodList", "items": [
    node_pod("kube-system", "270m", "280Mi"), node_pod("kube-amd-gpu", "215m", "576Mi"),
    node_pod("kube-amd-gpu", "500m", "512M"), node_pod("workstation-observability-host", "250m", "1216Mi"),
    # Already planned, or finished: never counted twice.
    node_pod("ai-home-lab", "16", "14Gi"), node_pod("kube-system", "4", "8Gi", "Succeeded")]}


def loaded(homes=True):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "homes.json").write_text(json.dumps(HOMES))
        (root / "sandbox.json").write_text(json.dumps({**CONFIG, **({"homes": str(root / "homes.json")} if homes else {})}))
        return sandbox.read_config(root / "sandbox.json")


def composition(mode, experience):
    objects = service.kustomize(ROOT / "apps/overlays" / (experience + ("-shared" if mode == "shared" else "")))
    contract = service.serving_contract(PROFILE, None, None, service.DEFAULT_OUTPUT_TOKENS, mode=mode)
    return service.apply_contract(objects, contract, mode, experience)[0]


class RenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = loaded()
        cls.objects = sandbox.render(cls.config, ROOT)
        cls.telemetry = service.kustomize(ROOT / "infrastructure/observability")

    def named(self, kind, name):
        return next(o for o in self.objects if o["kind"] == kind and o["metadata"]["name"] == name)

    def test_render_flag_off_and_on_and_shared_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "site.json").write_text(json.dumps(SITE))
            (root / "homes.json").write_text(json.dumps(HOMES))
            (root / "sandbox.json").write_text(json.dumps({**CONFIG, "homes": str(root / "homes.json")}))
            for enabled in (False, True):
                out = root / str(enabled)
                args = SimpleNamespace(site=root / "site.json", output=out, mode="large",
                    experience="assistant-memory", serving_profile=PROFILE,
                    sandbox_config=root / "sandbox.json" if enabled else None)
                service.render(args)
                workloads = service.load_objects((out / "workloads.yaml").read_text())
                webui = next(o for o in workloads if o["kind"] == "Deployment" and o["metadata"]["name"] == "open-webui")
                env = {e["name"]: e.get("value") for e in webui["spec"]["template"]["spec"]["containers"][0]["env"]}
                self.assertEqual(bool(json.loads(env["TERMINAL_SERVER_CONNECTIONS"])), enabled)
                self.assertIn("TERMINAL_SERVER_CONNECTIONS", env["WEBUI_PINNED_ENV"].split(","))
                self.assertEqual(bool((out / "sandbox.yaml").read_text()), enabled)
                evidence = json.loads((out / "evidence.json").read_text())["sandbox"]
                self.assertEqual((enabled, enabled, 2 if enabled else 0),
                                 (evidence["enabled"], evidence["family_enabled"], evidence["persistent_homes"]))
                if enabled:
                    args.mode, args.output = "shared", root / "shared"
                    with self.assertRaisesRegex(ValueError, "does not fit shared mode"):
                        service.render(args)
                    self.assertFalse(args.output.exists())

    def test_capacity_by_mode_matches_live_admission_and_shared_memory_refuses_the_pool(self):
        """The numbers behind the shared-mode decision (docs/ASSISTANT.md)."""
        live_telemetry = [o for o in self.telemetry if o["metadata"]["name"] in {"alloy", "kube-state-metrics"}]
        table = {}
        for mode in ("large", "shared"):
            for experience in ("assistant", "assistant-memory"):
                objects = composition(mode, experience)
                for pool in (False, True):
                    result = service.capacity(NODE, objects + (self.objects if pool else []), {"reserve_mib": 6656},
                                              live_telemetry, mode, node_pods=NODE_PODS)
                    table[mode, experience, pool] = (result["fits"], result["remaining_cpus"], result["remaining_mib"])
                    print("capacity", mode, experience, "with pool" if pool else "no pool", *table[mode, experience, pool],
                          "limits", result["limits"])
        pool = next(r for r in result["rows"] if r["name"] == "assistant-sandbox-pool")
        self.assertEqual((1.95, 4480), (pool["cpu"], pool["memory_mib"]))
        platform = next(r for r in result["rows"] if r["name"] == "platform")
        self.assertEqual((1.235, 2561), (platform["cpu"], platform["memory_mib"]))
        # Family mode as it runs today (memory on): admitted, as the live session admission
        # found on 14 Nov (37.9 of 42 CPUs, 49.9 of 51.3 GiB requested), with 1.3 GiB left.
        self.assertEqual((True, 4.015, 1348), table["shared", "assistant-memory", False])
        # The pool needs 4.4 GiB of requests: it does not fit there. Nothing is trimmed to make it.
        self.assertEqual((False, 2.065, -3132), table["shared", "assistant-memory", True])
        # Without the memory services it would, and coder mode has room either way.
        self.assertEqual((True, 3.565, 1348), table["shared", "assistant", True])
        self.assertEqual((True, 18.065, 17348), table["large", "assistant-memory", True])
        self.assertEqual((True, 19.565, 21828), table["large", "assistant", True])
        # Limits are reported, not hidden: family mode's burst ceilings sum past the node.
        shared = service.capacity(NODE, composition("shared", "assistant-memory"), {"reserve_mib": 6656},
                                  live_telemetry, "shared", node_pods=NODE_PODS)
        self.assertEqual({"cpu": 46.0, "memory_mib": 63744, "over_node_cpus": 4.0, "over_node_mib": 12411}, shared["limits"])
        self.assertEqual(("planned-limits-overcommitted", -4604), (shared["status"], shared["remaining_mib_after_telemetry_reserve"]))
        sglang = next(r for r in shared["rows"] if r["name"] == "sglang")
        # The 16 GiB memory-backed /dev/shm sits inside the pod's 14 GiB, not beside it.
        self.assertEqual((14336, 14336, 14336), (sglang["memory_mib"], sglang["limit_mib"], sglang["shared_memory_mib"]))
        # Without live pod evidence the plan says what it could not count.
        offline = service.capacity(NODE, composition("large", "assistant"), {"reserve_mib": 6656}, live_telemetry, "large")
        self.assertTrue(any("--node-pods" in note for note in offline["limitations"]))
        self.assertEqual("planned-not-qualified", offline["status"])
        self.assertFalse(any(r["name"] == "platform" for r in offline["rows"]))
        self.assertFalse(service.capacity(NODE, composition("large", "assistant") + self.objects, {"reserve_mib": 6656},
                                          live_telemetry, "large", vm_mib=30000)["fits"])
        with self.assertRaises(ValueError):
            service.capacity(NODE, composition("large", "assistant"), {"reserve_mib": 6656}, live_telemetry, "large",
                             node_pods={"kind": "PodList", "items": [node_pod("kube-system", "1", "1Pi")]})

    def test_security_idle_no_credentials_or_gpu_in_workspace(self):
        policies = sandbox.policies(self.config)
        self.assertEqual({"sandbox-family", "sandbox-parents", "home-alice", "home-kid"}, set(policies))
        for name, policy in policies.items():
            spec = policy["podTemplate"]["spec"]
            self.assertFalse(spec["automountServiceAccountToken"])
            self.assertFalse(spec["enableServiceLinks"])
            self.assertEqual("", policy["storage"])
            self.assertEqual((15, 120), (policy["idle_timeout_minutes"], policy["max_lifetime_minutes"]))
            self.assertFalse(any(k in spec for k in ("hostNetwork", "hostPID", "hostIPC", "resourceClaims")))
            security = policy["container_security_context"]
            self.assertTrue(security["readOnlyRootFilesystem"])
            self.assertTrue(security["runAsNonRoot"])
            self.assertEqual((1000, 1000), (security["runAsUser"], security["runAsGroup"]))
            self.assertFalse(security["allowPrivilegeEscalation"])
            self.assertEqual(["ALL"], security["capabilities"]["drop"])
            self.assertEqual("RuntimeDefault", security["seccompProfile"]["type"])
            self.assertEqual((1000, "OnRootMismatch"), (policy["pod_security_context"]["fsGroup"],
                                                        policy["pod_security_context"]["fsGroupChangePolicy"]))
            container = spec["containers"][0]
            self.assertEqual(container["resources"]["requests"], container["resources"]["limits"])
            volumes = {v["name"]: v for v in spec["volumes"]}
            self.assertEqual({"/home/user": "home", "/tmp": "scratch"},
                             {m["mountPath"]: m["name"] for m in container["volumeMounts"]})
            # Scratch always goes with the pod; only a mapped account's home is a claim, and its own.
            self.assertEqual({"sizeLimit": "2Gi"}, volumes["scratch"]["emptyDir"])
            if name.startswith("home-"):
                self.assertEqual({"claimName": "sandbox-home-" + name.removeprefix("home-")},
                                 volumes["home"]["persistentVolumeClaim"])
            else:
                self.assertIn("emptyDir", volumes["home"])
            self.assertFalse(any("hostPath" in v or "nfs" in v or "secret" in v for v in spec["volumes"]))
        for deployment in (o for o in self.objects if o["kind"] == "Deployment"):
            self.assertEqual(0, deployment["spec"]["replicas"])
        role = next(o for o in self.objects if o["kind"] == "Role")
        # The controller mounts existing claims by name; it cannot create, read or delete one.
        self.assertEqual(["pods", "services", "secrets"], role["rules"][0]["resources"])
        quota = self.named("ResourceQuota", "sandbox-pool")["spec"]["hard"]
        self.assertEqual(("5", "1950m", "4480Mi", "32"), (quota["pods"], quota["limits.cpu"], quota["limits.memory"],
                                                         quota["persistentvolumeclaims"]))

    def test_each_group_gets_its_own_list_and_proxy_and_nothing_else(self):
        lists = sandbox.egress_lists()
        self.assertEqual({"Family": "family", "Parents": "parents", "Admin": "parents"}, lists["groups"])
        family, parents = set(lists["lists"]["family"]), set(lists["lists"]["parents"])
        self.assertLess(family, parents)
        self.assertLessEqual({"pypi.org", "files.pythonhosted.org", "registry.npmjs.org", "docs.python.org",
                              "developer.mozilla.org"}, family)
        # General hosting, search, chat and video are not on the kids' list.
        self.assertFalse({"github.com", "stackoverflow.com", "en.wikipedia.org", "scratch.mit.edu"} & family)
        self.assertFalse(any(d.startswith("*") for d in family))
        acl = service.yaml.safe_load(self.named("ConfigMap", "sandbox-egress")["data"]["acl.yaml"])
        self.assertEqual({"family": sorted(family), "parents": sorted(parents)},
                         {s["name"]: sorted(s["allowed_domains"]) for s in acl["services"]})
        self.assertTrue(all(s["action"] == "enforce" for s in acl["services"]))
        # A request with no known group falls to a default that allows nothing.
        self.assertEqual({"name": "default", "project": "sandbox", "action": "enforce"}, acl["default"])
        policies = sandbox.policies(self.config)
        for policy, egress in (("sandbox-family", "family"), ("home-kid", "family"),
                               ("sandbox-parents", "parents"), ("home-alice", "parents")):
            env, proxy = policies[policy]["env"], f'http://{CONFIG["egress_service_ips"][egress]}:4750'
            self.assertEqual({proxy}, {env[k] for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")})
            self.assertEqual(egress, policies[policy]["podTemplate"]["metadata"]["labels"][sandbox.EGRESS_LABEL])
            self.assertEqual("localhost,127.0.0.1", env["NO_PROXY"])
        for name in ("family", "parents"):
            deployment = self.named("Deployment", "sandbox-egress-" + name)
            pod = deployment["spec"]["template"]["spec"]
            container = pod["containers"][0]
            self.assertEqual(CONFIG["egress_image"], container["image"])
            self.assertEqual(["--group", name, "--acl", "/config/acl.yaml", "--deny-ranges",
                              "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,169.254.0.0/16,100.64.0.0/10,127.0.0.0/8"],
                             container["args"])
            self.assertFalse(pod["automountServiceAccountToken"])
            self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
            self.assertEqual(["ALL"], container["securityContext"]["capabilities"]["drop"])
            self.assertEqual(container["resources"]["requests"], container["resources"]["limits"])
            service_ = self.named("Service", "sandbox-egress-" + name)
            self.assertEqual(CONFIG["egress_service_ips"][name], service_["spec"]["clusterIP"])

    def test_network_union_workspace_reaches_only_its_own_proxy(self):
        objects = self.objects + service.kustomize(ROOT / "apps/overlays/assistant-memory") + self.telemetry
        def rules(namespace, labels, direction):
            return [rule for o in objects if o["kind"] == "NetworkPolicy"
                    and o["metadata"].get("namespace") == namespace and direction in o["spec"]["policyTypes"]
                    and all(labels.get(k) == v for k, v in o["spec"]["podSelector"].get("matchLabels", {}).items())
                    for rule in o["spec"].get(direction.lower(), [])]
        controller = sandbox.peer(sandbox.NAMESPACE, sandbox.CONTROL)
        for name, other in (("family", "parents"), ("parents", "family")):
            workspace = {sandbox.LABEL: sandbox.WORKSPACE, sandbox.EGRESS_LABEL: name}
            # One rule in the union of every policy that selects the pod: its proxy, its port.
            # No DNS, node, service, LAN, peer or Internet rule; {} would allow them all.
            self.assertEqual([{"to": [sandbox.peer(sandbox.NAMESPACE, sandbox.EGRESS, **{sandbox.EGRESS_LABEL: name})],
                               "ports": [{"port": 4750, "protocol": "TCP"}]}],
                             rules(sandbox.NAMESPACE, workspace, "Egress"))
            self.assertEqual([{"from": [controller], "ports": [{"port": 8000, "protocol": "TCP"}]}],
                             rules(sandbox.NAMESPACE, workspace, "Ingress"))
            proxy = {sandbox.LABEL: sandbox.EGRESS, sandbox.EGRESS_LABEL: name}
            ingress = rules(sandbox.NAMESPACE, proxy, "Ingress")
            self.assertEqual([sandbox.peer(sandbox.NAMESPACE, sandbox.WORKSPACE, **{sandbox.EGRESS_LABEL: name})],
                             ingress[0]["from"])
            self.assertNotIn(other, json.dumps(ingress))
            egress = rules(sandbox.NAMESPACE, proxy, "Egress")
            blocks = [p["ipBlock"] for rule in egress for p in rule["to"] if "ipBlock" in p]
            # Public addresses on the web ports only: the LAN, the cluster and link-local are excepted.
            self.assertEqual([{"cidr": "0.0.0.0/0", "except": list(sandbox.PRIVATE_CIDRS)}], blocks)
            self.assertLessEqual({"192.168.0.0/16", "10.0.0.0/8", "169.254.0.0/16"}, set(blocks[0]["except"]))
            web = next(rule for rule in egress if "ipBlock" in rule["to"][0])
            self.assertEqual([80, 443], [port["port"] for port in web["ports"]])
            self.assertEqual({53}, {port["port"] for rule in egress if rule is not web for port in rule["ports"]})
        # A workspace pod that somehow lacks a list label keeps no egress at all.
        self.assertEqual([], rules(sandbox.NAMESPACE, {sandbox.LABEL: sandbox.WORKSPACE}, "Egress"))
        self.assertEqual([], rules(sandbox.NAMESPACE, {"anything": "else"}, "Egress"))

    def test_exact_access_grants_family_switch_and_config_validation(self):
        for enabled in (False, True):
            objects = [{"kind": "Deployment", "metadata": {"name": "open-webui"},
                        "spec": {"template": {"spec": {"containers": [{}]}}}}]
            sandbox.configure_webui(objects, {**CONFIG, "family_enabled": enabled})
            connection = json.loads(objects[0]["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"])[0]
            self.assertEqual("session", connection["auth_type"])
            self.assertFalse(connection["config"]["contexts"]["automation"])
            self.assertEqual(["parents-fixture"] + (["family-fixture"] if enabled else []),
                             [g["principal_id"] for g in connection["config"]["access_grants"]])
        gate = sandbox.gate_config(self.config)
        self.assertEqual({"alice": {"name": "alice", "group": "Parents"}, "kid": {"name": "kid", "group": "Family"}},
                         gate["users"])
        self.assertFalse({"image", "egress_image", "api_server_cidr", "quota"} & set(json.dumps(gate).replace('"', " ").split()))
        self.assertEqual([], loaded(homes=False)["users"])
        # Every mapped account's claim is one the storage render produces, in this namespace.
        claims = {o["metadata"]["name"]: o for o in sandbox_homes.render(self.config["users"]) if o["kind"] == "PersistentVolumeClaim"}
        self.assertEqual({"sandbox-home-alice", "sandbox-home-kid"}, set(claims))
        self.assertEqual({sandbox.NAMESPACE}, {o["metadata"]["namespace"] for o in claims.values()})
        self.assertEqual(sandbox_homes.OWNER, sandbox.policy(self.config, "family")["pod_security_context"]["runAsUser"])
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            missing = {k: v for k, v in CONFIG.items() if k != "family_enabled"}
            unresolved = {k: v for k, v in CONFIG.items() if k != "group_resolution"}
            for config in (missing, unresolved, *({**CONFIG, **change} for change in (
                    {"image": "example:latest"}, {"egress_image": "example:latest"}, {"api_server_cidr": "192.168.0.0/16"},
                    {"family_enabled": "false"}, {"parents_group_id": "family-fixture"}, {"egress": "all"},
                    {"group_resolution": "family-wins"}, {"egress_service_ips": {"family": "10.43.0.201"}},
                    {"egress_service_ips": {"family": "10.43.0.201", "parents": "10.43.0.201"}},
                    {"egress_service_ips": {"family": "10.43.0.201", "parents": "8.8.8.8"}}, {"homes": "homes.json"}))):
                path.write_text(json.dumps(config))
                with self.assertRaises(ValueError, msg=config):
                    sandbox.read_config(path)
            homes = Path(d) / "homes.json"
            entry = HOMES["users"][0]
            for users in ([{**entry, "name": "Alice"}], [{**entry, "name": "a/../b"}], [{**entry, "group": "Guests"}],
                          [{**entry, "quota": "lots"}], [entry, {**entry, "webui_user_id": "other"}],
                          [entry, {**entry, "name": "other"}], [{k: v for k, v in entry.items() if k != "group"}],
                          [{**entry, "path": "/mnt"}]):
                homes.write_text(json.dumps({"users": users}))
                with self.assertRaises(ValueError, msg=users):
                    sandbox_homes.read_users(homes)


def gate_config(**changes):
    return {**sandbox.gate_config({**CONFIG, "users": HOMES["users"]}), **changes}


class GroupTests(unittest.TestCase):
    """Which reviewed group, and so which egress list and home, each kind of account gets."""
    PARENTS, FAMILY = {"id": "parents-fixture"}, {"id": "family-fixture"}

    def resolve(self, uid, role, groups, **changes):
        config = gate_config(**changes)
        group = control.group_for({"id": uid, "role": role}, groups, config)
        return group, control.policy_for(uid, group, config) if group else None

    def test_default_resolution_most_permissive(self):
        # The owner's daily account is in both groups: it acts as Parents, not as a child.
        self.assertEqual(("Parents", "sandbox-parents"), self.resolve("owner", "user", [self.PARENTS, self.FAMILY]))
        self.assertEqual(("Parents", "sandbox-parents"), self.resolve("parent", "user", [self.PARENTS]))
        self.assertEqual(("Family", "sandbox-family"), self.resolve("child", "user", [self.FAMILY]))
        # The admin account is in no group; the role alone decides.
        self.assertEqual(("Admin", "sandbox-parents"), self.resolve("admin", "admin", []))
        self.assertEqual(("Admin", "sandbox-parents"), self.resolve("admin", "admin", [self.FAMILY]))
        self.assertEqual((None, None), self.resolve("guest", "user", [{"id": "other"}]))
        self.assertEqual((None, None), self.resolve("pending", "pending", [self.PARENTS]))

    def test_most_restrictive_and_the_family_switch(self):
        strict = {"group_resolution": "most-restrictive"}
        self.assertEqual(("Family", "sandbox-family"), self.resolve("owner", "user", [self.PARENTS, self.FAMILY], **strict))
        self.assertEqual(("Parents", "sandbox-parents"), self.resolve("parent", "user", [self.PARENTS], **strict))
        off = {"family_enabled": False}
        self.assertEqual((None, None), self.resolve("child", "user", [self.FAMILY], **off))
        # With the kids switch off a Family membership neither grants nor removes anything.
        self.assertEqual(("Parents", "sandbox-parents"), self.resolve("owner", "user", [self.PARENTS, self.FAMILY], **off))
        self.assertEqual(("Parents", "sandbox-parents"),
                         self.resolve("owner", "user", [self.PARENTS, self.FAMILY], **off, **strict))

    def test_mapping_names_the_group_but_cannot_grant_one(self):
        both = [self.PARENTS, self.FAMILY]
        # alice is mapped as Parents, kid as Family: each gets its own home on its group's list.
        self.assertEqual(("Parents", "home-alice"), self.resolve("alice", "user", both))
        self.assertEqual(("Parents", "home-alice"), self.resolve("alice", "user", both, group_resolution="most-restrictive"))
        self.assertEqual(("Family", "home-kid"), self.resolve("kid", "user", both))
        self.assertEqual(("Family", "home-kid"), self.resolve("kid", "user", [self.FAMILY]))
        # A mapping that names a group the account does not hold denies; it never widens.
        self.assertEqual((None, None), self.resolve("alice", "user", [self.FAMILY]))
        self.assertEqual((None, None), self.resolve("kid", "user", [self.PARENTS]))
        self.assertEqual((None, None), self.resolve("kid", "user", both, family_enabled=False))
        self.assertEqual((None, None), self.resolve("alice", "admin", []))


class GateTests(unittest.IsolatedAsyncioTestCase):
    async def test_identity_forgery_roles_group_revocation_and_no_content_metrics(self):
        forwarded = []
        role, groups, uid, failure, malformed = "user", [{"id": "parents-fixture"}], "parent", False, False
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
            gate = control.Gate(upstream, gate_config(), b'{"paths":{}}', verifier)
            with patch.dict(os.environ, {"TERMINALS_OPEN_WEBUI_URL": "http://fixture"}):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gate), base_url="http://gate") as client:
                    headers = {"Authorization": "Bearer fixture", "X-User-Id": "parent", "X-Terminal-Context-Id": "forged",
                               "X-Terminal-Policy-Id": "home-alice"}
                    self.assertEqual(200, (await client.post("/p/sandbox/execute", headers=headers, content="PRIVATE-CODE")).status_code)
                    self.assertNotIn(b"x-terminal-context-id", dict(forwarded[0]["headers"]))
                    self.assertNotIn(b"x-terminal-policy-id", dict(forwarded[0]["headers"]))
                    # The gate chose the policy from the account's group; the caller asked for "sandbox".
                    self.assertEqual(("/p/sandbox-parents/execute", b"/p/sandbox-parents/execute"),
                                     (forwarded[0]["path"], forwarded[0]["raw_path"]))
                    # Another account's home policy cannot be named in the path.
                    for path in ("/p/home-alice/execute", "/p/sandbox-family/execute", "/p/sandbox/../home-alice/execute"):
                        self.assertEqual(403, (await client.post(path, headers=headers)).status_code)
                    for name, value in (("Authorization", "Bearer other-fixture"), ("X-User-Id", "bob")):
                        duplicate = list(headers.items()) + [(name, value)]
                        self.assertEqual(403, (await client.post("/p/sandbox/execute", headers=duplicate)).status_code)
                    malformed = True
                    self.assertEqual(403, (await client.post("/p/sandbox/execute", headers=headers)).status_code)
                    malformed = False
                    uid = "bob"
                    self.assertEqual(403, (await client.post("/p/sandbox/execute", headers=headers)).status_code)
                    # A child: the Family list, counted as Family; removed from the group, refused.
                    uid, groups = "parent", [{"id": "family-fixture"}]
                    self.assertEqual(200, (await client.post("/p/sandbox/execute", headers=headers)).status_code)
                    self.assertEqual("/p/sandbox-family/execute", forwarded[-1]["path"])
                    groups = []
                    self.assertEqual(403, (await client.post("/p/sandbox/execute", headers=headers)).status_code)
                    # A mapped account lands in its own home policy.
                    uid, groups = "alice", [{"id": "parents-fixture"}, {"id": "family-fixture"}]
                    mapped = {**headers, "X-User-Id": "alice"}
                    self.assertEqual(200, (await client.get("/p/sandbox/files/cwd", headers=mapped)).status_code)
                    self.assertEqual("/p/home-alice/files/cwd", forwarded[-1]["path"])
                    uid, groups, role = "parent", [], "admin"
                    self.assertEqual(200, (await client.post("/p/sandbox/execute", headers=headers)).status_code)
                    self.assertEqual("/p/sandbox-parents/execute", forwarded[-1]["path"])
                    role, groups = "pending", []
                    self.assertEqual(403, (await client.post("/p/sandbox/execute", headers=headers)).status_code)
                    failure = True
                    self.assertEqual(403, (await client.get("/p/sandbox/files/cwd", headers=headers)).status_code)
                    for path in ("/execute", "/api/v1/policies", "/p/other/execute"):
                        self.assertEqual(403, (await client.post(path, headers=headers)).status_code)
                    self.assertEqual(200, (await client.get("/p/sandbox/openapi.json")).status_code)
                    metrics = (await client.get("/metrics")).text
                    self.assertIn('group="Parents"} 1', metrics)
                    self.assertIn('group="Family"} 1', metrics)
                    self.assertIn('group="Admin"} 1', metrics)
                    self.assertNotIn("alice", metrics)
                    self.assertNotIn("PRIVATE-CODE", metrics)
                    self.assertEqual(4, len(forwarded))


if __name__ == "__main__":
    unittest.main()
