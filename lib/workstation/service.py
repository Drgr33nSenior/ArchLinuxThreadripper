"""Offline service composition/admission and explicit operator key provisioning."""
import argparse
import ast
import copy
import datetime
import hashlib
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


def candidate_contract(pointer, expected_sha):
    """Read selected metadata only; never import or launch a candidate packet."""
    path = Path(pointer)
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 131072,
            "candidate pointer must be a bounded regular file")
    data = path.read_bytes()
    require(isinstance(expected_sha, str) and re.fullmatch(r"[a-f0-9]{64}", expected_sha) is not None
            and hashlib.sha256(data).hexdigest() == expected_sha, "candidate pointer digest differs")
    record = json.loads(data)
    require(isinstance(record, dict) and record.get("schema") == 1
            and record.get("candidate_selected") is True, "supply an explicitly selected candidate pointer")
    runtime = record.get("selected_runtime", {})
    require(isinstance(runtime, dict), "invalid selected runtime")
    cli = runtime.get("canonical_server_cli")
    require(isinstance(cli, list) and len(cli) <= 512
            and all(isinstance(arg, str) and len(arg) <= 4096 for arg in cli), "invalid candidate arguments")

    def option(name, optional=False):
        positions = [i for i, arg in enumerate(cli) if arg == name]
        if optional and not positions:
            return None
        require(len(positions) == 1 and positions[0] + 1 < len(cli), "missing or duplicate candidate option")
        value = cli[positions[0] + 1]
        require(not value.startswith("--"), "candidate option lacks a value")
        return value

    model = option("--served-model-name")
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:-]{0,199}", model) is not None and ".." not in model,
            "invalid served model identifier")
    revision = option("--revision")
    require(re.fullmatch(r"[a-f0-9]{40}", revision) is not None, "candidate revision must be pinned")
    resolved = runtime.get("resolved_contract", {})
    require(isinstance(resolved, dict), "invalid resolved contract")
    numbers = {}
    for flag, key, low, high in (("--context-length", "context_length", 2048, 262144),
            ("--max-total-tokens", "max_total_tokens", 2048, 1048576),
            ("--max-running-requests", "max_running_requests", 1, 8),
            ("--tp-size", "tp_size", 1, 2)):
        value = option(flag)
        require(re.fullmatch(r"[1-9][0-9]{0,6}", value) is not None, "invalid candidate token/resource bound")
        number = int(value)
        require(low <= number <= high and type(resolved.get(key)) is int and resolved[key] == number,
                "candidate CLI and resolved contract differ")
        numbers[key] = number
    require(numbers["max_total_tokens"] >= numbers["context_length"], "candidate pool is below one context")
    package = record.get("candidate_package_base", {})
    require(isinstance(package, dict) and isinstance(package.get("package_sha256"), str)
            and re.fullmatch(r"[a-f0-9]{64}", package["package_sha256"]) is not None,
            "candidate package digest is missing")
    arm = package.get("selected_arm")
    require(isinstance(arm, dict) and all(arm.get(key) == runtime.get(runtime_key)
            and arm.get(key) is not None for key, runtime_key in
            (("logical", "logical_arm"), ("physical", "physical_arm"), ("start", "start"))),
            "candidate arm selection differs")
    quantization, kv_dtype = option("--quantization"), option("--kv-cache-dtype")
    require(quantization == "quark" and kv_dtype == "bf16", "this plan requires the Quark/BF16 candidate")
    reasoning, tools = option("--reasoning-parser", True), option("--tool-call-parser", True)
    require(reasoning in {None, "qwen3"} and tools in {None, "qwen3_coder"}, "unreviewed candidate parser")
    # Only allowlisted metadata leaves the pointer. CLI keys, paths and values
    # for authentication, environment or arbitrary loader options are omitted.
    return {"pointer_sha256": expected_sha, "package_sha256": package["package_sha256"],
            "served_model": model, "model_revision": revision, **numbers,
            "quantization": quantization, "kv_cache_dtype": kv_dtype,
            "reasoning_parser": reasoning, "tool_call_parser": tools,
            "runtime_observed": False}


def client_profile(mode):
    require(mode in {"large", "split"}, "unknown client mode")
    return ({"AGENT_MODEL": "Qwen3.8-27B-FP8", "AGENT_CONTEXT_TOKENS": 32768, "AGENT_MAX_OUTPUT_TOKENS": 4096}
            if mode == "large" else
            {"AGENT_MODEL": "Qwen3.5-9B", "AGENT_CONTEXT_TOKENS": 4096, "AGENT_MAX_OUTPUT_TOKENS": 1024})


