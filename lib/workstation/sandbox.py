"""Opt-in native Open Terminal composition. No cluster calls or secret values."""
import ipaddress
import json
from pathlib import Path
import re

import yaml

NAMESPACE = "assistant-sandbox"
CONTROL = "assistant-terminals"
WORKSPACE = "assistant-workspace"
EGRESS = "sandbox-egress"
LABEL = "app.kubernetes.io/name"
# Which reviewed egress list a workspace or proxy pod belongs to.
EGRESS_LABEL = "workstation.ai/sandbox-egress"
WORKSPACE_CPU = "750m"
WORKSPACE_MEMORY = "2Gi"
WORKSPACES = 2
PROXY_PORT = 4750
PROXY_METRICS_PORT = 9810
PROXY_RESOURCES = {"cpu": "100m", "memory": "64Mi", "ephemeral-storage": "16Mi"}
RESOLUTIONS = ("most-permissive", "most-restrictive")
EGRESS_LISTS = Path(__file__).resolve().parents[2] / "infrastructure/service/sandbox-egress.json"
# Never reachable through the proxy, whatever a listed name resolves to: RFC 1918 (the
# LAN 192.168.0.0/16 and the cluster's pod and service ranges inside 10.0.0.0/8),
# link-local, carrier NAT and loopback. Used for the proxy's NetworkPolicy and its
# own deny list; Smokescreen refuses the same ranges by default after resolving.
PRIVATE_CIDRS = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10", "127.0.0.0/8")

# After the constants: sandbox_homes reads NAMESPACE from this module.
import sandbox_homes  # noqa: E402


