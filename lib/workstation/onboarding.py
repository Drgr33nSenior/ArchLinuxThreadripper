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
# Default key policy per tier; the site file may replace any value (`key_policies`).
# Parents (and administrators) run agents: the adult bounds of docs/AI-SERVICE.md step 6.
# Family is the children: half the request and token rate, no pictures. Two requests at
# once, not one: after each answer Open WebUI makes its follow-up, title and tag calls
# with the same key, one after another, and a child who sends the next message while
# they run would be refused with 429 at a limit of one.
POLICIES = {"parents": {"rpm": 60, "tpm": 2000000, "concurrency": 2, "days": 90, "images": True},
            "family": {"rpm": 30, "tpm": 1000000, "concurrency": 2, "days": 90, "images": False}}
# The gateway's bounds for any virtual key (service.KEY_LIMITS and the 90-day maximum; a test keeps them in step).
BOUNDS = {"rpm": 60, "tpm": 2000000, "concurrency": 2, "days": 90}
ADMIN_POLICIES = ("parents", "unmanaged")
# Unmeasured: a short standard-library Python process. Not counted by `service plan`.
RESOURCES = {"requests": {"cpu": "20m", "memory": "64Mi"}, "limits": {"cpu": "250m", "memory": "256Mi"}}
SECRETS = ["gateway-browser-keys", "gateway-image-permissions"]
STATUS = "key-onboarding-status"


def policies(overrides):
    """The two key policies: the defaults with the site's `key_policies` laid over them."""
    if not isinstance(overrides, dict) or set(overrides) - set(POLICIES):
        raise ValueError("key_policies may name parents and family only")
    result = {}
    for tier, default in POLICIES.items():
        change = overrides.get(tier, {})
        if not isinstance(change, dict) or set(change) - set(default):
            raise ValueError(f"key_policies.{tier} may set rpm, tpm, concurrency, days and images only")
        policy = {**default, **change}
        for name, bound in BOUNDS.items():
            if type(policy[name]) is not int or not 1 <= policy[name] <= bound:
                raise ValueError(f"key_policies.{tier}.{name} must be a whole number from 1 to {bound}")
        if type(policy["images"]) is not bool:
            raise ValueError(f"key_policies.{tier}.images must be true or false")
        if policy["days"] <= RENEW_DAYS:
            raise ValueError(f"key_policies.{tier}.days must be more than the {RENEW_DAYS}-day renewal window")
        result[tier] = policy
    return result


def job_config(mode, sandbox, models, key_policies=None, admin_policy="parents"):
    """What the job needs: group ids, key policies and who already has a home. No credential."""
    if admin_policy not in ADMIN_POLICIES:
        raise ValueError("admin_policy must be parents or unmanaged")
    return {"mode": mode, "namespace": NAMESPACE, "gateway": "http://litellm:4000", "database": "/webui/webui.db",
            "parents_group_id": sandbox["parents_group_id"], "family_group_id": sandbox["family_group_id"],
            "admin_policy": admin_policy,
            "policies": {tier: {"models": sorted(models), **policy} for tier, policy in policies(key_policies or {}).items()},
            "renew_days": RENEW_DAYS, "retire_grace_seconds": RETIRE_GRACE_SECONDS, "max_users": MAX_USERS,
            "home_user_ids": sorted(user["webui_user_id"] for user in sandbox["users"]),
            # Who gets a workspace at all: Parents always, Family when the sandbox admits them.
            "home_tiers": ["parents"] + (["family"] if sandbox["family_enabled"] else [])}


def obj(kind, name, api="v1", **fields):
    return {"apiVersion": api, "kind": kind, "metadata": {"name": name, "namespace": NAMESPACE}, **fields}


def render(mode, sandbox, objects, models, root, key_policies=None, admin_policy="parents"):
    """The onboarding bundle for a rendered assistant composition; empty when off.

    `dry-run` carries no admin credential and no write permission at all: it reads the
    two Secrets and the Open WebUI database and prints what it would do. `live` adds the
    master key, the right to replace the two Secrets and the status ConfigMap, and the
    flow to the gateway.
    """
    if mode not in MODES:
        raise ValueError("key_onboarding must be off, dry-run or live")
    policies(key_policies or {})    # a wrong site value is refused whether or not the job is on
    if admin_policy not in ADMIN_POLICIES:
        raise ValueError("admin_policy must be parents or unmanaged")
    if mode == "off":
        return []
    live = mode == "live"
    webui = next(o for o in objects if o["kind"] == "Deployment" and o["metadata"]["name"] == "open-webui")
    spec = webui["spec"]["template"]["spec"]
    # The job runs from the Open WebUI image, as its user: the same SQLite library as the
    # database's only writer, already on the node. Nothing of Open WebUI itself is started.
    image = spec["containers"][0]["image"]
    # Group membership issues a key with no further step, so only the owner may create accounts.
    if {"name": "ENABLE_SIGNUP", "value": "false"} not in spec["containers"][0].get("env", []):
        raise ValueError("key onboarding needs Open WebUI sign-up closed (ENABLE_SIGNUP=false)")
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
        obj("ConfigMap", NAME, data={"config.json": json.dumps(job_config(mode, sandbox, models, key_policies, admin_policy), indent=2) + "\n",
                                     "key_onboarding.py": (root / "infrastructure/service/key_onboarding.py").read_text()}),
        obj("NetworkPolicy", NAME, "networking.k8s.io/v1", spec={
            "podSelector": {"matchLabels": labels}, "policyTypes": ["Ingress", "Egress"], "ingress": [], "egress": egress}),
        *extra,
        obj("CronJob", NAME, "batch/v1", spec={
            "schedule": SCHEDULE, "concurrencyPolicy": "Forbid", "startingDeadlineSeconds": 120,
            "successfulJobsHistoryLimit": 1, "failedJobsHistoryLimit": 3,
            # Finished Jobs go after 20 minutes, so the Failed pods that exist at any moment are
            # recent failures: what the WorkstationKeyOnboardingFailing alert counts.
            "jobTemplate": {"spec": {"backoffLimit": 0, "activeDeadlineSeconds": 240, "ttlSecondsAfterFinished": 1200,
                                     "template": {
                "metadata": {"labels": labels}, "spec": pod}}}}),
    ]