def candidate_plan(args):
    """Prepare an offline integration comparison and a frozen quality menu."""
    contract = candidate_contract(args.pointer, args.pointer_sha256)
    require(type(args.output_tokens) is int and 128 <= args.output_tokens <= contract["context_length"] // 2,
            "output allowance must be independent and at most half the context")
    objects = kustomize(ROOT / "apps/overlays/assistant")
    runtime = next(o for o in objects if o["kind"] == "ConfigMap"
                   and "boundary.py" in o.get("data", {}))
    # Inspect the rendered source without executing it or inspecting credentials.
    tree = ast.parse(runtime["data"]["boundary.py"])
    models = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id == "CHAT_MODELS" for target in node.targets))
    config = next(o for o in objects if o["kind"] == "ConfigMap" and "litellm.yaml" in o.get("data", {}))
    routes = yaml.safe_load(config["data"]["litellm.yaml"])["model_list"]
    ui = next(o for o in objects if o["kind"] == "Deployment" and o["metadata"]["name"] == "open-webui")
    env = {entry["name"]: entry.get("value") for entry in ui["spec"]["template"]["spec"]["containers"][0]["env"]}
    selected = env["DEFAULT_MODELS"]
    route = next(r for r in routes if r["model_name"] == selected)
    profile = next(o["data"] for o in objects if o["kind"] == "ConfigMap" and "MODEL_REVISION" in o.get("data", {}))
    client = client_profile("large")
    checks = [
        {"id": "upstream-model", "matches": route["litellm_params"]["model"] == "openai/" + contract["served_model"],
         "current": route["litellm_params"]["model"], "candidate": "openai/" + contract["served_model"]},
        {"id": "gateway-context", "matches": models.get(selected, (None,))[0] == contract["context_length"],
         "current": models.get(selected, (None,))[0], "candidate": contract["context_length"]},
        {"id": "router-context", "matches": route["model_info"]["max_tokens"] == contract["context_length"],
         "current": route["model_info"]["max_tokens"], "candidate": contract["context_length"]},
        {"id": "client-context", "matches": client["AGENT_CONTEXT_TOKENS"] == contract["context_length"],
         "current": client["AGENT_CONTEXT_TOKENS"], "candidate": contract["context_length"]},
        {"id": "container-context", "matches": int(profile["CONTEXT_LENGTH"]) == contract["context_length"],
         "current": int(profile["CONTEXT_LENGTH"]), "candidate": contract["context_length"]},
        {"id": "container-model-revision", "matches": profile["MODEL_REVISION"] == contract["model_revision"],
         "current": profile["MODEL_REVISION"], "candidate": contract["model_revision"]},
    ]
    menu_path = ROOT / "infrastructure/sglang/assistant-quality.json"
    menu = json.loads(menu_path.read_text())
    pending = ["Package the selected native runtime and weights for a persistent authenticated endpoint.",
               "Verify model identity and actual tokenizer accounting, including templates, tools and output reserve.",
               "Qualify native reasoning and tool-call parsers through the gateway and browser.",
               "Measure the requested output allowance and client/gateway deadlines on the candidate.",
               "Complete the quality menu and fresh host/GPU capacity admission before activation."]
    output = new_directory(args.output)
    (output / "candidate-contract.json").write_text(json.dumps(contract, indent=2) + "\n")
    (output / "client-values.json").write_text(json.dumps({"AGENT_MODEL": contract["served_model"],
        "AGENT_CONTEXT_TOKENS": contract["context_length"], "AGENT_MAX_OUTPUT_TOKENS": args.output_tokens,
        "status": "proposed-not-qualified", "endpoint_required": True}, indent=2) + "\n")
    (output / "quality-menu.json").write_text(json.dumps(menu, indent=2) + "\n")
    report = {"schema": 1, "status": "blocked-integration", "deployment_ready": False,
              "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "candidate_pointer_sha256": contract["pointer_sha256"], "checks": checks,
              "assistant_source_sha256": hashlib.sha256(yaml.safe_dump_all(objects, sort_keys=True).encode()).hexdigest(),
              "planner_sha256": sha(Path(__file__)),
              "current_public_model": selected, "current_output_cap": models[selected][1],
              "proposed_output_cap": args.output_tokens, "output_cap_qualified": False,
              "quality_menu_sha256": sha(menu_path), "pending": pending,
              "files": {name: sha(output / name) for name in
                        ("candidate-contract.json", "client-values.json", "quality-menu.json")}}
    (output / "integration-plan.json").write_text(json.dumps(report, indent=2) + "\n")
    print("Candidate integration prepared; blocked pending runtime and quality qualification:", output)


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
    experience = getattr(args, "experience", "service")
    require(experience in {"service", "assistant"}, "unknown service experience")
    overlay = experience if args.mode == "large" else experience + "-split"
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
    (output / "client.conf").write_text(f'AGENT_BASE_URL=https://{site["api_host"]}:32443/v1\n' +
        "".join(f"{key}={value}\n" for key, value in client_profile(args.mode).items()))
    if experience == "assistant":
        with (output / "client.conf").open("a") as client:
            client.write('AGENT_RETRIEVAL_ENABLED=true\nAGENT_WEB_SEARCH_ENABLED=true\n')
    (output / "evidence.json").write_text(json.dumps({"status": "source-rendered-not-deployed", "mode": args.mode,
        "experience": experience,
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
    p.add_argument("--experience", choices=["service", "assistant"], default="service")
    p.add_argument("--existing-claims", help="operator-collected PVC JSON; preserve immutable storage classes")
    p = commands.add_parser("candidate-plan", help="compare a pinned selected native candidate with assistant source; offline only")
    p.add_argument("pointer"); p.add_argument("output")
    p.add_argument("--pointer-sha256", required=True)
    p.add_argument("--output-tokens", type=int, default=4096,
                   help="proposed output allowance, separate from context; remains unqualified")
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
        elif args.action == "candidate-plan": candidate_plan(args)
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
