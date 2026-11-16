"""Checks for sandbox-golive.sh. Reads local files only; never contacts the cluster.

Usage: golive_check.py ACTION ARGS...

  kdiff LIVE MERGED            KUBECTL_EXTERNAL_DIFF target: writes the changed paths per object
                               as JSON to $GOLIVE_DIFF_OUT; exit 1 when anything differs
  allow DIFF PROFILE [ENV...]  refuse any change outside the profile's allowlist
                               (webui: only these env values of open-webui; sandbox; homes)
  bundle SANDBOX_YAML EVIDENCE CONFIG USERS     static checks of the rendered sandbox bundle
  namespace SANDBOX_YAML OUT   the Namespace object alone (applied first)
  pick DEPLOYMENTS_JSON NAME OUT   one Deployment from a render's apply.json
  drift WORKLOADS_YAML LIVE_JSON   informational: other Deployments the next mode switch changes
  capacity CAPACITY_JSON       print the plan's figures; exit 1 unless it fits
  probe GROUP CONFIG [OTHER_POD_IP]   the network probe spec of docs/ASSISTANT.md for one group;
                               needs the site values NAS_ADDRESS and WORKSTATION_LAN_ADDRESS
                               in the environment (sandbox-golive.sh exports them)
  webui DEPLOYMENT_JSON CONFIG  the live open-webui offers the sandbox connection to the right groups
"""
import json
import os
import sys
from pathlib import Path

import yaml

NAMESPACE = "assistant-sandbox"
FOREIGN = {("NetworkPolicy", "ai-home-lab", "webui-sandbox"),
           ("NetworkPolicy", "workstation-observability", "alloy-sandbox")}
NOISE = {("metadata", "generation"), ("metadata", "resourceVersion"), ("metadata", "managedFields"),
         ("metadata", "creationTimestamp"), ("metadata", "uid"), ("status",)}
LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"


def keyed(value):
    """Lists of named dicts (containers, env, volumes, ports) compare by name, not position."""
    if isinstance(value, dict):
        return {k: keyed(v) for k, v in value.items()}
    if isinstance(value, list) and value and all(isinstance(i, dict) and "name" in i for i in value):
        return {f'[{i["name"]}]': keyed(i) for i in value}
    if isinstance(value, list):
        return [keyed(i) for i in value]
    return value


def paths(old, new, prefix=()):
    if isinstance(old, dict) and isinstance(new, dict):
        result = []
        for key in sorted(set(old) | set(new), key=str):
            result += paths(old.get(key), new.get(key), prefix + (key,))
        return result
    return [] if old == new else [prefix]


def clean(obj):
    obj = dict(obj or {})
    obj.pop("status", None)
    meta = dict(obj.get("metadata") or {})
    for key in ("generation", "resourceVersion", "managedFields", "creationTimestamp", "uid"):
        meta.pop(key, None)
    annotations = dict(meta.get("annotations") or {})
    annotations.pop(LAST_APPLIED, None)
    if annotations:
        meta["annotations"] = annotations
    else:
        meta.pop("annotations", None)
    obj["metadata"] = meta
    return keyed(obj)


def ident(obj):
    meta = obj.get("metadata") or {}
    return obj.get("kind", "?"), meta.get("namespace", ""), meta.get("name", "?")


def kdiff(live_dir, merged_dir):
    report = []
    names = sorted(set(os.listdir(live_dir)) | set(os.listdir(merged_dir)))
    def load(directory, name):
        path = Path(directory) / name
        return yaml.safe_load(path.read_text()) if path.exists() and path.stat().st_size else None

    for name in names:
        live, merged = load(live_dir, name), load(merged_dir, name)
        obj = merged or live
        kind, namespace, object_name = ident(obj)
        entry = {"kind": kind, "namespace": namespace, "name": object_name}
        if live is None:
            entry["state"] = "added"
        elif merged is None:
            entry["state"] = "removed"
        else:
            changed = paths(clean(live), clean(merged))
            if not changed:
                continue
            entry.update(state="changed", paths=[".".join(str(p) for p in path) for path in changed])
        report.append(entry)
    Path(os.environ["GOLIVE_DIFF_OUT"]).write_text(json.dumps(report, indent=1) + "\n")
    return 1 if report else 0


