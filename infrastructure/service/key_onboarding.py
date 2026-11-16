"""Family onboarding: browser gateway keys follow Open WebUI group membership.

The `key-onboarding` CronJob runs this file (lib/workstation/onboarding.py renders it;
docs/AI-SERVICE.md#automatic-family-onboarding describes it). Each run reads the Open
WebUI users and group members read-only, compares them with the browser mapping Secret,
and issues, renews or revokes LiteLLM virtual keys so that every member of Parents or
Family has a valid key and nobody else has one.

Standard library only. Output is counts, fixed codes and user-id prefixes: never a key,
a Secret document, a response body or exception text.
"""
import base64
import datetime
import json
import os
from pathlib import Path
import re
import sqlite3
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SCHEMA = "workstation-key-onboarding/1"
USER_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
TIERS = ("parents", "family")
MAPPING_SECRET, MAPPING_KEY = "gateway-browser-keys", "mapping.json"
IMAGE_SECRET, IMAGE_KEY = "gateway-image-permissions", "image-users.json"
STATUS_CONFIGMAP, STATUS_KEY = "key-onboarding-status", "status.json"
MAX_BODY = 1 << 20
# A run that changes anything first marks the mapping Secret; another run that finds a
# mark younger than this stops. Longer than the Job's activeDeadlineSeconds, so a run
# that died holds nobody up for more than one schedule.
CLAIM, CLAIM_SECONDS = "workstation.ai/key-onboarding-claim", 300
DAY = 86400
# Actions that issue a key: no usable key, a key near expiry, a key issued for another tier.
ISSUE = ("provision", "renew", "policy")


class Stop(Exception):
    """The run cannot continue safely. The code is a fixed reason without request data."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def read_config(path):
    config = json.loads(Path(path).read_text())
    keys = {"mode", "namespace", "gateway", "database", "parents_group_id", "family_group_id", "policies",
            "renew_days", "retire_grace_seconds", "max_users", "home_user_ids", "home_tiers"}
    if not isinstance(config, dict) or set(config) != keys or config["mode"] not in ("dry-run", "live"):
        raise Stop("config_invalid")
    if set(config["policies"]) != set(TIERS) or config["parents_group_id"] == config["family_group_id"]:
        raise Stop("config_invalid")
    for policy in config["policies"].values():
        if set(policy) != {"models", "rpm", "tpm", "concurrency", "days", "images"} or type(policy["images"]) is not bool \
                or not policy["models"] or config["renew_days"] >= policy["days"]:
            raise Stop("config_invalid")
    return config


def read_webui(path, attempts=5, delay=2.0):
    """Users, roles and direct group members from one read transaction.

    Open WebUI keeps the database in WAL mode and stays the only writer. The read-only
    URI plus `query_only` cannot change the file; a busy or mid-checkpoint database is
    retried. With Open WebUI stopped there is no WAL index to read through a read-only
    mount, so the run stops without changing anything.
    """
    uri = "file:" + urllib.parse.quote(str(path)) + "?mode=ro"
    for attempt in range(attempts):
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=5, isolation_level=None)
            try:
                connection.execute("PRAGMA query_only = ON")
                connection.execute("BEGIN")
                users = connection.execute('SELECT id, role FROM "user"').fetchall()
                groups = connection.execute('SELECT id FROM "group"').fetchall()
                members = connection.execute("SELECT group_id, user_id FROM group_member").fetchall()
            finally:
                connection.close()
            result = {"users": dict(users), "groups": {row[0] for row in groups}, "members": {}}
            for group, user in members:
                result["members"].setdefault(group, set()).add(user)
            return result
        except sqlite3.Error:
            if attempt + 1 < attempts:
                time.sleep(delay)
    raise Stop("webui_db_unavailable")


def eligible(config, webui):
    """{user id: tier} for active members; the most permissive group wins.

    Parents, and an administrator who is in either group, get the parents tier (pictures
    are for Admin and Parents). A member of Family only gets the family tier. A `pending`
    account cannot sign in, so it gets no key.
    """
    parents = webui["members"].get(config["parents_group_id"], set())
    family = webui["members"].get(config["family_group_id"], set())
    result, malformed = {}, 0
    for user in parents | family:
        role = webui["users"].get(user)
        if role not in ("user", "admin"):
            continue
        if not isinstance(user, str) or not USER_ID.match(user):
            malformed += 1
            continue
        result[user] = "parents" if user in parents or role == "admin" else "family"
    return result, malformed


def valid_entry(entry):
    return (isinstance(entry, dict) and isinstance(entry.get("key"), str) and entry["key"].startswith("sk-")
            and len(entry["key"]) >= 24 and type(entry.get("expires_at")) in (int, float))


def plan(config, webui, mapping, now):
    """The decision alone: ({user id: (action, tier)}, malformed member ids). Nothing is called.

    Actions: keep, provision, renew, policy, images, revoke, unmanaged.
    """
    for group in ("parents_group_id", "family_group_id"):
        if config[group] not in webui["groups"]:
            raise Stop("group_missing")
    if not webui["users"]:
        raise Stop("no_users")
    members, malformed = eligible(config, webui)
    if len(members) > config["max_users"]:
        raise Stop("too_many_users")
    actions = {}
    for user, tier in members.items():
        entry = mapping["users"].get(user)
        if not valid_entry(entry):
            action = "provision"
        elif entry["expires_at"] - now < config["renew_days"] * DAY:
            action = "renew"
        elif entry.get("policy") in TIERS and entry["policy"] != tier:
            action = "policy"
        elif entry.get("images") is not config["policies"][tier]["images"]:
            action = "images"
        else:
            action = "keep"
        actions[user] = (action, tier)
    for user in mapping["users"]:
        if user not in members:
            # An administrator outside both groups keeps whatever the owner bound by hand.
            actions[user] = ("unmanaged" if webui["users"].get(user) == "admin" else "revoke", None)
    revoked = sum(action == "revoke" for action, _ in actions.values())
    if revoked and not members:
        # Empty groups with keys to revoke look like a broken read more than a decision.
        raise Stop("no_eligible_members")
    return actions, malformed


def image_document(mapping):
    """What the image adapter may know: each user's expiry and picture permission, no key."""
    return {"service_key": mapping["service_key"], "users": {user: {
        "expires_at": entry["expires_at"], "images": entry.get("images", False)}
        for user, entry in mapping["users"].items() if isinstance(entry, dict) and "expires_at" in entry}}


