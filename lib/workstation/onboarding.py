"""Opt-in family key onboarding: the CronJob, its identity and its flows. No cluster calls or secret values.

The job itself is infrastructure/service/key_onboarding.py; docs/AI-SERVICE.md
(Automatic family onboarding) describes the design and its threat model.
"""
import json

NAME = "key-onboarding"
NAMESPACE = "ai-home-lab"
LABEL = "app.kubernetes.io/name"
MODES = ("off", "dry-run", "live")
SCHEDULE = "*/5 * * * *"
# A key is replaced when fewer than this many days remain. Keys last 90 days, the
# gateway's maximum, so a member's key changes about every 60 days.
RENEW_DAYS = 30
# The replaced key stays valid this long: the gateway reads the mapping from a projected
# Secret, which the kubelet refreshes within about a minute and a half.
RETIRE_GRACE_SECONDS = 600
# The same bound as the sandbox home mapping.
MAX_USERS = 32
# One policy per tier, inside service.KEY_LIMITS. Parents (and an administrator in either
# group) run agents: the adult bounds of docs/AI-SERVICE.md step 6. Family is the
# children: one request at a time, half the token rate, no pictures.
POLICIES = {"parents": {"rpm": 60, "tpm": 2000000, "concurrency": 2, "days": 90, "images": True},
            "family": {"rpm": 30, "tpm": 1000000, "concurrency": 1, "days": 90, "images": False}}
# Unmeasured: a short standard-library Python process. Not counted by `service plan`.
RESOURCES = {"requests": {"cpu": "20m", "memory": "64Mi"}, "limits": {"cpu": "250m", "memory": "256Mi"}}
SECRETS = ["gateway-browser-keys", "gateway-image-permissions"]
STATUS = "key-onboarding-status"


def job_config(mode, sandbox, models):
    """What the job needs: group ids, key policies and who already has a home. No credential."""
    return {"mode": mode, "namespace": NAMESPACE, "gateway": "http://litellm:4000", "database": "/webui/webui.db",
            "parents_group_id": sandbox["parents_group_id"], "family_group_id": sandbox["family_group_id"],
            "policies": {tier: {"models": sorted(models), **policy} for tier, policy in POLICIES.items()},
            "renew_days": RENEW_DAYS, "retire_grace_seconds": RETIRE_GRACE_SECONDS, "max_users": MAX_USERS,
            "home_user_ids": sorted(user["webui_user_id"] for user in sandbox["users"]),
            # Who gets a workspace at all: Parents always, Family when the sandbox admits them.
            "home_tiers": ["parents"] + (["family"] if sandbox["family_enabled"] else [])}


def obj(kind, name, api="v1", **fields):
    return {"apiVersion": api, "kind": kind, "metadata": {"name": name, "namespace": NAMESPACE}, **fields}