def read_config(path):
    value = json.loads(Path(path).read_text())
    required = {"image", "egress_image", "parents_group_id", "family_group_id", "family_enabled",
                "group_resolution", "egress_service_ips", "api_server_cidr", "api_server_port"}
    if not required <= value.keys() or value.keys() - required - {"homes"}:
        raise ValueError("sandbox config needs image, egress_image, group IDs, family_enabled, group_resolution, "
                         "egress_service_ips and the node API endpoint; homes is optional")
    for key in ("image", "egress_image"):
        if not (isinstance(value[key], str) and re.fullmatch(r"[a-z0-9./:_-]+@sha256:[a-f0-9]{64}", value[key])):
            raise ValueError(f"sandbox {key} must be pinned by digest")
    network = ipaddress.ip_network(value["api_server_cidr"])
    if network.version != 4 or network.prefixlen != 32 or not network.is_private:
        raise ValueError("sandbox control plane needs one private API server /32")
    if type(value["api_server_port"]) is not int or not 1 <= value["api_server_port"] <= 65535:
        raise ValueError("invalid API server port")
    for key in ("parents_group_id", "family_group_id"):
        if not (isinstance(value[key], str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", value[key])):
            raise ValueError("sandbox requires exact nonempty group IDs")
    if value["parents_group_id"] == value["family_group_id"]:
        raise ValueError("Parents and Family must be distinct groups")
    # No implied answers: the kids switch and the dual-membership rule are stated.
    if type(value["family_enabled"]) is not bool:
        raise ValueError("family_enabled must be boolean")
    if value["group_resolution"] not in RESOLUTIONS:
        raise ValueError("group_resolution must be most-permissive or most-restrictive")
    lists = egress_lists()["lists"]
    ips = value["egress_service_ips"]
    if not isinstance(ips, dict) or set(ips) != set(lists) or len(set(ips.values())) != len(ips):
        raise ValueError("egress_service_ips needs one distinct service address per egress list: " + ", ".join(sorted(lists)))
    for address in ips.values():
        # Workspaces have no DNS, so they reach their proxy by a fixed in-cluster address.
        if not isinstance(address, str) or not ipaddress.ip_address(address).is_private \
                or ipaddress.ip_address(address).version != 4:
            raise ValueError("egress_service_ips must be private IPv4 service addresses")
    # Without a mapping nobody has a persistent home; every workspace is disposable.
    homes = value.pop("homes", None)
    if homes is not None and not (isinstance(homes, str) and homes.startswith("/")):
        raise ValueError("homes must be an absolute path to the sandbox homes mapping")
    value["users"] = sandbox_homes.read_users(homes) if homes else []
    return value


def egress_lists():
    """The reviewed per-group domain lists: {"groups": {group: list}, "lists": {list: [domain]}}."""
    value = json.loads(EGRESS_LISTS.read_text())
    groups, lists = value["groups"], value["lists"]
    if set(groups) != set(sandbox_homes.GROUPS) or not set(groups.values()) <= set(lists):
        raise ValueError("egress lists must map Admin, Parents and Family to a named list")
    for name, domains in lists.items():
        if not re.fullmatch(r"[a-z]{1,16}", name) or not domains or len(set(domains)) != len(domains):
            raise ValueError("egress list names are short lowercase words with distinct domains")
        for domain in domains:
            # A domain, or one leading wildcard label. No bare wildcard, address, port or path.
            if not re.fullmatch(r"(\*\.)?([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}", domain):
                raise ValueError(f"egress list {name}: {domain!r} is not a domain")
    return {"groups": groups, "lists": lists}


def egress_acl(lists):
    """Smokescreen v1 ACL: one service per list; anything else gets the empty default."""
    return yaml.safe_dump({"version": "v1", "services": [
        {"name": name, "project": "sandbox", "action": "enforce", "allowed_domains": domains}
        for name, domains in sorted(lists.items())],
        "default": {"name": "default", "project": "sandbox", "action": "enforce"}}, sort_keys=False)


def peer(namespace, app, **labels):
    return {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": namespace}},
            "podSelector": {"matchLabels": {LABEL: app, **labels}}}


def obj(kind, name, spec=None, api="v1", namespace=NAMESPACE, **fields):
    result = {"apiVersion": api, "kind": kind,
              "metadata": {"name": name, **({"namespace": namespace} if namespace else {})}, **fields}
    if spec is not None:
        result["spec"] = spec
    return result


def restricted(user):
    return {"runAsNonRoot": True, "runAsUser": user, "runAsGroup": user,
            "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]}, "seccompProfile": {"type": "RuntimeDefault"}}


def policy(config, egress, home=None):
    """One upstream policy: a workspace on one egress list, with a persistent home or none.

    `home` is a mapped user's name. Their claim holds /home/user and outlives the pod;
    /tmp (caches, temporary files) is always a private emptyDir that goes with the pod.
    """
    resources = {"cpu": WORKSPACE_CPU, "memory": WORKSPACE_MEMORY, "ephemeral-storage": "2Gi"}
    proxy = f'http://{config["egress_service_ips"][egress]}:{PROXY_PORT}'
    volume = ({"persistentVolumeClaim": {"claimName": sandbox_homes.claim_name(home)}} if home
              else {"emptyDir": {"sizeLimit": "2Gi"}})
    env = {"HOME": "/home/user", "TMPDIR": "/tmp", "XDG_CACHE_HOME": "/tmp/cache", "npm_config_cache": "/tmp/npm",
           "OPEN_TERMINAL_ENABLE_DESKTOP": "false", "OPEN_TERMINAL_MULTI_USER": "false",
           "OPEN_TERMINAL_BROWSER_NO_SANDBOX": "true", "OPEN_TERMINAL_EXECUTE_TIMEOUT": "120",
           "OPEN_TERMINAL_MAX_SESSIONS": "4", "NODE_USE_ENV_PROXY": "1",
           "NO_PROXY": "localhost,127.0.0.1", "no_proxy": "localhost,127.0.0.1"}
    # Both spellings: tools disagree on which they read. There is no direct route to fall back to.
    env.update({name: proxy for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")})
    return {"image": config["image"], "cpu_limit": WORKSPACE_CPU, "memory_limit": WORKSPACE_MEMORY,
            "storage": "", "idle_timeout_minutes": 15, "max_lifetime_minutes": 120,
            "restricted": True, "container_security_context": restricted(1000),
            # OnRootMismatch: the NAS home is already owned by 1000; do not walk it on every mount.
            "pod_security_context": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000,
                                     "fsGroup": 1000, "fsGroupChangePolicy": "OnRootMismatch",
                                     "seccompProfile": {"type": "RuntimeDefault"}},
            "env": env,
            "podTemplate": {"metadata": {"labels": {LABEL: WORKSPACE, EGRESS_LABEL: egress}}, "spec": {
                "automountServiceAccountToken": False, "enableServiceLinks": False,
                "terminationGracePeriodSeconds": 10,
                "containers": [{"name": "open-terminal", "command": ["python", "/opt/sandbox/terminal.py"],
                    "resources": {"requests": resources, "limits": resources},
                    "volumeMounts": [{"name": "home", "mountPath": "/home/user"},
                                     {"name": "scratch", "mountPath": "/tmp"}]}],
                "volumes": [{"name": "home", **volume}, {"name": "scratch", "emptyDir": {"sizeLimit": "2Gi"}}]}}}


def policies(config):
    """Every policy the gate may route to: one per egress list, one per mapped user."""
    groups = egress_lists()["groups"]
    result = {f"sandbox-{name}": policy(config, name) for name in sorted(set(groups.values()))}
    for user in config["users"]:
        result["home-" + user["name"]] = policy(config, groups[user["group"]], user["name"])
    return result


def gate_config(config):
    """What the controller's gate needs; no image or network detail."""
    return {"parents_group_id": config["parents_group_id"], "family_group_id": config["family_group_id"],
            "family_enabled": config["family_enabled"], "group_resolution": config["group_resolution"],
            "egress": egress_lists()["groups"],
            "users": {user["webui_user_id"]: {"name": user["name"], "group": user["group"]} for user in config["users"]}}


def render(config, root):
    """Return a separate namespace bundle; every Deployment stays at zero."""
    labels = {LABEL: CONTROL}
    lists = egress_lists()["lists"]
    env = {"TERMINALS_BACKEND": "kubernetes", "TERMINALS_KUBERNETES_NAMESPACE": NAMESPACE,
           "TERMINALS_KUBERNETES_STORAGE_SIZE": "", "TERMINALS_ENABLE_UI": "false",
           "TERMINALS_OPEN_WEBUI_URL": "http://open-webui.ai-home-lab.svc:8080",
           "TERMINALS_DATABASE_URL": "sqlite+aiosqlite:////data/terminals.db",
           "TERMINALS_TOKEN_CACHE_TTL": "0", "TERMINALS_LOG_LEVEL": "CRITICAL",
           "TERMINALS_ACCESS_LOG": "false", "TERMINALS_REPLAY_BODY_LIMIT": "1048576",
           "TERMINALS_IDLE_TIMEOUT_MINUTES": "15", "TERMINALS_MAX_LIFETIME_MINUTES": "120",
           "TERMINALS_MAX_CPU": WORKSPACE_CPU, "TERMINALS_MAX_MEMORY": WORKSPACE_MEMORY,
           "TERMINALS_ALLOWED_IMAGES": config["image"], "TERMINALS_IMAGE": config["image"],
           "HOME": "/data", "PYTHONDONTWRITEBYTECODE": "1"}
    container = {"name": CONTROL, "image": config["image"], "imagePullPolicy": "IfNotPresent",
                 "command": ["python", "/opt/sandbox/control.py"], "securityContext": restricted(1000),
                 "env": [{"name": k, "value": v} for k, v in env.items()],
                 "resources": {"requests": {"cpu": "250m", "memory": "256Mi", "ephemeral-storage": "256Mi"},
                               "limits": {"cpu": "250m", "memory": "256Mi", "ephemeral-storage": "256Mi"}},
                 "ports": [{"name": "http", "containerPort": 3000}],
                 "readinessProbe": {"httpGet": {"path": "/health", "port": 3000}},
                 "volumeMounts": [{"name": "data", "mountPath": "/data"},
                                  {"name": "data", "mountPath": "/tmp"},
                                  {"name": "config", "mountPath": "/config", "readOnly": True}]}
    namespace = obj("Namespace", NAMESPACE, namespace=None)
    namespace["metadata"]["labels"] = {"pod-security.kubernetes.io/enforce": "restricted"}
    objects = [namespace, obj("ServiceAccount", CONTROL),
        obj("ConfigMap", "sandbox-config", data={"config.json": json.dumps(gate_config(config)),
                                               "policies.json": json.dumps(policies(config))}),
        obj("ConfigMap", EGRESS, data={"acl.yaml": egress_acl(lists)}),
        obj("Deployment", CONTROL, {"replicas": 0, "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": labels}, "template": {"metadata": {"labels": labels}, "spec": {
                "serviceAccountName": CONTROL, "automountServiceAccountToken": True,
                "enableServiceLinks": False, "securityContext": {"fsGroup": 1000},
                "containers": [container], "volumes": [{"name": "data", "emptyDir": {"sizeLimit": "256Mi"}},
                    {"name": "config", "configMap": {"name": "sandbox-config"}}]}}}, api="apps/v1"),
        obj("Service", CONTROL, {"selector": labels, "ports": [{"port": 3000, "targetPort": 3000}]}),
        obj("Role", CONTROL, api="rbac.authorization.k8s.io/v1", rules=[{
            "apiGroups": [""], "resources": ["pods", "services", "secrets"],
            "verbs": ["get", "list", "create", "delete"]}]),
        obj("RoleBinding", CONTROL, api="rbac.authorization.k8s.io/v1",
            roleRef={"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": CONTROL},
            subjects=[{"kind": "ServiceAccount", "name": CONTROL, "namespace": NAMESPACE}]),
    ]
    for name in sorted(lists):
        # One proxy per list. Which list a workspace gets is decided by which proxy the
        # NetworkPolicy lets it reach, not by anything the workspace sends.
        proxy = {LABEL: EGRESS, EGRESS_LABEL: name}
        objects += [
            obj("Deployment", f"{EGRESS}-{name}", {"replicas": 0, "strategy": {"type": "Recreate"},
                "selector": {"matchLabels": proxy}, "template": {"metadata": {"labels": proxy}, "spec": {
                    "automountServiceAccountToken": False, "enableServiceLinks": False,
                    "securityContext": {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}},
                    "containers": [{"name": EGRESS, "image": config["egress_image"], "imagePullPolicy": "IfNotPresent",
                        "args": ["--group", name, "--acl", "/config/acl.yaml",
                                 "--deny-ranges", ",".join(PRIVATE_CIDRS)],
                        "securityContext": restricted(65532),
                        "resources": {"requests": PROXY_RESOURCES, "limits": PROXY_RESOURCES},
                        "ports": [{"name": "proxy", "containerPort": PROXY_PORT},
                                  {"name": "metrics", "containerPort": PROXY_METRICS_PORT}],
                        "readinessProbe": {"tcpSocket": {"port": PROXY_PORT}},
                        "volumeMounts": [{"name": "config", "mountPath": "/config", "readOnly": True}]}],
                    "volumes": [{"name": "config", "configMap": {"name": EGRESS}}]}}}, api="apps/v1"),
            obj("Service", f"{EGRESS}-{name}", {"selector": proxy, "clusterIP": config["egress_service_ips"][name],
                "ports": [{"name": "proxy", "port": PROXY_PORT, "targetPort": PROXY_PORT},
                          {"name": "metrics", "port": PROXY_METRICS_PORT, "targetPort": PROXY_METRICS_PORT}]})]
    # The quota is the whole pool: the controller, one proxy per list and every
    # workspace slot. Each admitted pod has equal requests and limits. Home claims are
    # static, one per mapped user, applied from sandbox_homes after this bundle; the
    # bound is that mapping's own maximum, so adding a user needs no quota change.
    # The controller has no rights on claims.
    def total(key, memory=False):
        from telemetry import quantity
        proxies = len(lists) * quantity(PROXY_RESOURCES[key], memory)
        workspace = {"cpu": WORKSPACE_CPU, "memory": WORKSPACE_MEMORY, "ephemeral-storage": "2Gi"}[key]
        controller = container["resources"]["limits"][key]
        return quantity(controller, memory) + proxies + WORKSPACES * quantity(workspace, memory)
    cpu, memory, storage = f'{round(total("cpu") * 1000)}m', f'{total("memory", True):.0f}Mi', \
        f'{total("ephemeral-storage", True):.0f}Mi'
    pods = 1 + len(lists) + WORKSPACES
    objects.append(obj("ResourceQuota", "sandbox-pool", {"hard": {"pods": str(pods), "requests.cpu": cpu,
        "limits.cpu": cpu, "requests.memory": memory, "limits.memory": memory,
        "requests.ephemeral-storage": storage, "limits.ephemeral-storage": storage,
        "services": str(pods), "secrets": str(WORKSPACES),
        "persistentvolumeclaims": "32"}}))
    objects[-1]["metadata"]["labels"] = {"workstation.ai/capacity-reservation": "sandbox"}
    def net(name, selector, ingress=None, egress=None, namespace=NAMESPACE):
        spec = {"podSelector": {"matchLabels": selector}, "policyTypes": []}
        for direction, rules in (("Ingress", ingress), ("Egress", egress)):
            if rules is not None:
                spec["policyTypes"].append(direction)
                spec[direction.lower()] = rules
        return obj("NetworkPolicy", name, spec, api="networking.k8s.io/v1", namespace=namespace)
    def flow(direction, peers, port, protocol="TCP"):
        return {direction: peers, "ports": [{"port": port, "protocol": protocol}]}
    controller = peer(NAMESPACE, CONTROL)
    webui = peer("ai-home-lab", "open-webui")
    alloy = peer("workstation-observability", "alloy")
    # K3s CoreDNS uses k8s-app=kube-dns, not the application name label.
    dns = {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
           "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}
    public = {"ipBlock": {"cidr": "0.0.0.0/0", "except": list(PRIVATE_CIDRS)}}
    objects += [net("deny-all", {}, [], []),
        # Every workspace: only the controller in, nothing out. The per-list policies below
        # add the one proxy; a workspace without a list label keeps no egress at all.
        net("workspace", {LABEL: WORKSPACE}, [flow("from", [controller], 8000)], []),
        net("controller", labels, [flow("from", [webui, alloy], 3000)], [
            flow("to", [peer(NAMESPACE, WORKSPACE)], 8000),
            flow("to", [webui], 8080),
            flow("to", [{"ipBlock": {"cidr": config["api_server_cidr"]}}], config["api_server_port"]),
            *[flow("to", [dns], 53, p) for p in ("TCP", "UDP")]]),
        net("webui-sandbox", {LABEL: "open-webui"}, [flow("from", [controller], 8080)],
            [flow("to", [controller], 3000)], "ai-home-lab"),
        net("alloy-sandbox", {LABEL: "alloy"}, None, [flow("to", [controller], 3000),
            flow("to", [peer(NAMESPACE, EGRESS)], PROXY_METRICS_PORT)], "workstation-observability")]
    for name in sorted(lists):
        workspace, proxy = peer(NAMESPACE, WORKSPACE, **{EGRESS_LABEL: name}), peer(NAMESPACE, EGRESS, **{EGRESS_LABEL: name})
        objects += [
            net(f"workspace-egress-{name}", workspace["podSelector"]["matchLabels"], None,
                [flow("to", [proxy], PROXY_PORT)]),
            # The proxy: its own workspaces in; out to DNS and to public addresses on the
            # web ports only. The LAN, the node, the cluster and other pods stay closed
            # here too, so the address check does not rest on the proxy alone.
            net(f"{EGRESS}-{name}", proxy["podSelector"]["matchLabels"],
                [flow("from", [workspace], PROXY_PORT), flow("from", [alloy], PROXY_METRICS_PORT)],
                [*[flow("to", [dns], 53, p) for p in ("TCP", "UDP")],
                 {"to": [public], "ports": [{"port": 80, "protocol": "TCP"}, {"port": 443, "protocol": "TCP"}]}])]
    return objects


def configure_webui(objects, config):
    deployment = next(o for o in objects if o["kind"] == "Deployment" and o["metadata"]["name"] == "open-webui")
    env = deployment["spec"]["template"]["spec"]["containers"][0].setdefault("env", [])
    connections = []
    if config:
        groups = [config["parents_group_id"]]
        if config["family_enabled"]:
            groups.append(config["family_group_id"])
        connections = [{"id": "family-sandbox", "name": "Private coding workspace", "enabled": True,
            "server_type": "orchestrator", "policy_id": "sandbox", "auth_type": "session", "key": "",
            "url": f"http://{CONTROL}.{NAMESPACE}.svc:3000", "path": "/openapi.json",
            "config": {"access_grants": [{"principal_type": "group", "principal_id": g, "permission": "read"}
                                         for g in groups],
                       "contexts": {"chat": {"context_id": "default"}, "automation": False}}}]
    env[:] = [e for e in env if e["name"] != "TERMINAL_SERVER_CONNECTIONS"]
    env.append({"name": "TERMINAL_SERVER_CONNECTIONS", "value": json.dumps(connections)})


def capacity_rows(objects):
    """Reserve the entire quota, including workspaces that are currently asleep."""
    quota = next((o for o in objects if o["kind"] == "ResourceQuota"
                  and o["metadata"].get("namespace") == NAMESPACE), None)
    if not quota:
        return []
    from telemetry import quantity
    hard = quota["spec"]["hard"]
    return [{"name": "assistant-sandbox-pool", "cpu": quantity(hard["limits.cpu"]),
             "memory_mib": round(quantity(hard["limits.memory"], True))}]
