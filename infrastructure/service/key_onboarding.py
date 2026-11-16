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
import http.client
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
# Open WebUI's user ids are lower-case UUIDs. Anything else gets no key and no alias.
USER_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
TIERS = ("parents", "family")
MAPPING_SECRET, MAPPING_KEY = "gateway-browser-keys", "mapping.json"
IMAGE_SECRET, IMAGE_KEY = "gateway-image-permissions", "image-users.json"
STATUS_CONFIGMAP, STATUS_KEY = "key-onboarding-status", "status.json"
MAX_BODY = 1 << 20
# A run that changes anything first marks the mapping Secret; another run that finds a
# mark younger than this leaves the gateway alone. Longer than the Job's
# activeDeadlineSeconds (240) and shorter than the schedule (300), so a run that died
# does not hold up the next one.
CLAIM, CLAIM_SECONDS = "workstation.ai/key-onboarding-claim", 270
DAY = 86400
# Actions that issue a key: no usable key, a key near expiry, a key issued for another
# tier, a mapped key the gateway no longer accepts.
ISSUE = ("provision", "renew", "policy", "dead")


class Stop(Exception):
    """The run cannot continue safely. The code is a fixed reason without request data."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def read_config(path):
    config = json.loads(Path(path).read_text())
    keys = {"mode", "namespace", "gateway", "database", "parents_group_id", "family_group_id", "admin_policy", "policies",
            "renew_days", "retire_grace_seconds", "max_users", "home_user_ids", "home_tiers"}
    if not isinstance(config, dict) or set(config) != keys or config["mode"] not in ("dry-run", "live"):
        raise Stop("config_invalid")
    if set(config["policies"]) != set(TIERS) or config["parents_group_id"] == config["family_group_id"] \
            or config["admin_policy"] not in ("parents", "unmanaged"):
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
    retried. With Open WebUI stopped cleanly there is no WAL index to read through a
    read-only mount, so the run stops without changing anything.
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
    """{user id: tier} for active accounts; the most permissive rule wins.

    Parents get the parents tier and a member of Family only the family tier. An
    administrator gets the parents tier: in any group, and with `admin_policy: parents`
    in no group at all. A `pending` account cannot sign in, so it gets no key. Only
    direct members count.
    """
    parents = webui["members"].get(config["parents_group_id"], set())
    family = webui["members"].get(config["family_group_id"], set())
    admins = {user for user, role in webui["users"].items() if role == "admin"} if config["admin_policy"] == "parents" else set()
    result, malformed = {}, 0
    for user in parents | family | admins:
        role = webui["users"].get(user)
        if role not in ("user", "admin"):
            continue
        if not isinstance(user, str) or not USER_ID.match(user):
            malformed += 1
            continue
        result[user] = "parents" if user in parents or role == "admin" else "family"
    return result, malformed


def valid_key(key):
    return isinstance(key, str) and key.startswith("sk-") and len(key) >= 24


def valid_entry(entry):
    return isinstance(entry, dict) and valid_key(entry.get("key")) and type(entry.get("expires_at")) in (int, float)


def plan(config, webui, mapping, now):
    """The decision alone: ({user id: (action, tier)}, malformed member ids). Nothing is called.

    Actions: keep, provision, renew, policy, images, revoke, unmanaged. (`dead` is added
    by the live run, which can ask the gateway.)
    """
    # A read that cannot be right stops here. Both groups and the user table present
    # with nobody in the groups is a valid state: the last member left, and is revoked.
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
            # With `admin_policy: unmanaged`, an administrator outside both groups keeps what the owner bound by hand.
            actions[user] = ("unmanaged" if webui["users"].get(user) == "admin" else "revoke", None)
    return actions, malformed


def restrict(config, mapping, actions):
    """Take away what the plan no longer allows, without asking the gateway for anything.

    Runs before, and whether or not, a key is issued: a member's picture flag is set to
    the tier's, and a revoked account leaves the mapping. Returns the removed entries.
    """
    removed = {}
    for user, (action, tier) in actions.items():
        entry = mapping["users"].get(user)
        if action == "revoke":
            removed[user] = mapping["users"].pop(user)
        elif tier is not None and isinstance(entry, dict):
            entry["images"] = config["policies"][tier]["images"] if valid_entry(entry) else False
    return removed


def image_document(mapping):
    """What the image adapter may know: each usable entry's expiry and picture permission, no key."""
    return {"service_key": mapping["service_key"], "users": {user: {
        "expires_at": entry["expires_at"], "images": entry.get("images") is True}
        for user, entry in mapping["users"].items() if valid_entry(entry)}}


