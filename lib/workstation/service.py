"""Offline service composition/admission and explicit operator key provisioning."""
import argparse
import copy
import datetime
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request

import yaml

from telemetry import kustomize, load_objects, new_directory, pod_budget, pod_spec, sha, verify


ROOT = Path(__file__).resolve().parents[2]
MODELS = {"Qwen3.8-27B-FP8", "Qwen3.5-9B"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def protected(path):
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), "credential must be a regular file")
    meta = path.stat()
    require(meta.st_uid == os.geteuid() and stat.S_IMODE(meta.st_mode) == 0o600,
            "credential must be caller-owned mode 0600")
    return path.read_text().strip()


def site_values(path):
    site = json.loads(Path(path).read_text())
    require(set(site) == {"environment", "context", "node", "node_address", "management_cidr",
                          "client_cidrs", "webui_host", "api_host", "tls_secret"}, "unexpected/missing site keys")
    require(site["environment"] in {"dev", "tst", "int"}, "explicit non-production classification required")
    address = ipaddress.ip_address(site["node_address"])
    management = ipaddress.ip_network(site["management_cidr"])
    private = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
    require(address.version == 4 and address in management and any(management.subnet_of(n) for n in private),
            "use actual private IPv4 node/management network")
    require(isinstance(site["client_cidrs"], list) and 1 <= len(site["client_cidrs"]) <= 8,
            "supply approved client CIDRs")
    for cidr in site["client_cidrs"]:
        network = ipaddress.ip_network(cidr)
        require(network.version == 4 and any(network.subnet_of(n) for n in private), "client CIDR must be private")
    for key in ("webui_host", "api_host"):
        require(isinstance(site[key], str) and re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", site[key])
                and "." in site[key] and not site[key].endswith(".invalid"), "supply actual private DNS names")
    require(site["webui_host"] != site["api_host"], "browser/API names must differ")
    for key in ("node", "context", "tls_secret"):
        require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,200}", site[key]) is not None, "invalid target identifier")
    return site