def allow(diff_path, profile, env_names):
    report = json.loads(Path(diff_path).read_text()) if Path(diff_path).stat().st_size else []
    bad = []
    for entry in report:
        key = (entry["kind"], entry["namespace"], entry["name"])
        label = f'{entry["kind"]} {entry["namespace"] + "/" if entry["namespace"] else ""}{entry["name"]}'
        if entry["state"] == "removed":
            bad.append(f"{label}: removed")
            continue
        if profile == "webui":
            allowed = {f"spec.template.spec.containers.[open-webui].env.[{name}].value" for name in env_names}
            if key != ("Deployment", "ai-home-lab", "open-webui") or entry["state"] != "changed":
                bad.append(f'{label}: {entry["state"]}, outside the WebUI allowlist')
                continue
            for path in entry["paths"]:
                print(f"  {label}: {path}" + ("" if path in allowed else "   <- NOT ALLOWED"))
                if path not in allowed:
                    bad.append(f"{label}: {path}")
        elif profile == "sandbox":
            ok = (entry["namespace"] == NAMESPACE or key == ("Namespace", "", NAMESPACE)
                  or (key in FOREIGN and entry["state"] == "added"))
            print(f'  {label}: {entry["state"]}' + ("" if ok else "   <- NOT ALLOWED"))
            if not ok:
                bad.append(f'{label}: {entry["state"]}')
        elif profile == "homes":
            ok = entry["name"].startswith("sandbox-home-") and entry["state"] == "added" and (
                (entry["kind"] == "PersistentVolume" and not entry["namespace"])
                or (entry["kind"] == "PersistentVolumeClaim" and entry["namespace"] == NAMESPACE))
            print(f'  {label}: {entry["state"]}' + ("" if ok else "   <- NOT ALLOWED (homes are added once, never changed here)"))
            if not ok:
                bad.append(f'{label}: {entry["state"]}')
        else:
            raise SystemExit(f"unknown profile {profile}")
    if profile == "webui" and not report:
        print("  open-webui: no change (already rolled out?)")
    for line in bad:
        print(f"REFUSED: {line}")
    return 1 if bad else 0


# The sandbox pods' account (workstation sandbox.SANDBOX_UID): never 1000, the owner's login.
SANDBOX_UID = 2002


def load_all(path):
    return [d for d in yaml.safe_load_all(Path(path).read_text()) if d]


def bundle(sandbox_yaml, evidence_path, config_path, users_path):
    objects = load_all(sandbox_yaml)
    config = json.loads(Path(config_path).read_text())
    users = json.loads(Path(users_path).read_text())["users"]
    evidence = json.loads(Path(evidence_path).read_text())
    problems = []
    sandbox = evidence.get("sandbox", {})
    want = {"enabled": True, "family_enabled": config["family_enabled"],
            "group_resolution": config["group_resolution"], "persistent_homes": len(users)}
    for key, value in want.items():
        if sandbox.get(key) != value:
            problems.append(f"evidence.json sandbox.{key} is {sandbox.get(key)!r}, expected {value!r}")
    if evidence.get("mode") != "shared":
        problems.append(f'the render is mode {evidence.get("mode")!r}, not shared (family)')
    counts = {}
    for obj in objects:
        kind, namespace, name = ident(obj)
        counts[kind] = counts.get(kind, 0) + 1
        if not (namespace == NAMESPACE or (kind, namespace, name) in FOREIGN or (kind, name) == ("Namespace", NAMESPACE)):
            problems.append(f"unexpected object {kind} {namespace}/{name}")
        if kind == "Deployment":
            if obj["spec"].get("replicas") != 0:
                problems.append(f"Deployment {name} is not rendered at zero replicas")
            for container in obj["spec"]["template"]["spec"]["containers"]:
                if container["image"] not in (config["image"], config["egress_image"]):
                    problems.append(f"Deployment {name} runs an image that is not in sandbox.json")
        if kind == "Service" and name.startswith("sandbox-egress-"):
            policy = name.removeprefix("sandbox-egress-")
            if obj["spec"].get("clusterIP") != config["egress_service_ips"].get(policy):
                problems.append(f"Service {name} address differs from sandbox.json")
        if kind == "Deployment" and name == "assistant-terminals":
            spec = obj["spec"]["template"]["spec"]
            ids = {spec["containers"][0]["securityContext"].get(k) for k in ("runAsUser", "runAsGroup")}
            if ids | {spec.get("securityContext", {}).get("fsGroup")} != {SANDBOX_UID}:
                problems.append(f"the controller does not run as UID/GID/fsGroup {SANDBOX_UID}")
        if kind == "ConfigMap" and name == "sandbox-config":
            for policy_name, policy in json.loads(obj["data"]["policies.json"]).items():
                pod, container = policy["pod_security_context"], policy["container_security_context"]
                if {pod.get("runAsUser"), pod.get("runAsGroup"), pod.get("fsGroup"),
                        container.get("runAsUser"), container.get("runAsGroup")} != {SANDBOX_UID}:
                    problems.append(f"workspace policy {policy_name} does not run as UID/GID/fsGroup {SANDBOX_UID}")
        if kind == "Namespace" and obj["metadata"].get("labels", {}).get("pod-security.kubernetes.io/enforce") != "restricted":
            problems.append("the namespace does not enforce the restricted Pod Security level")
    deployments = sorted(ident(o)[2] for o in objects if o["kind"] == "Deployment")
    if deployments != ["assistant-terminals", "sandbox-egress-family", "sandbox-egress-parents"]:
        problems.append(f"unexpected Deployments {deployments}")
    print("  sandbox.yaml: " + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())))
    print(f'  evidence: mode {evidence.get("mode")}, sandbox {json.dumps({k: sandbox.get(k) for k in want})}')
    for line in problems:
        print(f"REFUSED: {line}")
    return 1 if problems else 0