def narrowed(current, final):
    """The image document on the way to `final` that grants nothing `current` does not.

    Published before the mapping, so that a denial is never waiting behind a write
    that may fail. Grants arrive with the last write, after the mapping.
    """
    before = current.get("users") if isinstance(current, dict) and isinstance(current.get("users"), dict) else {}
    users = {}
    for user, entry in final["users"].items():
        old = before.get(user)
        if isinstance(old, dict) and type(old.get("expires_at")) in (int, float):
            users[user] = {"expires_at": min(entry["expires_at"], old["expires_at"]),
                           "images": entry["images"] and old.get("images") is True}
    return {"service_key": final["service_key"], "users": users}


def check_mapping(mapping):
    # An absent or unreadable mapping is never replaced: the owner's document is the base.
    if not isinstance(mapping, dict) or not isinstance(mapping.get("service_key"), str) \
            or len(mapping["service_key"]) < 32 or not isinstance(mapping.get("users"), dict):
        raise Stop("mapping_invalid")
    # retired: keys to delete once the mapping no longer names them. sweep: users who may
    # hold a key in an alias slot that the mapping does not name.
    retired, sweep = mapping.setdefault("retired", []), mapping.setdefault("sweep", [])
    if not isinstance(retired, list) or not all(
            isinstance(item, dict) and valid_key(item.get("key")) and type(item.get("after")) in (int, float)
            and isinstance(item.get("user", ""), str) for item in retired):
        raise Stop("mapping_invalid")
    if not isinstance(sweep, list) or not all(isinstance(user, str) and USER_ID.match(user) for user in sweep):
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
    except (OSError, ValueError, http.client.HTTPException):
        return 0, None


class Gateway:
    """The LiteLLM admin API with the master key, inside the cluster."""

    def __init__(self, base, master_key):
        self.base, self.key, self.opener = base.rstrip("/"), master_key, _opener()

    def call(self, path, payload):
        return _call(self.opener, "POST", self.base + path, self.key, payload)

    def accepts(self, key):
        """Whether the gateway still accepts a user's key: True, False (401), or None when unknown.

        The model list is the cheapest authenticated call; the key travels in the
        Authorization header only.
        """
        status, _ = _call(self.opener, "GET", self.base + "/v1/models", key)
        return True if status == 200 else False if status == 401 else None


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
    """The mapping as published: empty bookkeeping lists are left out."""
    return {name: value for name, value in mapping.items() if name not in ("retired", "sweep") or value}


def prefix(user):
    return user[:8]


