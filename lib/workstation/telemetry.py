"""Offline observability rendering, integrity checks and capacity planning."""

import argparse
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

import yaml


FILES = ("stack.yaml", "workloads.yaml", "evidence.json")
PROFILES = ("full", "metrics")
METRICS_ONLY_COMPONENTS = {"loki", "tempo"}
PRIVATE_NETS = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"
))


class InvalidTelemetry(ValueError):
    """Only fixed, nonsecret diagnostic messages may use this exception."""


def require(condition, message):
    if not condition:
        raise InvalidTelemetry(message)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def new_directory(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=False, exist_ok=False)
    return path


def private_address(value):
    address = ipaddress.ip_address(value)
    require(any(address in net for net in PRIVATE_NETS if net.version == address.version),
            "select actual private numeric API/node addresses, not public, loopback or example addresses")
    return f"{address}/{address.max_prefixlen}"


def load_objects(text):
    objects = list(yaml.safe_load_all(text))
    require(objects and all(isinstance(o, dict) and o.get("kind") for o in objects),
            "empty or malformed Kubernetes render")
    require(not any(o["kind"] == "Secret" for o in objects),
            "provision credentials separately; Secret resources cannot enter generated evidence")
    return objects


def kustomize(path):
    result = subprocess.run(["kubectl", "kustomize", str(path)], capture_output=True,
                            text=True, timeout=120, check=False)
    require(result.returncode == 0, "local kustomize failed; inspect the selected source separately")
    return load_objects(result.stdout)


def source_identity(root):
    """Hashes describe dirty/package content; a Git revision alone does not."""
    identity = {"git_revision": None, "build_identity_sha256": None,
                "installer_source_file_sha256": None}
    for name, key in (("BUILD-IDENTITY", "build_identity_sha256"),
                      ("INSTALLER-SOURCE.sha256", "installer_source_file_sha256")):
        path = root / name
        if path.is_file() and not path.is_symlink():
            identity[key] = sha(path)
    if (root / ".git").exists() and shutil.which("git"):
        result = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=root,
                                capture_output=True, text=True, timeout=10, check=False)
        if result.returncode == 0 and re.fullmatch(r"[a-f0-9]{40,64}\n?", result.stdout):
            identity["git_revision"] = result.stdout.strip()
    return identity


def pod_spec(obj):
    if obj["kind"] in {"Deployment", "StatefulSet", "DaemonSet"}:
        return obj["spec"]["template"]["spec"]
    return None


def images(objects):
    return sorted({c["image"] for obj in objects if pod_spec(obj)
                   for c in pod_spec(obj).get("containers", []) + pod_spec(obj).get("initContainers", [])})


def quantity(value, memory=False):
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(Ki|Mi|Gi|Ti|m)?", str(value))
    require(match is not None, "unsupported or missing resource quantity")
    number, unit = float(match[1]), match[2] or ""
    factors = {"": 1 / 1048576, "Ki": 1 / 1024, "Mi": 1, "Gi": 1024, "Ti": 1048576} if memory else {"": 1, "m": .001}
    require(unit in factors and number > 0, "invalid resource quantity")
    return number * factors[unit]


def pod_budget(obj):
    spec = pod_spec(obj)
    limits = [c["resources"]["limits"] for c in spec["containers"]]
    init = []
    for container in spec.get("initContainers", []):
        require(container.get("restartPolicy") != "Always", "native sidecar budgeting needs explicit planner support")
        init.append(container["resources"]["limits"])
    memory = max([sum(quantity(c["memory"], True) for c in limits)] + [quantity(c["memory"], True) for c in init])
    cpu = max([sum(quantity(c["cpu"]) for c in limits)] + [quantity(c["cpu"]) for c in init])
    shm = sum(quantity(v["emptyDir"]["sizeLimit"], True) for v in spec.get("volumes", [])
              if v.get("emptyDir", {}).get("medium") == "Memory")
    require(shm <= memory, "memory-backed volumes exceed pod limits")
    return {"memory_mib": math.ceil(memory), "cpu": cpu, "shared_memory_mib": math.ceil(shm)}


