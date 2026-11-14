"""Opt-in native Open Terminal composition. No cluster calls or secret values."""
import json
from pathlib import Path
import re

NAMESPACE = "assistant-sandbox"
CONTROL = "assistant-terminals"
LABEL = "app.kubernetes.io/name"
WORKSPACE_CPU = "750m"
WORKSPACE_MEMORY = "2Gi"


def read_config(path):
    value = json.loads(Path(path).read_text())
    required = {"image", "parents_group_id", "family_group_id", "api_server_cidr", "api_server_port"}
    if not required <= value.keys() or value.keys() - required - {"family_enabled"}:
        raise ValueError("sandbox config needs image, group IDs and the node API endpoint")
    if not re.fullmatch(r"[a-z0-9./:_-]+@sha256:[a-f0-9]{64}", value["image"]):
        raise ValueError("sandbox image must be pinned by digest")
    import ipaddress
    network = ipaddress.ip_network(value["api_server_cidr"])
    if network.version != 4 or network.prefixlen != 32 or not network.is_private:
        raise ValueError("sandbox control plane needs one private API server /32")
    if type(value["api_server_port"]) is not int or not 1 <= value["api_server_port"] <= 65535:
        raise ValueError("invalid API server port")
    for key in ("parents_group_id", "family_group_id"):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", value[key]):
            raise ValueError("sandbox requires exact nonempty group IDs")
    if value["parents_group_id"] == value["family_group_id"]:
        raise ValueError("Parents and Family must be distinct groups")
    if type(value.get("family_enabled", False)) is not bool:
        raise ValueError("family_enabled must be boolean")
    return value


def peer(namespace, app):
    return {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": namespace}},
            "podSelector": {"matchLabels": {LABEL: app}}}


def obj(kind, name, spec=None, api="v1", namespace=NAMESPACE, **fields):
    result = {"apiVersion": api, "kind": kind,
              "metadata": {"name": name, **({"namespace": namespace} if namespace else {})}, **fields}
    if spec is not None:
        result["spec"] = spec
    return result


def policy(config):
    security = {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000,
                "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                "capabilities": {"drop": ["ALL"]}, "seccompProfile": {"type": "RuntimeDefault"}}
    resources = {"cpu": WORKSPACE_CPU, "memory": WORKSPACE_MEMORY, "ephemeral-storage": "2Gi"}
    return {"image": config["image"], "cpu_limit": WORKSPACE_CPU, "memory_limit": WORKSPACE_MEMORY,
            "storage": "", "idle_timeout_minutes": 15, "max_lifetime_minutes": 120,
            "restricted": True, "container_security_context": security,
            "pod_security_context": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000,
                                     "fsGroup": 1000, "seccompProfile": {"type": "RuntimeDefault"}},
            "env": {"HOME": "/home/user", "TMPDIR": "/home/user/tmp",
                    "OPEN_TERMINAL_ENABLE_DESKTOP": "false", "OPEN_TERMINAL_MULTI_USER": "false",
                    "OPEN_TERMINAL_BROWSER_NO_SANDBOX": "true", "OPEN_TERMINAL_EXECUTE_TIMEOUT": "120",
                    "OPEN_TERMINAL_MAX_SESSIONS": "4"},
            "podTemplate": {"metadata": {"labels": {LABEL: "assistant-workspace"}}, "spec": {
                "automountServiceAccountToken": False, "enableServiceLinks": False,
                "terminationGracePeriodSeconds": 10,
                "containers": [{"name": "open-terminal", "command": ["python", "/opt/sandbox/terminal.py"],
                    "resources": {"requests": resources, "limits": resources},
                    "volumeMounts": [{"name": "workspace", "mountPath": "/home/user"},
                                     {"name": "workspace", "mountPath": "/tmp"}]}],
                "volumes": [{"name": "workspace", "emptyDir": {"sizeLimit": "2Gi"}}]}}}


def render(config, root):
    """Return a separate namespace bundle; the control Deployment stays at zero."""
    labels = {LABEL: CONTROL}
    security = policy(config)["container_security_context"]
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
                 "command": ["python", "/opt/sandbox/control.py"], "securityContext": security,
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
        obj("ConfigMap", "sandbox-config", data={"config.json": json.dumps(config),
                                               "policy.json": json.dumps(policy(config))}),
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
        # Quota includes the one controller. Each admitted workspace has equal requests/limits.
        obj("ResourceQuota", "sandbox-pool", {"hard": {"pods": "3", "requests.cpu": "1750m",
            "limits.cpu": "1750m", "requests.memory": "4352Mi", "limits.memory": "4352Mi",
            "requests.ephemeral-storage": "4352Mi", "limits.ephemeral-storage": "4352Mi",
            "services": "3", "secrets": "2", "persistentvolumeclaims": "0"}}),
    ]
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
    objects += [net("deny-all", {}, [], []),
        net("workspace", {LABEL: "assistant-workspace"}, [flow("from", [controller], 8000)], []),
        net("controller", labels, [flow("from", [webui, alloy], 3000)], [
            flow("to", [peer(NAMESPACE, "assistant-workspace")], 8000),
            flow("to", [webui], 8080),
            flow("to", [{"ipBlock": {"cidr": config["api_server_cidr"]}}], config["api_server_port"]),
            *[flow("to", [peer("kube-system", "coredns")], 53, p) for p in ("TCP", "UDP")]]),
        net("webui-sandbox", {LABEL: "open-webui"}, [flow("from", [controller], 8080)],
            [flow("to", [controller], 3000)], "ai-home-lab"),
        net("alloy-sandbox", {LABEL: "alloy"}, None, [flow("to", [controller], 3000)],
            "workstation-observability")]
    # K3s CoreDNS uses k8s-app=kube-dns, not the application name label.
    for rule in objects[-3]["spec"]["egress"][-2:]:
        rule["to"][0]["podSelector"] = {"matchLabels": {"k8s-app": "kube-dns"}}
    return objects


def configure_webui(objects, config):
    deployment = next(o for o in objects if o["kind"] == "Deployment" and o["metadata"]["name"] == "open-webui")
    env = deployment["spec"]["template"]["spec"]["containers"][0].setdefault("env", [])
    connections = []
    if config:
        groups = [config["parents_group_id"]]
        if config.get("family_enabled", False):
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
             "memory_mib": quantity(hard["limits.memory"], True)}]