def check_mapping(mapping):
    # An absent or unreadable mapping is never replaced: the owner's document is the base.
    if not isinstance(mapping, dict) or not isinstance(mapping.get("service_key"), str) \
            or len(mapping["service_key"]) < 32 or not isinstance(mapping.get("users"), dict):
        raise Stop("mapping_invalid")
    retired = mapping.setdefault("retired", [])
    if not isinstance(retired, list) or not all(
            isinstance(item, dict) and isinstance(item.get("key"), str) and type(item.get("after")) in (int, float)
            for item in retired):
        raise Stop("mapping_invalid")
    return mapping


def _opener(context=None):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        # A credential must never follow a redirect.
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    handlers = [urllib.request.ProxyHandler({}), NoRedirect()]
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    return urllib.request.build_opener(*handlers)


def _call(opener, method, url, token, payload=None):
    """(status, document). A refusal is its status; a transport or parse failure is 0."""
    request = urllib.request.Request(url, method=method, data=json.dumps(payload).encode() if payload is not None else None,
                                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                                              "Accept": "application/json"})
    try:
        with opener.open(request, timeout=20) as response:
            data = response.read(MAX_BODY + 1)
            if len(data) > MAX_BODY:
                return 0, None
            return response.status, json.loads(data)
    except urllib.error.HTTPError as error:
        error.close()
        return error.code, None
    except (OSError, ValueError):
        return 0, None


class Gateway:
    """The LiteLLM admin API with the master key, inside the cluster."""

    def __init__(self, base, master_key):
        self.base, self.key, self.opener = base.rstrip("/"), master_key, _opener()

    def call(self, path, payload):
        return _call(self.opener, "POST", self.base + path, self.key, payload)


class Kube:
    """Named Secrets and ConfigMaps of one namespace, with this pod's ServiceAccount."""

    ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")

    def __init__(self, namespace, base=None, token=None, context=None):
        if base is None:
            base = f'https://{os.environ["KUBERNETES_SERVICE_HOST"]}:{os.environ["KUBERNETES_SERVICE_PORT"]}'
            token = (self.ACCOUNT / "token").read_text().strip()
            context = ssl.create_default_context(cafile=str(self.ACCOUNT / "ca.crt"))
        self.base, self.token, self.opener = f"{base}/api/v1/namespaces/{namespace}", token, _opener(context)

    def get(self, kind, name):
        status, value = _call(self.opener, "GET", f"{self.base}/{kind}/{name}", self.token)
        if status != 200 or not isinstance(value, dict):
            raise Stop("kubernetes_read_failed")
        return value

    def put(self, kind, name, value):
        """Replace at the read resourceVersion: a concurrent change is refused, never overwritten."""
        status, stored = _call(self.opener, "PUT", f"{self.base}/{kind}/{name}", self.token, value)
        if status != 200 or not isinstance(stored, dict):
            raise Stop("kubernetes_conflict" if status == 409 else "kubernetes_write_failed")
        return stored