def telemetry_policy(name, direction, port):
    peer = {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "workstation-observability"}},
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": "alloy"}}}
    return {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
            "metadata": {"name": name, "namespace": "ai-home-lab"},
            "spec": {"podSelector": {"matchLabels": {"app.kubernetes.io/name": "sglang"}},
                     "policyTypes": [direction], direction.lower(): [{
                         "from" if direction == "Ingress" else "to": [peer],
                         "ports": [{"protocol": "TCP", "port": port}]}]}}


def configure_workloads(objects, trace, locked_image):
    matches = [o for o in objects if o["kind"] == "Deployment" and o["metadata"]["name"] == "sglang"]
    require(len(matches) == 1, "selected overlay must contain exactly one SGLang deployment")
    deployment = matches[0]
    container = next(c for c in pod_spec(deployment)["containers"] if c["name"] == "sglang")
    require(container["image"] == locked_image, "recheck the telemetry contract before changing the pinned SGLang image")
    args = container["args"]
    require(not any(arg in args for arg in ("--enable-metrics", "--enable-trace", "--log-requests")),
            "source already overrides telemetry/request logging; reconcile it explicitly")
    args.append("--enable-metrics")
    objects.append(telemetry_policy("sglang-metrics-from-alloy", "Ingress", 30000))
    if trace:
        args.extend(["--enable-trace", "--trace-modules", "request", "--otlp-traces-endpoint",
                     "alloy.workstation-observability.svc.cluster.local:4317"])
        container.setdefault("env", []).append({"name": "SGLANG_TRACE_LEVEL", "value": "1"})
        objects.append(telemetry_policy("sglang-traces-to-alloy", "Egress", 4317))
        deployment["metadata"].setdefault("annotations", {})["workstation.local/trace-qualification"] = "pending-exact-image-import-and-load-test"
    return objects


def metrics_stack(objects):
    """Remove the log/trace-only objects from the reviewed full composition."""
    selected = []
    for obj in objects:
        name = obj.get("metadata", {}).get("name", "")
        # Kustomize hashes generated ConfigMaps. Prefix matching is limited to
        # the two config maps that do not exist in the metrics-only profile.
        if name.startswith(("loki-", "tempo-")) or name in METRICS_ONLY_COMPONENTS:
            continue
        if obj.get("kind") == "NetworkPolicy" and any(part in name for part in METRICS_ONLY_COMPONENTS):
            continue
        selected.append(obj)
    return selected


def configure_profile(stage, profile):
    """Apply a fixed source composition before Kustomize hashes ConfigMaps."""
    require(profile in PROFILES, "unsupported telemetry profile")
    if profile != "metrics":
        return
    config = stage / "config" / "config.alloy"
    config.write_text((stage / "config" / "config.metrics.alloy").read_text())
    kustomization = stage / "kustomization.yaml"
    data = yaml.safe_load(kustomization.read_text())
    for generator in data.get("configMapGenerator", []):
        if generator.get("name") == "grafana-datasources":
            generator["files"] = ["datasources.yaml=config/datasources.metrics.yaml"]
        if generator.get("name") == "prometheus-config":
            generator["files"] = ["prometheus.yml=config/prometheus.yaml", "alerts.yml=config/alerts.metrics.yaml"]
    alerts = yaml.safe_load((stage / "config" / "alerts.yaml").read_text())
    for group in alerts.get("groups", []):
        for rule in group.get("rules", []):
            if rule.get("alert") == "WorkstationExporterDown":
                rule["expr"] = 'up{job=~"node|kube-state-metrics|alloy|workstation-host-alloy|prometheus|grafana"} == 0'
            elif rule.get("alert") == "WorkstationTelemetryExportFailed":
                rule["expr"] = 'sum(increase(otelcol_exporter_send_failed_metric_points_total[5m])) > 0 or sum(increase(prometheus_remote_storage_samples_failed_total[5m])) > 0'
            elif rule.get("alert") == "WorkstationTelemetryRefused":
                rule["expr"] = 'sum(increase({__name__=~"otelcol_processor(_memory_limiter)?_refused_metric_points_total"}[5m])) > 0'
    (stage / "config" / "alerts.metrics.yaml").write_text(yaml.safe_dump(alerts, sort_keys=False))
    kustomization.write_text(yaml.safe_dump(data, sort_keys=False))