def render(args):
    site = site_values(args.site)
    overlay = "service" if args.mode == "large" else "service-split"
    objects = kustomize(ROOT / "apps/overlays" / overlay)
    existing = {}
    if getattr(args, "existing_claims", None):
        claims = json.loads(Path(args.existing_claims).read_text())
        require(claims.get("kind") in {"PersistentVolumeClaimList", "List"},
                "supply kubectl get pvc -o json metadata")
        for claim in claims["items"]:
            require(claim["kind"] == "PersistentVolumeClaim" and claim["metadata"]["namespace"] == "ai-home-lab",
                    "claim metadata has wrong kind or namespace")
            existing[claim["metadata"]["name"]] = {"uid": claim["metadata"]["uid"],
                "storage_class": claim["spec"].get("storageClassName", "")}
    for obj in objects:
        name, kind = obj["metadata"]["name"], obj["kind"]
        if kind == "Deployment" and name == "service-tools":
            obj["spec"]["template"]["spec"]["containers"][0].setdefault("env", []).append(
                {"name": "PUBLIC_WEBUI_BASE_URL", "value": f'https://{site["webui_host"]}:32443'})
        if kind == "Ingress":
            host = site["webui_host"] if name == "open-webui" else site["api_host"]
            obj["spec"]["rules"][0]["host"] = host
            obj["spec"]["tls"] = [{"hosts": [host], "secretName": site["tls_secret"]}]
            annotations = obj["metadata"].setdefault("annotations", {})
            current = annotations.get("traefik.ingress.kubernetes.io/router.middlewares", "")
            annotations["traefik.ingress.kubernetes.io/router.middlewares"] = ",".join(filter(None, [current, "ai-home-lab-private-clients@kubernetescrd"]))
        if kind == "PersistentVolumeClaim":
            obj["spec"]["storageClassName"] = existing.get(name, {}).get("storage_class", "workstation-retain")
            if name in {"webui-rag-qwen3-data", "webui-data", "creative-models", "gateway-postgres", "service-tools-state"}:
                obj["metadata"].setdefault("labels", {})["workstation.ai/backup"] = "irreplaceable"
    objects.append({"apiVersion": "traefik.io/v1alpha1", "kind": "Middleware",
                    "metadata": {"name": "private-clients", "namespace": "ai-home-lab"},
                    "spec": {"ipAllowList": {"sourceRange": site["client_cidrs"]}}})
    output = new_directory(args.output)
    (output / "workloads.yaml").write_text(yaml.safe_dump_all(objects, sort_keys=False))
    (output / "traefik.yaml").write_text((ROOT / "infrastructure/service/traefik.yaml").read_text())
    gateway = next(o for o in objects if o["kind"] == "Deployment" and o["metadata"]["name"] == "litellm")
    template = copy.deepcopy(gateway["spec"]["template"])
    template["metadata"]["labels"]["app.kubernetes.io/name"] = "litellm"
    container = template["spec"]["containers"][0]
    container["command"] = ["python", "/runtime/migrate.py"]
    for name in ("args", "ports", "readinessProbe", "livenessProbe", "startupProbe"):
        container.pop(name, None)
    template["spec"]["restartPolicy"] = "Never"
    job = {"apiVersion": "batch/v1", "kind": "Job",
           "metadata": {"generateName": "litellm-migrate-1-106-2-", "namespace": "ai-home-lab"},
           "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 660, "template": template}}
    (output / "migration.yaml").write_text(yaml.safe_dump(job, sort_keys=False))
    (output / "site.json").write_text(json.dumps(site, indent=2) + "\n")
    (output / "client.conf").write_text(
        f'AGENT_BASE_URL=https://{site["api_host"]}:32443/v1\n'
        f'AGENT_MODEL={"Qwen3.8-27B-FP8" if args.mode == "large" else "Qwen3.5-9B"}\n'
        f'AGENT_CONTEXT_TOKENS={32768 if args.mode == "large" else 4096}\n'
        f'AGENT_MAX_OUTPUT_TOKENS={4096 if args.mode == "large" else 1024}\n')
    (output / "evidence.json").write_text(json.dumps({"status": "source-rendered-not-deployed", "mode": args.mode,
        "existing_claims": existing,
        "files": {n: sha(output / n) for n in ("workloads.yaml", "traefik.yaml", "migration.yaml", "site.json", "client.conf")},
        "required": ["TLS Secret", "private DNS", "NodePort firewall review", "retained-PV migration",
                     "backup restore acceptance", "database migration", "workload qualification"]}, indent=2) + "\n")
    print("Rendered service source; replicas remain zero:", output)


def capacity(resources, workloads, telemetry, telemetry_stack, mode, build_mib=0, build_cpus=0, vm_mib=0, vm_cpus=0):
    require(resources.get("schema_version") == 1 and resources.get("status") == "offline-plan-not-applied", "use resources plan output")
    require(all(type(n) is int and n >= 0 for n in (build_mib, build_cpus, vm_mib, vm_cpus)), "budgets must be nonnegative integers")
    names = {"sglang", "open-webui", "litellm", "gateway-postgres", "service-tools"}
    if mode == "split":
        names.add("swarmui")
    selected = {o["metadata"]["name"]: o for o in workloads if o["kind"] == "Deployment"}
    require(names <= selected.keys(), "service composition incomplete")
    rows = [{"name": name, **pod_budget(selected[name])} for name in sorted(names)]
    reserve = telemetry["reserve_mib"]
    telemetry_cpu = sum(pod_budget(o)["cpu"] for o in telemetry_stack if pod_spec(o))
    # Alertmanager is an optional extension to the existing telemetry composition.
    # Its current declared limits are counted from its canonical manifest.
    alerts = load_objects((ROOT / "infrastructure/service/alertmanager.yaml").read_text())
    alert_budget = next(pod_budget(o) for o in alerts if o["kind"] == "Deployment")
    rows += [{"name": "alertmanager", **alert_budget},
             {"name": "telemetry", "memory_mib": reserve, "cpu": telemetry_cpu},
             {"name": "builds", "memory_mib": build_mib, "cpu": build_cpus},
             {"name": "optional-vms", "memory_mib": vm_mib, "cpu": vm_cpus}]
    gpu = sum(int(c["resources"]["limits"].get("amd.com/gpu", 0))
              for name in names for c in selected[name]["spec"]["template"]["spec"]["containers"])
    memory = resources["memory"]["allocatable_mib"] - sum(r["memory_mib"] for r in rows)
    cpu = resources["allocatable_logical_cpus"] - sum(r["cpu"] for r in rows)
    fits = memory >= 0 and cpu >= 0 and gpu <= resources["gpu_count"]
    return {"status": "planned-not-qualified" if fits else "blocked-capacity", "fits": fits,
            "mode": mode, "rows": rows, "remaining_mib": memory, "remaining_cpus": round(cpu, 3),
            "required_gpus": gpu, "observed_total_mib": resources["memory"]["total_mib"],
            "observed_dimms": resources["memory"].get("dimms"),
            "limitations": ["Capacity is not current available RAM or qualification.",
                            "CPU limits are conservatively summed; no silent overcommit.",
                            "Builds/VMs need explicit budgets; running unrelated work remains additional demand.",
                            "Image mode never changes the requested model identity."]}


def plan(args):
    evidence = json.loads((Path(args.rendered) / "evidence.json").read_text())
    require(all(sha(Path(args.rendered) / n) == h for n, h in evidence["files"].items()), "render changed; regenerate")
    telemetry = verify(args.telemetry)
    require(telemetry["enabled"], "supply enabled telemetry evidence")
    result = capacity(json.loads(Path(args.resources).read_text()),
        load_objects((Path(args.rendered) / "workloads.yaml").read_text()), telemetry,
        load_objects((Path(args.telemetry) / "stack.yaml").read_text()), evidence["mode"],
        args.build_mib, args.build_cpus, args.vm_mib, args.vm_cpus)
    output = new_directory(args.output)
    (output / "capacity.json").write_text(json.dumps(result, indent=2) + "\n")
    print(result["status"], output / "capacity.json")
    return 0 if result["fits"] else 1


def admin_request(base, key, path, payload=None):
    parsed = urllib.parse.urlsplit(base)
    require(parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1"} and parsed.port
            and not parsed.username and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment,
            "admin operations require an explicit operator loopback port-forward")
    request = urllib.request.Request(base.rstrip("/") + path, data=json.dumps(payload).encode() if payload is not None else None,
                                     headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    # No redirects: an admin credential must never follow an upstream redirect.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(request, timeout=20) as response:
        data = response.read(262145)
        require(len(data) <= 262144, "admin response exceeded bound")
        return json.loads(data)


def provision(args):
    key = protected(args.master_file)
    request = json.loads(Path(args.request).read_text())
    require(set(request) == {"user_id", "client", "models", "rpm", "tpm", "concurrency", "days"}, "invalid provisioning request")
    require(re.fullmatch(r"[A-Za-z0-9_-]{1,100}", request["user_id"]), "invalid user id")
    require(re.fullmatch(r"[A-Za-z0-9_-]{1,60}", request["client"]), "invalid client name")
    require(isinstance(request["models"], list) and request["models"] and set(request["models"]) <= MODELS,
            "explicit supported model allowlist required")
    for field, maximum in (("rpm", 60), ("tpm", 131072), ("concurrency", 2), ("days", 90)):
        require(type(request[field]) is int and 1 <= request[field] <= maximum, "invalid bounded key policy")
    output = Path(args.output).resolve()
    require(not output.is_relative_to(ROOT), "credential output must be outside the checkout")
    output = new_directory(output)
    if args.existing_user:
        user = admin_request(args.base_url, key, "/v2/user/info?user_id=" + urllib.parse.quote(request["user_id"], safe=""))
    else:
        user = admin_request(args.base_url, key, "/user/new", {"user_id": request["user_id"],
                             "user_role": "internal_user", "auto_create_key": False})
    require(user.get("user_id") == request["user_id"], "gateway user creation did not match")
    result = admin_request(args.base_url, key, "/key/generate", {
        "user_id": request["user_id"], "key_alias": request["client"], "models": request["models"],
        "duration": str(request["days"]) + "d", "rpm_limit": request["rpm"], "tpm_limit": request["tpm"],
        "max_parallel_requests": request["concurrency"]})
    require(isinstance(result.get("key"), str) and result["key"].startswith("sk-"), "key provisioning failed")
    credential = output / "virtual-key.json"
    credential.write_text(json.dumps(result) + "\n")
    credential.chmod(0o600)
    (output / "policy.json").write_text(json.dumps(request, indent=2) + "\n")
    print("Credential saved in protected output; no credential values printed.")


def credentials(args):
    output = Path(args.output).resolve()
    require(not output.is_relative_to(ROOT), "credentials must be outside the checkout")
    output = new_directory(output)
    app_password = secrets.token_urlsafe(48)
    service_key = secrets.token_urlsafe(48)
    values = {"admin-password": secrets.token_urlsafe(48), "litellm-password": app_password,
              "database-url": f"postgresql://litellm:{app_password}@gateway-postgres:5432/litellm",
              "master-key": "sk-" + secrets.token_urlsafe(48), "salt-key": secrets.token_urlsafe(48),
              "control-key": secrets.token_urlsafe(48), "service-key": service_key,
              "mapping.json": json.dumps({"service_key": service_key, "users": {}}),
              "image-users.json": json.dumps({"service_key": service_key, "users": {}})}
    for name, value in values.items():
        path = output / name
        path.write_text(value + "\n")
        path.chmod(0o600)
    print("Protected credential files prepared; provision Secrets through the owner workflow.")


def save_browser_mapping(path, mapping):
    path = Path(path)
    images = {"service_key": mapping["service_key"], "users": {uid: {
        "expires_at": entry["expires_at"], "images": entry.get("images", False)}
        for uid, entry in mapping["users"].items()}}
    # The image process does not receive any user's inference key.
    for target, value in ((path, mapping), (path.with_name("image-users.json"), images)):
        temporary = target.with_name(target.name + ".new")
        with temporary.open("x") as output:
            json.dump(value, output)
        temporary.chmod(0o600)
        temporary.replace(target)


def notifications(args):
    verify(args.telemetry)
    network = ipaddress.ip_network(args.receiver_cidr)
    require(network.prefixlen == network.max_prefixlen and not network.network_address.is_unspecified,
            "use the selected receiver's exact IP CIDR")
    require(1 <= args.receiver_port <= 65535, "invalid receiver port")
    objects = load_objects((Path(args.telemetry) / "stack.yaml").read_text())
    config = next(o for o in objects if o["kind"] == "ConfigMap" and "prometheus.yml" in o.get("data", {}))
    prometheus = yaml.safe_load(config["data"]["prometheus.yml"])
    prometheus["alerting"] = {"alertmanagers": [{"static_configs": [{"targets": ["alertmanager:9093"]}], "timeout": "10s"}]}
    prometheus["scrape_configs"] = [{"job_name": "workstation-service", "metrics_path": "/_service/metrics",
        "scrape_timeout": "5s", "static_configs": [{"targets": ["litellm.ai-home-lab.svc:4000"]}]}]
    config["data"]["prometheus.yml"] = yaml.safe_dump(prometheus, sort_keys=False)
    rules = yaml.safe_load(config["data"]["alerts.yml"])
    rules["groups"] += yaml.safe_load((ROOT / "infrastructure/service/alerts.yaml").read_text())["groups"]
    config["data"]["alerts.yml"] = yaml.safe_dump(rules, sort_keys=False)
    objects += load_objects((ROOT / "infrastructure/service/alertmanager.yaml").read_text())
    for obj in objects:
        if obj["kind"] == "NetworkPolicy" and obj["metadata"]["name"] == "service-alertmanager":
            obj["spec"]["egress"] = [{"to": [{"ipBlock": {"cidr": str(network)}}],
                                      "ports": [{"protocol": "TCP", "port": args.receiver_port}]}]
    output = new_directory(args.output)
    (output / "stack.yaml").write_text(yaml.safe_dump_all(objects, sort_keys=False))
    (output / "evidence.json").write_text(json.dumps({"status": "source-rendered-not-deployed",
        "base_evidence_sha256": sha(Path(args.telemetry) / "evidence.json"), "stack_sha256": sha(output / "stack.yaml")}, indent=2))
    print("Notification composition prepared; receiver Secret and delivery acceptance still required.")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    p = commands.add_parser("render")
    p.add_argument("site"); p.add_argument("output"); p.add_argument("--mode", choices=["large", "split"], default="large")
    p.add_argument("--existing-claims", help="operator-collected PVC JSON; preserve immutable storage classes")
    p = commands.add_parser("plan")
    for name in ("resources", "rendered", "telemetry", "output"):
        p.add_argument(name)
    for name in ("build-mib", "build-cpus", "vm-mib", "vm-cpus"):
        p.add_argument("--" + name, type=int, default=0)
    p = commands.add_parser("credentials"); p.add_argument("output")
    p = commands.add_parser("provision")
    p.add_argument("--existing-user", action="store_true")
    for name in ("base-url", "master-file", "request", "output"):
        p.add_argument("--" + name, required=True)
    p = commands.add_parser("revoke")
    p.add_argument("--mapping")
    for name in ("base-url", "master-file", "key-file"):
        p.add_argument("--" + name, required=True)
    p = commands.add_parser("browser-bind")
    for name in ("mapping", "key-file", "user-id"):
        p.add_argument("--" + name, required=True)
    p.add_argument("--images", action="store_true")
    p = commands.add_parser("notifications")
    p.add_argument("telemetry"); p.add_argument("output")
    p.add_argument("--receiver-cidr", required=True)
    p.add_argument("--receiver-port", type=int, default=443)
    args = parser.parse_args()
    try:
        if args.action == "render": render(args)
        elif args.action == "plan": return plan(args)
        elif args.action == "credentials": credentials(args)
        elif args.action == "notifications": notifications(args)
        elif args.action == "provision": provision(args)
        elif args.action == "revoke":
            credential = json.loads(protected(args.key_file))
            admin_request(args.base_url, protected(args.master_file), "/key/delete", {"keys": [credential["key"]]})
            if args.mapping:
                mapping = json.loads(protected(args.mapping))
                mapping["users"] = {uid: entry for uid, entry in mapping["users"].items() if entry.get("key") != credential["key"]}
                save_browser_mapping(args.mapping, mapping)
            print("Gateway accepted key revocation; verify denial before retiring the credential record.")
        elif args.action == "browser-bind":
            mapping = json.loads(protected(args.mapping))
            credential = json.loads(protected(args.key_file))
            require(credential.get("user_id") == args.user_id, "key owner must match exact WebUI user id")
            expiry = datetime.datetime.fromisoformat(credential["expires"].replace("Z", "+00:00"))
            require(expiry.tzinfo is not None, "key expiry must include timezone")
            mapping["users"][args.user_id] = {"key": credential["key"], "expires_at": expiry.timestamp(), "images": args.images}
            save_browser_mapping(args.mapping, mapping)
            print("Protected browser mapping updated; publish the Secret through the owner workflow.")
        return 0
    except (ValueError, KeyError, OSError, urllib.error.URLError):
        # urllib/prisma errors can contain credential-bearing request data.
        print("Service operation failed; verify nonsecret inputs and protected operator evidence.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
