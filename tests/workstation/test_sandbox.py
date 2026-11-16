"""Native sandbox render, capacity, network-policy union and group boundaries."""
import asyncio
import http.server
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.request

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib/workstation"))
import sandbox
import sandbox_homes
import service
from telemetry import quantity

module = importlib.util.spec_from_file_location("sandbox_control", ROOT / "infrastructure/ai-stack/sandbox/control.py")
control = importlib.util.module_from_spec(module)
module.loader.exec_module(control)
module = importlib.util.spec_from_file_location("sandbox_terminal", ROOT / "infrastructure/ai-stack/sandbox/terminal.py")
terminal = importlib.util.module_from_spec(module)
module.loader.exec_module(terminal)
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

    def test_render_flag_off_and_on_in_both_rendered_modes(self):
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
                    # Family (shared) mode renders the same pool; the older compositions still refuse it.
                    args.mode, args.output = "shared", root / "shared"
                    service.render(args)
                    self.assertTrue((args.output / "sandbox.yaml").read_text())
                    args.mode, args.output = "split", root / "split"
                    with self.assertRaisesRegex(ValueError, "large or shared mode"):
                        service.render(args)
                    self.assertFalse(args.output.exists())

    def test_capacity_by_mode_matches_live_admission_and_the_pool_fits_on_requests(self):
        """The numbers behind the capacity table (docs/ASSISTANT.md)."""
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
        # Held by the scheduler: requests. What the pool may burst to: limits.
        self.assertEqual((0.7, 832, 5.0, 8960), (pool["cpu"], pool["memory_mib"], pool["limit_cpu"], pool["limit_mib"]))
        platform = next(r for r in result["rows"] if r["name"] == "platform")
        self.assertEqual((1.235, 2561), (platform["cpu"], platform["memory_mib"]))
        # Family mode as it runs today (memory on): admitted, as the live session admission
        # found on 14 Nov (37.9 of 42 CPUs, 49.9 of 51.3 GiB requested, comfyui-gpu1 then at 12 GiB); with
        # comfyui-gpu1 at its measured 9 GiB, 4.3 GiB are left.
        self.assertEqual((True, 4.015, 4420), table["shared", "assistant-memory", False])
        # The pool requests 0.7 CPUs and 832 MiB at measured use: it is admitted there with 3,588 MiB
        # unrequested. No other workload's budget changed.
        self.assertEqual((True, 3.315, 3588), table["shared", "assistant-memory", True])
        self.assertEqual((True, 4.815, 8068), table["shared", "assistant", True])
        self.assertEqual((True, 19.315, 20996), table["large", "assistant-memory", True])
        self.assertEqual((True, 20.815, 25476), table["large", "assistant", True])
        # Its limits (5 CPUs, 8,960 MiB) are overcommit, and are reported as such.
        with_pool = service.capacity(NODE, composition("shared", "assistant-memory") + self.objects, {"reserve_mib": 6656},
                                     live_telemetry, "shared", node_pods=NODE_PODS)
        self.assertEqual(("planned-limits-overcommitted", {"cpu": 51.0, "memory_mib": 69632, "over_node_cpus": 9.0,
                                                           "over_node_mib": 18299}), (with_pool["status"], with_pool["limits"]))
        # Limits are reported, not hidden: family mode's burst ceilings sum past the node.
        shared = service.capacity(NODE, composition("shared", "assistant-memory"), {"reserve_mib": 6656},
                                  live_telemetry, "shared", node_pods=NODE_PODS)
        self.assertEqual({"cpu": 46.0, "memory_mib": 60672, "over_node_cpus": 4.0, "over_node_mib": 9339}, shared["limits"])
        self.assertEqual(("planned-limits-overcommitted", -1532), (shared["status"], shared["remaining_mib_after_telemetry_reserve"]))
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

    def test_node_reserves_the_sandbox_uid_through_sysusers(self):
        # The runtime package's sysusers drop-in must reserve exactly SANDBOX_UID, never a login.
        conf = (ROOT / "templates/workstation/sandbox/workstation-sandbox.sysusers").read_text().splitlines()
        entries = [line.split() for line in conf if line and not line.startswith("#")]
        self.assertEqual(["g", "sandbox", str(sandbox.SANDBOX_UID)], entries[0])
        self.assertEqual(["u", "sandbox", f"{sandbox.SANDBOX_UID}:{sandbox.SANDBOX_UID}"], entries[1][:3])
        self.assertEqual("/usr/bin/nologin", entries[1][-1])
        self.assertNotEqual(1000, sandbox.SANDBOX_UID)

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
            # The dedicated non-login sandbox account; never 1000, the owner's login.
            self.assertEqual((2002, 2002), (security["runAsUser"], security["runAsGroup"]))
            self.assertFalse(security["allowPrivilegeEscalation"])
            self.assertEqual(["ALL"], security["capabilities"]["drop"])
            self.assertEqual("RuntimeDefault", security["seccompProfile"]["type"])
            self.assertEqual((2002, "OnRootMismatch"), (policy["pod_security_context"]["fsGroup"],
                                                        policy["pod_security_context"]["fsGroupChangePolicy"]))
            container = spec["containers"][0]
            # Burstable: requested at measured use, limited with headroom; storage stays bounded.
            self.assertEqual({"requests": {"cpu": "250m", "memory": "256Mi", "ephemeral-storage": "2Gi"},
                              "limits": {"cpu": "2", "memory": "4Gi", "ephemeral-storage": "2Gi"}}, container["resources"])
            self.assertEqual(("2", "4Gi"), (policy["cpu_limit"], policy["memory_limit"]))
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
        # The controller shares the image's sandbox account and mounts no home.
        controller = self.named("Deployment", sandbox.CONTROL)["spec"]["template"]["spec"]
        self.assertEqual({"fsGroup": 2002}, controller["securityContext"])
        self.assertEqual((2002, 2002), tuple(controller["containers"][0]["securityContext"][key]
                                             for key in ("runAsUser", "runAsGroup")))
        self.assertFalse(any("persistentVolumeClaim" in v or "nfs" in v for v in controller["volumes"]))
        quota =self.named("ResourceQuota", "sandbox-pool")["spec"]["hard"]
        self.assertEqual({"pods": "5", "requests.cpu": "700m", "requests.memory": "832Mi", "requests.ephemeral-storage": "4384Mi",
                          "limits.cpu": "5000m", "limits.memory": "8960Mi", "limits.ephemeral-storage": "4384Mi",
                          "services": "5", "secrets": "2", "persistentvolumeclaims": "32"}, quota)
        for deployment in (o for o in self.objects if o["kind"] == "Deployment"):
            resources = deployment["spec"]["template"]["spec"]["containers"][0]["resources"]
            for key in ("cpu", "memory"):
                self.assertLess(quantity(resources["requests"][key], key == "memory"),
                                quantity(resources["limits"][key], key == "memory"))
            self.assertEqual(resources["requests"]["ephemeral-storage"], resources["limits"]["ephemeral-storage"])

    def test_each_group_gets_its_own_open_policy_and_the_family_one_is_filtered(self):
        egress = sandbox.egress_policies()
        self.assertEqual({"Family": "family", "Parents": "parents", "Admin": "parents"}, egress["groups"])
        self.assertEqual({"parents": {"filter": "open", "blocked": []}, "family": {"filter": "family", "blocked": []}},
                         egress["policies"])
        # The owner's blocked domains reach each proxy as its own file; nothing is an allowlist.
        files = self.named("ConfigMap", "sandbox-egress")["data"]
        self.assertEqual({"family.txt": "", "parents.txt": ""}, files)
        policies = sandbox.policies(self.config)
        for policy, name in (("sandbox-family", "family"), ("home-kid", "family"),
                             ("sandbox-parents", "parents"), ("home-alice", "parents")):
            env, proxy = policies[policy]["env"], f'http://{CONFIG["egress_service_ips"][name]}:4750'
            self.assertEqual({proxy}, {env[k] for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")})
            self.assertEqual(name, policies[policy]["podTemplate"]["metadata"]["labels"][sandbox.EGRESS_LABEL])
            self.assertEqual("localhost,127.0.0.1", env["NO_PROXY"])
        private = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,169.254.0.0/16,100.64.0.0/10,127.0.0.0/8"
        expected = {
            # Adults: open, minus the owner's list. Every policy keeps the private-range denial.
            "parents": ["--group", "parents", "--blocklist", "/config/parents.txt", "--deny-ranges", private],
            # Children: also the maintained lists in the image, the family resolvers, and no address literals.
            "family": ["--group", "family", "--blocklist", "/config/family.txt", "--deny-ranges", private,
                       "--maintained", "/blocklists", "--resolvers", "1.1.1.3:53,1.0.0.3:53", "--deny-literals"]}
        for name in ("family", "parents"):
            deployment = self.named("Deployment", "sandbox-egress-" + name)
            pod = deployment["spec"]["template"]["spec"]
            container = pod["containers"][0]
            self.assertEqual(CONFIG["egress_image"], container["image"])
            self.assertEqual(expected[name], container["args"])
            self.assertFalse(pod["automountServiceAccountToken"])
            self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
            self.assertEqual(["ALL"], container["securityContext"]["capabilities"]["drop"])
            self.assertEqual(sandbox.PROXY_RESOURCES, container["resources"])
            service_ = self.named("Service", "sandbox-egress-" + name)
            self.assertEqual(CONFIG["egress_service_ips"][name], service_["spec"]["clusterIP"])
        # The owner's resolvers replace the default; the policy file cannot unfilter the children.
        custom = sandbox.proxy_args({**self.config, "family_dns": ["9.9.9.11"]}, "family", egress["policies"]["family"])
        self.assertIn("9.9.9.11:53", custom)
        record = json.loads(sandbox.EGRESS_POLICIES.read_text())
        for change in ({"policies": {**record["policies"], "family": {"filter": "open", "blocked": []}}},
                       {"groups": {**record["groups"], "Family": "parents"}},
                       {"policies": {**record["policies"], "parents": {"filter": "open", "blocked": ["*.example.com"]}}},
                       {"policies": {**record["policies"], "parents": {"filter": "open", "blocked": ["192.168.1.1"]}}},
                       {"policies": {**record["policies"], "parents": {"filter": "open", "allowed": ["example.com"], "blocked": []}}}):
            with tempfile.TemporaryDirectory() as d, patch.object(sandbox, "EGRESS_POLICIES", Path(d) / "egress.json"):
                sandbox.EGRESS_POLICIES.write_text(json.dumps({**record, **change}))
                with self.assertRaises(ValueError, msg=change):
                    sandbox.egress_policies()
        with tempfile.TemporaryDirectory() as d, patch.object(sandbox, "EGRESS_POLICIES", Path(d) / "egress.json"):
            sandbox.EGRESS_POLICIES.write_text(json.dumps({**record, "policies": {
                "family": {"filter": "family", "blocked": ["kids-only.example"]},
                "parents": {"filter": "open", "blocked": ["time-sink.example", "other.example"]}}}))
            files = next(o for o in sandbox.render(self.config, ROOT) if o["metadata"]["name"] == "sandbox-egress"
                         and o["kind"] == "ConfigMap")["data"]
            self.assertEqual({"family.txt": "kids-only.example\n", "parents.txt": "time-sink.example\nother.example\n"}, files)

    def test_pinned_family_blocklists_are_bounded_and_commit_pinned(self):
        record = json.loads((ROOT / "infrastructure/ai-stack/egress/blocklists.json").read_text())["lists"]
        self.assertEqual(["nsfw", "bypass", "gambling"], [item["name"] for item in record])
        for item in record:
            self.assertRegex(item["url"], r"^https://raw\.githubusercontent\.com/[^/]+/[^/]+/[a-f0-9]{40}/")
            self.assertRegex(item["refresh_url"], r"^https://raw\.githubusercontent\.com/[^/]+/[^/]+/(main|master)/")
            self.assertEqual(item["url"].split("/")[3:5], item["refresh_url"].split("/")[3:5])
            self.assertRegex(item["sha256"], r"^[a-f0-9]{64}$")
            self.assertTrue(0 < item["bytes"] <= 32 << 20 and item["entries"] > 1000 and item["licence"])

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
            web = [rule for rule in egress if rule["to"][0].get("ipBlock", {}).get("cidr") == "0.0.0.0/0"]
            # Public addresses on the web ports only: the LAN, the cluster and link-local are excepted.
            self.assertEqual([[{"ipBlock": {"cidr": "0.0.0.0/0", "except": list(sandbox.PRIVATE_CIDRS)}}]], [r["to"] for r in web])
            self.assertLessEqual({"192.168.0.0/16", "10.0.0.0/8", "169.254.0.0/16"}, set(web[0]["to"][0]["ipBlock"]["except"]))
            self.assertEqual([80, 443], [port["port"] for port in web[0]["ports"]])
            dns = [rule for rule in egress if rule is not web[0]]
            self.assertEqual({53}, {port["port"] for rule in dns for port in rule["ports"]})
            if name == "family":
                # The children's proxy can ask only the family resolvers, never the cluster's DNS.
                self.assertEqual([[{"ipBlock": {"cidr": "1.1.1.3/32"}}, {"ipBlock": {"cidr": "1.0.0.3/32"}}]] * 2,
                                 [rule["to"] for rule in dns])
            else:
                self.assertTrue(all(rule["to"][0]["podSelector"] == {"matchLabels": {"k8s-app": "kube-dns"}} for rule in dns))
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
        self.assertEqual(["1.1.1.3", "1.0.0.3"], self.config["family_dns"])
        # Every mapped account's claim is one the storage render produces, in this namespace.
        claims = {o["metadata"]["name"]: o for o in sandbox_homes.render(self.config["users"]) if o["kind"] == "PersistentVolumeClaim"}
        self.assertEqual({"sandbox-home-alice", "sandbox-home-kid"}, set(claims))
        self.assertEqual({sandbox.NAMESPACE}, {o["metadata"]["namespace"] for o in claims.values()})
        self.assertEqual(sandbox.SANDBOX_UID, sandbox.policy(self.config, "family")["pod_security_context"]["runAsUser"])
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            missing = {k: v for k, v in CONFIG.items() if k != "family_enabled"}
            unresolved = {k: v for k, v in CONFIG.items() if k != "group_resolution"}
            for config in (missing, unresolved, *({**CONFIG, **change} for change in (
                    {"image": "example:latest"}, {"egress_image": "example:latest"}, {"api_server_cidr": "192.168.0.0/16"},
                    {"family_enabled": "false"}, {"parents_group_id": "family-fixture"}, {"egress": "all"},
                    {"group_resolution": "family-wins"}, {"egress_service_ips": {"family": "10.43.0.201"}},
                    {"egress_service_ips": {"family": "10.43.0.201", "parents": "10.43.0.201"}},
                    {"egress_service_ips": {"family": "10.43.0.201", "parents": "8.8.8.8"}}, {"homes": "homes.json"},
                    {"family_dns": []}, {"family_dns": ["10.43.0.10"]}, {"family_dns": ["192.168.1.1"]},
                    {"family_dns": "1.1.1.3"}, {"family_dns": ["dns.example"]}))):
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


class FakeEgressProxy(http.server.ThreadingHTTPServer):
    """Stands in for the egress proxy: refuses CONNECT to listed names with the reason
    header, and serves the status page with the refusals of the asking address only."""
    LISTED = {"adult.example": "blocked by the family filter"}

    def __init__(self):
        self.refused, self.requests = {}, []
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_CONNECT(self):
                host = self.path.rsplit(":", 1)[0]
                server.requests.append(self.requestline)
                reason = server.LISTED.get(host, "")
                server.refused.setdefault(self.client_address[0], []).insert(0, {"host": host, "reason": reason, "seconds_ago": 0})
                self.send_response(403)
                self.send_header("X-Sandbox-Blocked", reason)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                server.requests.append(self.requestline)
                body = json.dumps({"blocked": server.refused.get(self.client_address[0], [])}).encode()
                code = 200 if self.path == terminal.STATUS_URL else 404
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        super().__init__(("127.0.0.1", 0), Handler)


def tool(results):
    """A stand-in for Open Terminal: answers each call with the next canned JSON result."""
    calls = []
    async def app(scope, receive, send):
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        calls.append(body)
        status, value, content_type = results.pop(0)
        payload = value if isinstance(value, bytes) else json.dumps(value).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", content_type), (b"content-length", str(len(payload)).encode())]})
        await send({"type": "http.response.body", "body": payload})
    return app, calls


class BlockedReasonTests(unittest.IsolatedAsyncioTestCase):
    """The workspace's tool results name the proxy's reason for a refused tunnel."""
    FAILED = {"detail": {"completed_actions": 0, "failed_action_index": 0, "error": "net::ERR_TUNNEL_CONNECTION_FAILED"}}

    async def call(self, middleware, method, path, payload=None):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=middleware), base_url="http://terminal") as client:
            return await client.request(method, path, json=payload)

    async def test_browser_navigation_names_the_proxy_reason(self):
        lookups = []
        def lookup():
            lookups.append(1)
            return [{"host": "adult.example", "reason": "blocked by the family filter", "seconds_ago": 1},
                    {"host": "earlier.example", "reason": "blocked by the sandbox blocklist", "seconds_ago": 200}]
        app, calls = tool([(502, self.FAILED, b"application/json")])
        response = await self.call(terminal.BlockedReason(app, lookup), "POST", "/browser/action",
                                   {"actions": [{"action": "navigate", "url": "https://adult.example/"}]})
        self.assertEqual(502, response.status_code)
        self.assertEqual(int(response.headers["content-length"]), len(response.content))
        value = response.json()
        # Upstream's result is kept whole; the reason is added beside it.
        self.assertEqual(self.FAILED["detail"], value["detail"])
        self.assertEqual([{"host": "adult.example", "reason": "blocked by the family filter", "seconds_ago": 1}],
                         value["sandbox_egress"]["refused"])
        self.assertIn("retrying will not help", value["sandbox_egress"]["note"])
        self.assertEqual("curl -s http://sandbox-egress.invalid/blocked", value["sandbox_egress"]["check"])
        self.assertEqual(1, len(lookups))
        self.assertIn(b"adult.example", calls[0])

    async def test_a_click_into_a_refused_site_shows_the_latest_refusal(self):
        snapshot = {"url": "https://example.org/", "snapshot": 'heading "This site can\'t be reached" ERR_TUNNEL_CONNECTION_FAILED'}
        blocked = [{"host": "adult.example", "reason": "blocked by the family filter", "seconds_ago": 3},
                   {"host": "old.example", "reason": "private address", "seconds_ago": 240}]
        app, _ = tool([(200, snapshot, b"application/json")])
        value = (await self.call(terminal.BlockedReason(app, lambda: blocked), "GET", "/browser/snapshot")).json()
        self.assertEqual(snapshot["snapshot"], value["snapshot"])
        self.assertEqual(["adult.example"], [entry["host"] for entry in value["sandbox_egress"]["refused"]])

    async def test_unrelated_results_pass_through_unchanged(self):
        lookups = []
        def lookup():
            lookups.append(1)
            return []
        ok = {"status": "done", "exit_code": 0, "output": [{"type": "output", "data": "fine"}]}
        app, _ = tool([(200, ok, b"application/json"), (502, self.FAILED, b"application/json"),
                       (200, b"ERR_TUNNEL_CONNECTION_FAILED", b"text/plain"),
                       (200, ["ERR_TUNNEL_CONNECTION_FAILED"], b"application/json")])
        middleware = terminal.BlockedReason(app, lookup)
        self.assertEqual(json.dumps(ok).encode(), (await self.call(middleware, "POST", "/execute", {"command": "true"})).content)
        # Not a command or browser result: never read.
        self.assertEqual(self.FAILED, (await self.call(middleware, "GET", "/files/read")).json())
        self.assertEqual(b"ERR_TUNNEL_CONNECTION_FAILED", (await self.call(middleware, "POST", "/execute", {})).content)
        self.assertEqual(["ERR_TUNNEL_CONNECTION_FAILED"], (await self.call(middleware, "POST", "/execute", {})).json())
        self.assertEqual([], lookups)

    async def test_no_record_or_no_answer_still_says_where_to_look(self):
        app, _ = tool([(502, self.FAILED, b"application/json"), (502, self.FAILED, b"application/json")])
        value = (await self.call(terminal.BlockedReason(app, lambda: []), "POST", "/browser/action",
                                 {"actions": [{"action": "navigate", "url": "https://missing.example/"}]})).json()
        self.assertIn("something else", value["sandbox_egress"]["note"])
        self.assertNotIn("refused", value["sandbox_egress"])
        def unreachable():
            raise OSError("proxy unreachable")
        value = (await self.call(terminal.BlockedReason(app, unreachable), "POST", "/browser/action",
                                 {"actions": [{"action": "navigate", "url": "https://adult.example/"}]})).json()
        self.assertIn("could not be asked", value["sandbox_egress"]["note"])
        self.assertEqual("curl -s http://sandbox-egress.invalid/blocked", value["sandbox_egress"]["check"])

    async def test_command_output_through_a_fake_proxy(self):
        """An HTTP library through the proxy sees the 403 unchanged; the tool result gains the reason."""
        proxy = FakeEgressProxy()
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        address = f"http://127.0.0.1:{proxy.server_address[1]}"
        try:
            with patch.dict(os.environ, {"http_proxy": address, "https_proxy": address, "HTTP_PROXY": address,
                                         "HTTPS_PROXY": address, "no_proxy": "localhost", "NO_PROXY": "localhost"}):
                # The HTTP-library behaviour is the proxy's 403: unchanged by this work.
                with self.assertRaises(OSError) as raised:
                    await asyncio.to_thread(urllib.request.build_opener().open, "https://adult.example/", timeout=5)
                self.assertIn("Tunnel connection failed: 403", str(raised.exception))
                result = {"status": "done", "exit_code": 1, "output": [{"type": "output", "data": str(raised.exception)}]}
                app, _ = tool([(200, result, b"application/json")])
                response = await self.call(terminal.BlockedReason(app), "POST", "/execute",
                                           {"command": "python fetch.py https://adult.example/"})
            value = response.json()
            self.assertEqual(result["output"], value["output"])
            self.assertEqual([{"host": "adult.example", "reason": "blocked by the family filter", "seconds_ago": 0}],
                             value["sandbox_egress"]["refused"])
            # The lookup went through the workspace's proxy to the status name, never direct.
            self.assertIn("GET http://sandbox-egress.invalid/blocked HTTP/1.1", proxy.requests)
        finally:
            proxy.shutdown()
            proxy.server_close()


if __name__ == "__main__":
    unittest.main()