def namespace(sandbox_yaml, out):
    found = [o for o in load_all(sandbox_yaml) if o["kind"] == "Namespace"]
    if len(found) != 1 or found[0]["metadata"]["name"] != NAMESPACE:
        raise SystemExit("the render has no single assistant-sandbox Namespace")
    Path(out).write_text(yaml.safe_dump(found[0], sort_keys=False))
    return 0


def pick(list_path, name, out):
    items = json.loads(Path(list_path).read_text())["items"]
    found = [o for o in items if o["kind"] == "Deployment" and o["metadata"]["name"] == name]
    if len(found) != 1:
        raise SystemExit(f"the render holds {len(found)} Deployments named {name}")
    Path(out).write_text(json.dumps(found[0], indent=1) + "\n")
    print(f'  {name}: replicas {found[0]["spec"].get("replicas")} (live count, set by the mode render)')
    return 0


def drift(workloads_yaml, live_json):
    live = {d["metadata"]["name"]: d for d in json.loads(Path(live_json).read_text())["items"]}
    rendered = {o["metadata"]["name"]: o for o in load_all(workloads_yaml) if o["kind"] == "Deployment"}
    def view(dep):
        spec = dep["spec"]["template"]["spec"]
        return {c["name"]: {"image": c.get("image"), "resources": c.get("resources", {}),
                            "env": sorted(e["name"] for e in c.get("env", []))} for c in spec["containers"]}
    lines = []
    for name, dep in sorted(rendered.items()):
        if name == "open-webui" or name not in live:
            continue
        old, new = view(live[name]), view(dep)
        if old != new:
            for container in sorted(set(old) | set(new)):
                a, b = old.get(container, {}), new.get(container, {})
                for field in ("image", "resources", "env"):
                    if a.get(field) != b.get(field):
                        if field == "env":
                            added = sorted(set(b.get("env", [])) - set(a.get("env", [])))
                            removed = sorted(set(a.get("env", [])) - set(b.get("env", [])))
                            lines.append(f"  {name}/{container}: env names +{added} -{removed}")
                        else:
                            lines.append(f"  {name}/{container}: {field} {json.dumps(a.get(field))} -> {json.dumps(b.get(field))}")
    missing = sorted(set(rendered) - set(live))
    if missing:
        lines.append(f"  not live yet: {missing}")
    print("\n".join(lines) if lines else "  none")
    return 0