def render(args):
    root = Path(args.root).resolve()
    enabled = args.enabled == "true"
    require(args.profile in PROFILES, "unsupported telemetry profile")
    require(args.reserve_mib > 0, "reserve must be positive")
    require(args.margin_mib > 0, "telemetry margin must be positive")
    if enabled:
        api, host = private_address(args.api_address), private_address(args.host_address)
        require(args.gpu_exporter == "false", "no verified coherent ROCm10 exporter artifact; use the packaged read-only sysfs sampler")
        replacements = {"192.0.2.1/32": api, "192.0.2.2/32": host,
                        "192.0.2.2:10250": (f"[{args.host_address}]:10250" if ":" in args.host_address else f"{args.host_address}:10250"),
                        "review-required-node": args.node_name}
        with tempfile.TemporaryDirectory(prefix="workstation-telemetry-") as temp:
            stage = Path(temp) / "source"
            shutil.copytree(root / "infrastructure/observability", stage)
            configure_profile(stage, args.profile)
            if args.kubelet == "true":
                require(re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", args.node_name), "set the actual Kubernetes node name")
                path = stage / "kustomization.yaml"
                config = yaml.safe_load(path.read_text())
                config.setdefault("components", []).append("kubelet")
                path.write_text(yaml.safe_dump(config, sort_keys=False))
            # Resolve before hashing generated ConfigMaps so changed target
            # addresses also change pod references and trigger a rollout.
            for path in stage.rglob("*"):
                if path.is_file() and path.suffix in {".yaml", ".alloy"}:
                    value = path.read_text()
                    for old, new in replacements.items():
                        value = value.replace(old, new)
                    path.write_text(value)
            stack = kustomize(stage)
            if args.profile == "metrics":
                stack = metrics_stack(stack)
        require(all(re.search(r"@sha256:[a-f0-9]{64}$", image) for image in images(stack)),
                "every telemetry image needs an immutable digest")
        overlay = Path(args.overlay)
        require(not overlay.is_absolute() and ".." not in overlay.parts and overlay.parts[:2] == ("apps", "overlays"),
                "select a relative apps/overlays path")
        selected = (root / overlay).resolve()
        require(root in selected.parents, "workload overlay escapes source root")
        lock = dict(line.split("=", 1) for line in (root / "versions.lock").read_text().splitlines()
                    if line and not line.startswith("#"))
        require(args.profile == "full" or args.trace == "false",
                "metrics telemetry profile does not collect traces")
        workloads = configure_workloads(kustomize(selected), args.trace == "true", lock["SGLANG_ROCM_IMAGE"])
    else:
        stack, workloads = [], []
    total = sum(pod_budget(o)["memory_mib"] for o in stack if pod_spec(o))
    component_limits = {"cluster_stack_mib": total, "host_alloy_mib": 512 if enabled else 0,
                        "hardware_sampler_mib": 128 if enabled else 0}
    calculated_allowance = sum(component_limits.values()) + args.margin_mib
    require(calculated_allowance <= args.reserve_mib,
            "telemetry component limits plus margin exceed TELEMETRY_RESERVE_MIB")
    selected_names = args.workloads.split(",")
    require(len(set(selected_names)) == len(selected_names) and all(re.fullmatch(r"[a-z0-9-]+", n) for n in selected_names),
            "invalid or duplicate planned workload")
    source_files = sorted({p for folder in ("infrastructure/observability", "apps")
                           for p in (root / folder).rglob("*") if p.is_file() and not p.is_symlink()})
    source_files += [root / "versions.lock", root / "lib/workstation/telemetry.py", root / "lib/workstation/telemetry.sh"]
    evidence = {"schema": 2, "status": "generated-not-deployed" if enabled else "disabled-not-deployed",
                "hardware_qualification": "NOT RUN", "enabled": enabled,
                "profile": args.profile,
                "gpu_exporter": args.gpu_exporter == "true", "sglang_trace": args.trace == "true",
                "kubelet": args.kubelet == "true", "node_name": args.node_name,
                "api_address": args.api_address, "workstation_address": args.host_address,
                "reserve_mib": args.reserve_mib, "stack_limit_mib": total,
                "component_limits_mib": component_limits, "margin_mib": args.margin_mib,
                "calculated_allowance_mib": calculated_allowance,
                "planned_workloads": selected_names, "workload_overlay": args.overlay,
                "stack_images": images(stack), "workload_images": images(workloads),
                "source_identity": source_identity(root),
                "source_sha256": {str(p.relative_to(root)): sha(p) for p in source_files}}
    output = new_directory(args.output)
    for name, objects in (("stack.yaml", stack), ("workloads.yaml", workloads)):
        (output / name).write_text(yaml.safe_dump_all(objects, sort_keys=False))
    (output / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    (output / "SHA256SUMS").write_text("".join(f"{sha(output / name)}  {name}\n" for name in FILES))
    print(f"{evidence['status']}: {output}; no credentials, cluster writes or hardware qualification")


def validate_evidence(evidence):
    """Validate the nonsecret capacity fields shared with offline planners.

    Schema 1 predates selectable profiles, so it is deliberately full-only.
    Schema 2 records all component limits and an explicit safety margin.  This
    function does not make a render deployable or qualified.
    """
    require(isinstance(evidence, dict) and evidence.get("schema") in {1, 2}
            and evidence.get("status") in {"generated-not-deployed", "disabled-not-deployed"}
            and type(evidence.get("enabled")) is bool
            and type(evidence.get("reserve_mib")) is int and evidence["reserve_mib"] > 0
            and type(evidence.get("stack_limit_mib")) is int and evidence["stack_limit_mib"] >= 0,
            "unsupported evidence status/schema")
    if evidence["schema"] == 1:
        require(evidence.get("profile", "full") == "full", "legacy telemetry evidence supports full profile only")
    else:
        components = evidence.get("component_limits_mib")
        require(evidence.get("profile") in PROFILES and type(evidence.get("margin_mib")) is int
                and evidence["margin_mib"] > 0 and isinstance(components, dict)
                and set(components) == {"cluster_stack_mib", "host_alloy_mib", "hardware_sampler_mib"}
                and all(type(value) is int and value >= 0 for value in components.values())
                and evidence["stack_limit_mib"] == components["cluster_stack_mib"]
                and evidence.get("calculated_allowance_mib") == sum(components.values()) + evidence["margin_mib"]
                and evidence["reserve_mib"] >= evidence["calculated_allowance_mib"],
                "invalid telemetry profile allowance")
    return evidence


def verify(directory):
    directory = Path(directory)
    require(not directory.is_symlink() and not any((directory / name).is_symlink() for name in (*FILES, "SHA256SUMS")),
            "evidence files must not be symlinks")
    expected = {}
    for line in (directory / "SHA256SUMS").read_text().splitlines():
        match = re.fullmatch(r"([a-f0-9]{64})  ([a-zA-Z0-9.-]+)", line)
        require(match is not None and match[2] in FILES and match[2] not in expected, "invalid evidence manifest")
        expected[match[2]] = match[1]
    require(set(expected) == set(FILES) and all(sha(directory / n) == h for n, h in expected.items()),
            "generated evidence changed or is incomplete; regenerate and review")
    return validate_evidence(json.loads((directory / "evidence.json").read_text()))


def plan(args):
    evidence = verify(args.rendered)
    resources = json.loads(Path(args.resources).read_text())
    require(resources.get("schema_version") == 1 and resources.get("status") == "offline-plan-not-applied",
            "use an existing workstationctl resources plan result")
    require(evidence["enabled"], "telemetry is disabled; no enabled-stack capacity plan")
    stack = load_objects((Path(args.rendered) / "stack.yaml").read_text())
    workloads = load_objects((Path(args.rendered) / "workloads.yaml").read_text())
    selected = {o["metadata"]["name"]: o for o in workloads if o["kind"] == "Deployment"}
    rows = []
    for name in evidence["planned_workloads"]:
        require(name in selected, "planned workload is absent from selected overlay")
        obj = selected[name]
        require(obj["spec"].get("replicas", 1) <= 1, "planner supports one intended replica per workload")
        rows.append({"name": name, **pod_budget(obj), "planned_replicas": 1})
    stack_cpu = sum(pod_budget(o)["cpu"] for o in stack if pod_spec(o))
    memory = resources["memory"]["allocatable_mib"] - evidence["reserve_mib"] - sum(r["memory_mib"] for r in rows)
    cpu = resources["allocatable_logical_cpus"] - stack_cpu - sum(r["cpu"] for r in rows)
    gpu = sum(int(c["resources"]["limits"].get("amd.com/gpu", 0))
              for name in evidence["planned_workloads"] for c in pod_spec(selected[name])["containers"])
    fits = memory >= 0 and cpu >= 0 and gpu <= resources["gpu_count"]
    result = {"schema": 1, "status": "planned-not-qualified" if fits else "blocked-capacity", "fits": fits,
              "resource_plan_sha256": sha(Path(args.resources)), "render_sha256": sha(Path(args.rendered) / "SHA256SUMS"),
              "workloads": rows, "telemetry_reserve_mib": evidence["reserve_mib"],
              "telemetry_profile": evidence.get("profile", "full"),
              "telemetry_component_limits_mib": evidence.get("component_limits_mib", {"cluster_stack_mib": evidence["stack_limit_mib"]}),
              "telemetry_margin_mib": evidence.get("margin_mib", None),
              "telemetry_calculated_allowance_mib": evidence.get("calculated_allowance_mib", evidence["stack_limit_mib"]),
              "telemetry_cpu_limit": stack_cpu, "remaining_memory_mib": memory, "remaining_cpu": cpu,
              "required_gpu_count": gpu, "observed_gpu_count": resources["gpu_count"],
              "limitations": ["Capacity plan, not current free memory or measured performance.",
                              "Selected workloads counted at one intended replica even when gated at zero.",
                              "Unselected VMs, builds, games and pods are additional demand.",
                              "Shared memory is inside pod limits, not extra RAM.",
                              "Journal and physical boot/GPU qualification remain separate."]}
    output = new_directory(args.output)
    (output / "capacity.json").write_text(json.dumps(result, indent=2) + "\n")
    print(f"{result['status']}: {output / 'capacity.json'}")
    return 0 if fits else 1


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    render_parser = actions.add_parser("render")
    render_parser.add_argument("output")
    render_parser.add_argument("overlay", nargs="?", default="apps/overlays/dual-gpu")
    render_parser.add_argument("--root", required=True)
    for name in ("enabled", "gpu-exporter", "trace", "kubelet"):
        render_parser.add_argument("--" + name, choices=("true", "false"), required=True)
    for name in ("api-address", "host-address", "workloads", "node-name"):
        render_parser.add_argument("--" + name, required=True)
    render_parser.add_argument("--profile", choices=PROFILES, default="full")
    render_parser.add_argument("--reserve-mib", type=int, required=True)
    render_parser.add_argument("--margin-mib", type=int, required=True)
    verify_parser = actions.add_parser("verify")
    verify_parser.add_argument("directory")
    plan_parser = actions.add_parser("plan")
    for name in ("resources", "rendered", "output"):
        plan_parser.add_argument(name)
    args = parser.parse_args()
    try:
        if args.action == "render":
            render(args)
        elif args.action == "verify":
            result = verify(args.directory)
            print(f"integrity verified; {result['status']}; hardware NOT RUN")
        else:
            return plan(args)
    except InvalidTelemetry as error:
        print(f"FAILED: {error}; no deployment performed", file=sys.stderr)
        return 1
    except (ValueError, KeyError, TypeError, OSError, StopIteration, subprocess.TimeoutExpired, yaml.YAMLError):
        print("FAILED: telemetry inputs or tools unavailable/malformed; no deployment performed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