def render(mode, sandbox, objects, models, root):
    """The onboarding bundle for a rendered assistant composition; empty when off.

    `dry-run` carries no admin credential and no write permission at all: it reads the
    two Secrets and the Open WebUI database and prints what it would do. `live` adds the
    master key, the right to replace the two Secrets and the status ConfigMap, and the
    flow to the gateway.
    """
    if mode not in MODES:
        raise ValueError("key_onboarding must be off, dry-run or live")
    if mode == "off":
        return []
    live = mode == "live"
    webui = next(o for o in objects if o["kind"] == "Deployment" and o["metadata"]["name"] == "open-webui")
    spec = webui["spec"]["template"]["spec"]
    # The job runs from the Open WebUI image, as its user: the same SQLite library as the
    # database's only writer, already on the node. Nothing of Open WebUI itself is started.
    image = spec["containers"][0]["image"]
    if "@sha256:" not in image:
        raise ValueError("key onboarding needs the Open WebUI image pinned by digest")
    claim = next(v["persistentVolumeClaim"]["claimName"] for v in spec["volumes"] if v["name"] == "data")
    storage = next(o for o in objects if o["kind"] == "PersistentVolumeClaim" and o["metadata"]["name"] == claim)
    if storage["spec"]["accessModes"] != ["ReadWriteOnce"]:
        raise ValueError("key onboarding mounts the Open WebUI claim read-only beside Open WebUI: it must be ReadWriteOnce")
    labels = {LABEL: NAME}
    volumes = [{"name": "runtime", "configMap": {"name": NAME}},
               {"name": "webui", "persistentVolumeClaim": {"claimName": claim, "readOnly": True}}]
    mounts = [{"name": "runtime", "mountPath": "/runtime/key_onboarding.py", "subPath": "key_onboarding.py", "readOnly": True},
              {"name": "runtime", "mountPath": "/config/config.json", "subPath": "config.json", "readOnly": True},
              {"name": "webui", "mountPath": "/webui", "readOnly": True}]
    rules = [{"apiGroups": [""], "resources": ["secrets"], "resourceNames": SECRETS,
              "verbs": ["get", "update"] if live else ["get"]}]
    egress = [{"to": [{"ipBlock": {"cidr": sandbox["api_server_cidr"]}}],
               "ports": [{"protocol": "TCP", "port": sandbox["api_server_port"]}]}]
    extra = []
    if live:
        # Only the master key of the gateway's credentials; the database URL, salt and control keys stay out.
        volumes.append({"name": "master", "secret": {"secretName": "gateway-credentials", "defaultMode": 0o440,
                                                     "items": [{"key": "master-key", "path": "master-key"}]}})
        mounts.append({"name": "master", "mountPath": "/run/gateway", "readOnly": True})
        rules.append({"apiGroups": [""], "resources": ["configmaps"], "resourceNames": [STATUS], "verbs": ["get", "update"]})
        gateway = {"podSelector": {"matchLabels": {LABEL: "litellm"}}}
        egress.append({"to": [gateway], "ports": [{"protocol": "TCP", "port": 4000}]})
        extra = [obj("ConfigMap", STATUS, data={}),
                 obj("NetworkPolicy", "litellm-key-onboarding-ingress", "networking.k8s.io/v1", spec={
                     "podSelector": gateway["podSelector"], "policyTypes": ["Ingress"],
                     "ingress": [{"from": [{"podSelector": {"matchLabels": labels}}],
                                  "ports": [{"protocol": "TCP", "port": 4000}]}]})]
    pod = {"serviceAccountName": NAME, "automountServiceAccountToken": True, "enableServiceLinks": False,
           "restartPolicy": "Never", "nodeSelector": spec.get("nodeSelector", {}),
           # Open WebUI's own user and group: the volume root already has this group, so
           # OnRootMismatch changes nothing, and the mount is read-only besides.
           "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000, "fsGroup": 1000,
                               "fsGroupChangePolicy": "OnRootMismatch", "seccompProfile": {"type": "RuntimeDefault"}},
           "containers": [{"name": NAME, "image": image, "imagePullPolicy": "IfNotPresent",
                           # -I -S: no environment, user site or site-packages; the standard library only.
                           "command": ["python", "-I", "-S", "/runtime/key_onboarding.py"],
                           "resources": RESOURCES,
                           "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                               "capabilities": {"drop": ["ALL"]}},
                           "volumeMounts": mounts}],
           "volumes": volumes}
    return [
        obj("ServiceAccount", NAME, automountServiceAccountToken=False),
        obj("Role", NAME, "rbac.authorization.k8s.io/v1", rules=rules),
        obj("RoleBinding", NAME, "rbac.authorization.k8s.io/v1",
            roleRef={"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": NAME},
            subjects=[{"kind": "ServiceAccount", "name": NAME, "namespace": NAMESPACE}]),
        obj("ConfigMap", NAME, data={"config.json": json.dumps(job_config(mode, sandbox, models), indent=2) + "\n",
                                     "key_onboarding.py": (root / "infrastructure/service/key_onboarding.py").read_text()}),
        obj("NetworkPolicy", NAME, "networking.k8s.io/v1", spec={
            "podSelector": {"matchLabels": labels}, "policyTypes": ["Ingress", "Egress"], "ingress": [], "egress": egress}),
        *extra,
        obj("CronJob", NAME, "batch/v1", spec={
            "schedule": SCHEDULE, "concurrencyPolicy": "Forbid", "startingDeadlineSeconds": 120,
            "successfulJobsHistoryLimit": 1, "failedJobsHistoryLimit": 3,
            "jobTemplate": {"spec": {"backoffLimit": 0, "activeDeadlineSeconds": 240, "template": {
                "metadata": {"labels": labels}, "spec": pod}}}}),
    ]