def secret_document(secret, key):
    """The JSON document under one Secret key, or None when it is absent or unreadable."""
    try:
        return json.loads(base64.b64decode(secret["data"][key], validate=True))
    except (KeyError, TypeError, ValueError):
        return None


def stored_mapping(mapping):
    """The mapping as published: an empty retirement list is left out."""
    return {name: value for name, value in mapping.items() if name != "retired" or value}


def prefix(user):
    return user[:8]


class Reconciler:
    def __init__(self, config, kube, gateway, now=None):
        self.config, self.kube, self.gateway = config, kube, gateway
        self.now = time.time() if now is None else now
        self.live = config["mode"] == "live"

    def issue(self, user, tier, entry):
        """A new key for the exact WebUI user id, or None. The old key is not touched here."""
        policy, call = self.config["policies"][tier], self.gateway.call
        # 409: the gateway already knows this user id.
        if call("/user/new", {"user_id": user, "user_role": "internal_user", "auto_create_key": False})[0] not in (200, 409):
            return None
        # Two alias slots per user. The free one may still hold a key from a run that
        # stopped before the mapping was written; nobody holds its value, so it is removed.
        current = entry.get("alias") if isinstance(entry, dict) else None
        alias = f"webui-{user}-" + ("b" if current == f"webui-{user}-a" else "a")
        if call("/key/delete", {"key_aliases": [alias]})[0] not in (200, 404):
            return None
        status, result = call("/key/generate", {
            "user_id": user, "key_alias": alias, "models": policy["models"], "duration": f'{policy["days"]}d',
            "rpm_limit": policy["rpm"], "tpm_limit": policy["tpm"], "max_parallel_requests": policy["concurrency"]})
        try:
            key = result["key"]
            expires = datetime.datetime.fromisoformat(result["expires"].replace("Z", "+00:00"))
            expiry = expires.timestamp()
            if status != 200 or expires.tzinfo is None or not key.startswith("sk-") or len(key) < 24 or result.get("user_id") != user \
                    or not self.now < expiry <= self.now + (policy["days"] + 1) * DAY:
                return None
        except (AttributeError, KeyError, TypeError, ValueError):
            return None
        return {"key": key, "expires_at": expiry, "images": policy["images"], "alias": alias, "policy": tier}

    def claim(self, secrets):
        """Mark the mapping Secret as being changed by this run, or stop if another run holds it.

        Two runs at once (a Job started by hand beside the schedule) could each delete
        the key the other had just issued. The replace at the read resourceVersion lets
        exactly one of them set the mark.
        """
        secret = secrets[MAPPING_SECRET]
        annotations = secret["metadata"].get("annotations") or {}
        try:
            if 0 <= self.now - float(annotations.get(CLAIM, "")) < CLAIM_SECONDS:
                raise Stop("another_run_active")
        except ValueError:
            pass
        secret = dict(secret, metadata={**secret["metadata"], "annotations": {**annotations, CLAIM: str(int(self.now))}})
        secrets[MAPPING_SECRET] = self.kube.put("secrets", MAPPING_SECRET, secret)

    def write(self, mapping, secrets, release=False):
        """Publish the mapping, then the image permissions derived from it; `release` removes the mark."""
        for name, key, document in ((MAPPING_SECRET, MAPPING_KEY, stored_mapping(mapping)),
                                    (IMAGE_SECRET, IMAGE_KEY, image_document(mapping))):
            secret = secrets[name]
            annotations = secret["metadata"].get("annotations") or {}
            marked = release and CLAIM in annotations
            if marked or secret_document(secret, key) != document:
                secret = dict(secret, data={**(secret.get("data") or {}),
                                            key: base64.b64encode(json.dumps(document).encode()).decode()})
                if marked:
                    secret["metadata"] = {**secret["metadata"],
                                          "annotations": {k: v for k, v in annotations.items() if k != CLAIM}}
                secrets[name] = self.kube.put("secrets", name, secret)

    def run(self, webui):
        secrets = {name: self.kube.get("secrets", name) for name in (MAPPING_SECRET, IMAGE_SECRET)}
        mapping = check_mapping(secret_document(secrets[MAPPING_SECRET], MAPPING_KEY))
        actions, malformed = plan(self.config, webui, mapping, self.now)
        done = {}
        due = any(item["after"] <= self.now for item in mapping["retired"])
        changing = due or any(action not in ("keep", "unmanaged") for action, _ in actions.values())
        if self.live and changing:
            self.claim(secrets)
            for user, (action, tier) in sorted(actions.items()):
                entry = mapping["users"].get(user)
                if action in ISSUE:
                    issued = self.issue(user, tier, entry)
                    if issued and valid_entry(entry):
                        # The gateway may hold the old mapping until the kubelet refreshes it.
                        mapping["retired"].append({"key": entry["key"], "after": self.now + self.config["retire_grace_seconds"]})
                    if issued:
                        mapping["users"][user] = issued
                    done[user] = bool(issued)
                elif action == "images":
                    entry["images"] = self.config["policies"][tier]["images"]
                    done[user] = True
                elif action == "revoke":
                    del mapping["users"][user]
                    if isinstance(entry, dict) and isinstance(entry.get("key"), str) and entry["key"].startswith("sk-"):
                        mapping["retired"].append({"key": entry["key"], "after": self.now})
                    done[user] = True
            self.write(mapping, secrets)
            # Only after the mapping no longer names a key is that key deleted. 404: already gone.
            kept = [item for item in mapping["retired"] if item["after"] > self.now
                    or self.gateway.call("/key/delete", {"keys": [item["key"]]})[0] not in (200, 404)]
            mapping["retired"] = kept
        if self.live:
            # Also on a settled run: restores image permissions that no longer match the mapping.
            self.write(mapping, secrets, release=changing)
        return self.status(webui, mapping, secrets, actions, malformed, done)

    def status(self, webui, mapping, secrets, actions, malformed, done):
        counts = {name: 0 for name in ("keep", *ISSUE, "images", "revoke", "unmanaged")}
        for action, _ in actions.values():
            counts[action] += 1
        changes = {user: value for user, value in actions.items() if value[0] not in ("keep", "unmanaged")}
        failed = sum(not done.get(user, False) for user in changes) if self.live else 0
        soon = self.now + self.config["renew_days"] * DAY
        managed = [entry["expires_at"] for user, entry in mapping["users"].items()
                   if valid_entry(entry) and actions.get(user, ("", None))[0] != "unmanaged"]
        homes = set(self.config["home_user_ids"])
        return {
            "schema": SCHEMA, "mode": self.config["mode"],
            "time": datetime.datetime.fromtimestamp(self.now, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "result": "partial" if failed or (self.live and mapping["retired"] and any(
                item["after"] <= self.now for item in mapping["retired"])) else "ok", "code": None,
            "counts": {"members": sum(tier is not None for _, tier in actions.values()), **counts,
                       "failed": failed, "malformed_members": malformed, "retired_pending": len(mapping["retired"])},
            "changes": [{"user": prefix(user), "action": action, "tier": tier,
                         "state": ("done" if done.get(user) else "failed") if self.live else "planned"}
                        for user, (action, tier) in sorted(changes.items())],
            # A member with a workspace but no home mapping: the workspace is disposable
            # until the owner creates the home (docs/ASSISTANT.md).
            "homes_needed": sorted(prefix(user) for user, (_, tier) in actions.items()
                                   if tier in self.config["home_tiers"] and user not in homes),
            "earliest_expiry_days": int((min(managed) - self.now) // DAY) if managed else None,
            # Hand-bound administrator keys this job will not renew.
            "unmanaged_expiring": sum(1 for user, (action, _) in actions.items() if action == "unmanaged"
                                      and not (valid_entry(mapping["users"][user])
                                               and mapping["users"][user]["expires_at"] > soon)),
            "image_permissions_in_step": secret_document(secrets[IMAGE_SECRET], IMAGE_KEY) == image_document(mapping),
        }


def publish(kube, status):
    configmap = kube.get("configmaps", STATUS_CONFIGMAP)
    configmap["data"] = {STATUS_KEY: json.dumps(status, indent=2) + "\n"}
    kube.put("configmaps", STATUS_CONFIGMAP, configmap)


def main(config_path="/config/config.json", master_path="/run/gateway/master-key", kube=None, gateway=None,
         now=None, output=sys.stdout):
    config = kube_client = None
    try:
        config = read_config(config_path)
        kube_client = kube or Kube(config["namespace"])
        if config["mode"] == "live" and gateway is None:
            gateway = Gateway(config["gateway"], Path(master_path).read_text().strip())
        status = Reconciler(config, kube_client, gateway, now).run(read_webui(config["database"]))
    except Stop as stop:
        status = {"schema": SCHEMA, "mode": config["mode"] if config else None, "result": "stopped", "code": stop.code}
    except Exception as error:  # noqa: BLE001 - exception text may carry request data; only its class name leaves.
        status = {"schema": SCHEMA, "mode": config["mode"] if config else None, "result": "stopped", "code": "internal_error",
                  "exception": type(error).__name__}
    if config and config["mode"] == "live" and kube_client:
        try:
            publish(kube_client, status)
        except Exception:  # noqa: BLE001
            status = {**status, "result": "partial" if status["result"] == "ok" else status["result"],
                      "code": status.get("code") or "status_not_published"}
    print(json.dumps(status, sort_keys=True), file=output)
    return 0 if status["result"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