def aliases(user):
    return [f"webui-{user}-a", f"webui-{user}-b"]


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
        first, second = aliases(user)
        alias = second if isinstance(entry, dict) and entry.get("alias") == first else first
        if call("/key/delete", {"key_aliases": [alias]})[0] not in (200, 404):
            return None
        status, result = call("/key/generate", {
            "user_id": user, "key_alias": alias, "models": policy["models"], "duration": f'{policy["days"]}d',
            "rpm_limit": policy["rpm"], "tpm_limit": policy["tpm"], "max_parallel_requests": policy["concurrency"]})
        try:
            key = result["key"]
            expires = datetime.datetime.fromisoformat(result["expires"].replace("Z", "+00:00"))
            expiry = expires.timestamp()
            if status != 200 or expires.tzinfo is None or not valid_key(key) or result.get("user_id") != user \
                    or not self.now < expiry <= self.now + (policy["days"] + 1) * DAY:
                return None
        except (AttributeError, KeyError, TypeError, ValueError):
            return None
        return {"key": key, "expires_at": expiry, "images": policy["images"], "alias": alias, "policy": tier}

    def held(self, secrets):
        """Whether another run's mark on the mapping Secret is still fresh."""
        annotations = secrets[MAPPING_SECRET]["metadata"].get("annotations") or {}
        try:
            return 0 <= self.now - float(annotations.get(CLAIM, "")) < CLAIM_SECONDS
        except ValueError:
            return False

    def claim(self, secrets, mapping):
        """Mark the mapping Secret as being changed by this run.

        Two runs at once (a Job started by hand beside the schedule) could each delete
        the key the other had just issued. The replace at the read resourceVersion lets
        exactly one of them set the mark. The same write publishes what this run has
        already taken away and records who may be about to get a key the mapping does
        not name yet (`sweep`), so a run that dies here leaves a trail.
        """
        secret = secrets[MAPPING_SECRET]
        annotations = secret["metadata"].get("annotations") or {}
        secret = dict(secret, metadata={**secret["metadata"], "annotations": {**annotations, CLAIM: str(int(self.now))}},
                      data={**(secret.get("data") or {}), MAPPING_KEY: self.encode(stored_mapping(mapping))})
        secrets[MAPPING_SECRET] = self.kube.put("secrets", MAPPING_SECRET, secret)

    @staticmethod
    def encode(document):
        return base64.b64encode(json.dumps(document).encode()).decode()

    def put_images(self, secrets, document):
        secret = secrets[IMAGE_SECRET]
        if secret_document(secret, IMAGE_KEY) != document:
            secrets[IMAGE_SECRET] = self.kube.put("secrets", IMAGE_SECRET, dict(
                secret, data={**(secret.get("data") or {}), IMAGE_KEY: self.encode(document)}))

    def write(self, mapping, secrets, release=False):
        """Publish the mapping, then the picture grants it justifies; `release` removes the mark.

        The image adapter authorises from its own document. The run has already taken
        from that document whatever the plan denies (run()); it gains only here, after
        the mapping. A failure in between leaves pictures denied.
        """
        secret = secrets[MAPPING_SECRET]
        annotations = secret["metadata"].get("annotations") or {}
        marked = release and CLAIM in annotations
        if marked or secret_document(secret, MAPPING_KEY) != stored_mapping(mapping):
            secret = dict(secret, data={**(secret.get("data") or {}), MAPPING_KEY: self.encode(stored_mapping(mapping))})
            if marked:
                secret["metadata"] = {**secret["metadata"], "annotations": {k: v for k, v in annotations.items() if k != CLAIM}}
            secrets[MAPPING_SECRET] = self.kube.put("secrets", MAPPING_SECRET, secret)
        self.put_images(secrets, image_document(mapping))

    def probe(self, mapping, actions):
        """Ask the gateway about every key that would otherwise be left as it is."""
        for user, (action, tier) in sorted(actions.items()):
            if action in ("keep", "images"):
                accepted = self.gateway.accepts(mapping["users"][user]["key"])
                if accepted is None:
                    return      # the gateway is not answering; nothing is concluded from that
                if accepted is False:
                    actions[user] = ("dead", tier)

    def run(self, webui):
        secrets = {name: self.kube.get("secrets", name) for name in (MAPPING_SECRET, IMAGE_SECRET)}
        mapping = check_mapping(secret_document(secrets[MAPPING_SECRET], MAPPING_KEY))
        actions, malformed = plan(self.config, webui, mapping, self.now)
        done = {}
        if self.live:
            self.probe(mapping, actions)
            issuing = sorted(user for user, (action, _) in actions.items() if action in ISSUE)
            removed = restrict(self.config, mapping, actions)
            for item in mapping["retired"]:
                if item.get("user") in removed:
                    item["after"] = self.now        # no grace for an account that is gone
            for user, entry in removed.items():
                if isinstance(entry, dict) and valid_key(entry.get("key")):
                    mapping["retired"].append({"key": entry["key"], "after": self.now, "user": user})
            mapping["sweep"] = sorted({*mapping["sweep"], *issuing, *(user for user in removed if USER_ID.match(user))})
            # Pictures this plan denies are denied first, whatever happens next and whoever holds the mark.
            self.put_images(secrets, narrowed(secret_document(secrets[IMAGE_SECRET], IMAGE_KEY), image_document(mapping)))
            changing = bool(issuing or mapping["sweep"] or any(item["after"] <= self.now for item in mapping["retired"]))
            if self.held(secrets) and (changing or secret_document(secrets[MAPPING_SECRET], MAPPING_KEY) != stored_mapping(mapping)):
                raise Stop("another_run_active")
            if changing:
                self.claim(secrets, mapping)
            for user in issuing:
                entry = mapping["users"].get(user)
                issued = self.issue(user, actions[user][1], entry)
                if issued:
                    if valid_entry(entry):
                        # The gateway may hold the old mapping until the kubelet refreshes it.
                        mapping["retired"].append({"key": entry["key"], "user": user,
                                                   "after": self.now + self.config["retire_grace_seconds"]})
                    mapping["users"][user] = issued
                done[user] = bool(issued)
            done.update({user: True for user, (action, _) in actions.items() if action in ("images", "revoke")})
            if changing:
                self.write(mapping, secrets)
                # Only now, with the mapping published, are keys it no longer names deleted. 404: already gone.
                delete = self.gateway.call
                mapping["retired"] = [item for item in mapping["retired"] if item["after"] > self.now
                                      or delete("/key/delete", {"keys": [item["key"]]})[0] not in (200, 404)]
                # Alias slots the mapping does not name are emptied: a key from a run that died
                # after issuing it, and both slots of an account that is gone. A user issued a
                # key just now is skipped: the other slot holds the key in its grace period.
                def swept(user):
                    entry = mapping["users"].get(user)
                    named = entry.get("alias") if isinstance(entry, dict) else None
                    return all(delete("/key/delete", {"key_aliases": [alias]})[0] in (200, 404)
                               for alias in aliases(user) if alias != named)
                mapping["sweep"] = [user for user in mapping["sweep"] if not (done.get(user) and user in issuing) and not swept(user)]
            # Also on a settled run: restores image permissions that no longer match the mapping.
            self.write(mapping, secrets, release=changing)
        return self.status(mapping, secrets, actions, malformed, done)

    def status(self, mapping, secrets, actions, malformed, done):
        counts = {name: 0 for name in ("keep", *ISSUE, "images", "revoke", "unmanaged")}
        for action, _ in actions.values():
            counts[action] += 1
        changes = {user: value for user, value in actions.items() if value[0] not in ("keep", "unmanaged")}
        failed = sum(not done.get(user, False) for user in changes) if self.live else 0
        soon = self.now + self.config["renew_days"] * DAY
        managed = [entry["expires_at"] for user, entry in mapping["users"].items()
                   if valid_entry(entry) and actions.get(user, ("", None))[0] != "unmanaged"]
        homes = set(self.config["home_user_ids"])
        owed = self.live and (mapping["sweep"] or any(item["after"] <= self.now for item in mapping["retired"]))
        return {
            "schema": SCHEMA, "mode": self.config["mode"],
            "time": datetime.datetime.fromtimestamp(self.now, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "result": "partial" if failed or owed else "ok", "code": None,
            "counts": {"members": sum(tier is not None for _, tier in actions.values()), **counts,
                       "failed": failed, "malformed_members": malformed, "retired_pending": len(mapping["retired"]),
                       "sweep_pending": len(mapping["sweep"])},
            "changes": [{"user": prefix(user), "action": action, "tier": tier,
                         "state": ("done" if done.get(user) else "failed") if self.live else "planned"}
                        for user, (action, tier) in sorted(changes.items())],
            # Whether mapped keys were checked against the gateway; a dry run has no route to it.
            "liveness": "checked" if self.live else "not_checked",
            # A member with a workspace but no home mapping: the workspace is disposable
            # until the owner creates the home (docs/ASSISTANT.md).
            "homes_needed": sorted(prefix(user) for user, (_, tier) in actions.items()
                                   if tier in self.config["home_tiers"] and user not in homes),
            "earliest_expiry_days": int((min(managed) - self.now) // DAY) if managed else None,
            # Hand-bound administrator keys this job will not renew (admin_policy: unmanaged).
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