def capacity(path):
    plan = json.loads(Path(path).read_text())
    rows = {r["name"]: r for r in plan["rows"]}
    pool = rows.get("assistant-sandbox-pool")
    print(f'  status {plan["status"]}, fits {plan["fits"]}, mode {plan["mode"]}, GPUs {plan["required_gpus"]}')
    print(f'  remaining by requests: {plan["remaining_cpus"]} CPUs, {plan["remaining_mib"]} MiB '
          f'({plan["remaining_mib_after_telemetry_reserve"]} MiB after the whole telemetry reserve)')
    print(f'  limits: {plan["limits"]["cpu"]} CPUs, {plan["limits"]["memory_mib"]} MiB; past the node: '
          f'{plan["limits"]["over_node_cpus"]} CPUs, {plan["limits"]["over_node_mib"]} MiB')
    if pool:
        print(f'  sandbox pool: requests {pool["cpu"]} CPUs, {pool["memory_mib"]} MiB; limits {pool["limit_cpu"]} CPUs, {pool["limit_mib"]} MiB')
    else:
        print("REFUSED: the plan has no sandbox pool row; the render is not sandbox-enabled")
        return 1
    if "platform" not in rows:
        print("REFUSED: the plan did not count the node's other pods (--node-pods)")
        return 1
    return 0 if plan["fits"] else 1


def site_address(name):
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"STOP: set {name} (tools/README.md, \"Site values\")")
    return value


def probe(group, config_path, other=None):
    config = json.loads(Path(config_path).read_text())
    nas, lan = site_address("NAS_ADDRESS"), site_address("WORKSTATION_LAN_ADDRESS")
    ips = config["egress_service_ips"]
    api = config["api_server_cidr"].split("/")[0]
    own, cross = (ips["family"], ips["parents"]) if group == "family" else (ips["parents"], ips["family"])
    closed = {"node API": [api, config["api_server_port"]], "node kubelet": [api, 10250],
              "kubernetes service": ["10.43.0.1", 443], "DNS service": ["10.43.0.10", 53],
              "family resolver": [config["family_dns"][0], 53], "NAS": [nas, 2049],
              "LAN": [lan, 22], "Internet": ["1.1.1.1", 443],
              f'{"parents" if group == "family" else "family"} proxy': [cross, 4750]}
    if other:
        closed["other user's workspace"] = [other, 8000]
    allowed = ["pypi.org", "docs.python.org", "github.com"]
    # nip.io: a public name that resolves to the LAN address in it.
    denied = [f"{lan}.nip.io:22", f"{lan}:22", "10.43.0.1"]
    if group == "family":
        denied += ["nudity.testcategory.com", "1.1.1.1"]
    else:
        allowed += ["nudity.testcategory.com", "1.1.1.1"]
    print(json.dumps({"closed": closed, "open": {"own proxy": [own, 4750]}, "dns": "10.43.0.10",
                      "allowed": allowed, "denied": denied}))
    return 0


def webui(deployment_path, config_path):
    dep = json.loads(Path(deployment_path).read_text())
    config = json.loads(Path(config_path).read_text())
    env = {e["name"]: e.get("value") for e in dep["spec"]["template"]["spec"]["containers"][0].get("env", [])}
    connections = json.loads(env.get("TERMINAL_SERVER_CONNECTIONS") or "[]")
    want = {config["parents_group_id"]} | ({config["family_group_id"]} if config["family_enabled"] else set())
    ok = (len(connections) == 1 and connections[0]["id"] == "family-sandbox"
          and connections[0]["url"] == "http://assistant-terminals.assistant-sandbox.svc:3000"
          and {g["principal_id"] for g in connections[0]["config"]["access_grants"]} == want)
    print(f'  open-webui TERMINAL_SERVER_CONNECTIONS: {len(connections)} connection(s)'
          + (f', "{connections[0]["name"]}", {len(connections[0]["config"]["access_grants"])} group grant(s)' if connections else "")
          + ("" if ok else "   <- NOT the rendered sandbox connection"))
    return 0 if ok else 1


ACTIONS = ("kdiff", "allow", "bundle", "namespace", "pick", "drift", "capacity", "probe", "webui")


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help") or argv[1] not in ACTIONS:
        print(__doc__.strip())
        return 0 if len(argv) > 1 and argv[1] in ("-h", "--help") else 2
    action, args = argv[1], argv[2:]
    if action == "kdiff":
        return kdiff(*args)
    if action == "allow":
        return allow(args[0], args[1], args[2:])
    return {"bundle": bundle, "namespace": namespace, "pick": pick, "drift": drift,
            "capacity": capacity, "probe": probe, "webui": webui}[action](*args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
