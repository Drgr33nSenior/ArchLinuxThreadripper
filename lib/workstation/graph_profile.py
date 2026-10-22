"""Offline, source-bound BF16 full-b4 selection. Never contact or restart a Pod."""

import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat


ROOT = Path(__file__).resolve().parents[2]
PROFILE = ROOT / "infrastructure/sglang/bf16-full-b4/profile.json"
PROFILE_SHA256 = "0e5744ea5e4eb3775e485b23086021d78b23b4751476120a0380d503fdf95745"
CACHE_NAMES = {"TRITON_CACHE_DIR": "triton", "TORCHINDUCTOR_CACHE_DIR": "torchinductor",
               "SGLANG_JIT_CACHE_DIR": "sglang-jit", "XDG_CACHE_HOME": "xdg"}
GRAPH_FLAGS = {"--cuda-graph-config", "--disable-cuda-graph-padding"}
FORBIDDEN_ENV = {"HIP_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES", "CUDA_VISIBLE_DEVICES",
                 "GPU_DEVICE_ORDINAL", "HSA_OVERRIDE_GFX_VERSION", "PYTHONPATH", "LD_PRELOAD",
                 "LD_LIBRARY_PATH", "HIPBLASLT_TUNING_OVERRIDE_FILE", "SGLANG_MOE_CONFIG_DIR",
                 "SGLANG_DP_RANK", "GPU_MAX_HW_QUEUES"}
ENVIRONMENT = {"HF_HUB_OFFLINE": "1", "SGLANG_USE_AITER": "0", "OMP_NUM_THREADS": "4", "MAX_JOBS": "4",
               "PYTORCH_TUNABLEOP_ENABLED": "0", "PYTORCH_TUNABLEOP_TUNING": "0",
               "PYTORCH_TUNABLEOP_RECORD_UNTUNED": "0",
               "ROCM_HOME": "/usr/local/lib/python3.14/dist-packages/_rocm_sdk_devel"}
SHA256 = re.compile(r"[a-f0-9]{64}")


def canonical(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def fingerprint(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _mapping(value):
    return value if isinstance(value, dict) else {}


def read_json(path, maximum=2 << 20):
    """Read a bounded non-link JSON artifact; reject duplicate keys and NaN."""
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= maximum:
        raise ValueError("graph input must be a bounded regular non-link file")

    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result:
                raise ValueError("duplicate graph JSON key")
            result[key] = value
        return result

    def constant(_value):
        raise ValueError("nonfinite graph JSON value")

    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("graph input exceeds its bound")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def profile():
    value = read_json(PROFILE)
    if hashlib.sha256(PROFILE.read_bytes()).hexdigest() != PROFILE_SHA256:
        raise ValueError("BF16 graph profile changed; review its source-bound contract")
    return value


def graph_config(batch):
    return {"decode": {"backend": "full", "bs": list(range(1, batch + 1)), "max_bs": batch},
            "prefill": {"backend": "disabled"}}


def validate_workload(workload, pinned):
    """Plan only the measured C2/C4 native-token shapes, with a shared token pool."""
    if (not isinstance(workload, dict) or workload.get("schema") != 1
            or workload.get("model_revision") != pinned["model"]["revision"]
            or workload.get("prefix_state") != "new-prefix"
            or type(workload.get("repetitions")) is not int or not 3 <= workload["repetitions"] <= 20
            or workload.get("requests_per_worker") != 1 or type(workload.get("requests_per_worker")) is not int):
        raise ValueError("BF16 graph workload requires its pinned model, new prefixes and bounded repetitions")
    concurrency, cases = workload.get("concurrency"), workload.get("cases")
    if (not isinstance(concurrency, list) or not concurrency or any(type(c) is not int or c not in (2, 4) for c in concurrency)
            or len(set(concurrency)) != len(concurrency) or not isinstance(cases, list) or not 1 <= len(cases) <= 3):
        raise ValueError("BF16 graph performance scope is concurrency two/four, not eight or batch-three confirmation")
    for case in cases:
        if not isinstance(case, dict) or type(case.get("context_tokens")) is not int or case["context_tokens"] != 4096:
            raise ValueError("BF16 graph context contract is 4096 tokens")
        requests = case.get("requests")
        if requests is None:
            requests = [case] * max(concurrency)
        elif (not isinstance(requests, list) or len(concurrency) != 1 or len(requests) != concurrency[0]
              or "input_ids" in case or "output_tokens" in case):
            raise ValueError("an explicit native wave must contain exactly its declared concurrent requests")
        if sum(len(row.get("input_ids", [])) + row.get("output_tokens", 0) for row in requests
               if isinstance(row, dict) and isinstance(row.get("input_ids", []), list)
               and type(row.get("output_tokens", 0)) is int) > 4096:
            raise ValueError("concurrent native requests exceed the single 4096-token allocation domain")
        for row in requests:
            ids = row.get("input_ids") if isinstance(row, dict) else None
            if (not isinstance(ids, list) or len(ids) != 512 or any(type(token) is not int or not 0 <= token < 151936 for token in ids)
                    or type(row.get("output_tokens")) is not int or row["output_tokens"] != 128
                    or not SHA256.fullmatch(str(row.get("source_sha256")))):
                raise ValueError("BF16 graph performance scope requires native 512/128 IDs and the frozen corpus hash")


def parse_arguments(args, pinned, selected_index=0):
    """No aliases, unresolved variables, duplicate controls or hidden new axes."""
    if not isinstance(args, list) or any(not isinstance(item, str) or not item or "$" in item for item in args):
        raise ValueError("flatten rendered graph arguments before selection")
    wanted = {**pinned["settings"], "--cuda-graph-config": None,
              "--disable-cuda-graph-padding": True, "--base-gpu-id": str(selected_index)}
    parsed, cursor = {}, 0
    while cursor < len(args):
        option = args[cursor]
        cursor += 1
        if option not in wanted or option in parsed:
            raise ValueError("unsupported or duplicate BF16 graph argument: " + option)
        if wanted[option] is True:
            parsed[option] = True
        else:
            if cursor == len(args) or args[cursor].startswith("--"):
                raise ValueError("missing BF16 graph argument value")
            parsed[option] = args[cursor]
            cursor += 1
    if "--base-gpu-id" not in parsed and selected_index == 0:
        parsed["--base-gpu-id"] = "0"  # Source-bound default, not GPU identity.
    expected = {**wanted, "--cuda-graph-config": graph_config(1)}
    try:
        parsed["--cuda-graph-config"] = json.loads(parsed.get("--cuda-graph-config", "null"))
    except (TypeError, ValueError) as error:
        raise ValueError("invalid graph configuration JSON") from error
    if parsed != expected:
        raise ValueError("rendered baseline must be the pinned full-b1 comparator at max-running four")
    return parsed


def cache_paths(pinned, observation, workload):
    """Version the proposed namespace; never create it or qualify cache reuse."""
    pod = _mapping(observation.get("pod"))
    uid = _mapping(pod.get("metadata")).get("uid")
    key = fingerprint({"profile_sha256": fingerprint(pinned), "workload_sha256": fingerprint(workload),
                       "preflight": observation.get("preflight"),
                       "pod_uid": uid})
    return key, {name: f"/cache/{directory}/workstation/{key}" for name, directory in CACHE_NAMES.items()}


def _receipt_failures(pinned, observation, now):
    failures = []

    def check(condition, message):
        if not condition:
            failures.append(message)

    preflight = observation.get("preflight", {})
    try:
        stamp = datetime.fromisoformat(preflight["observed_at"].replace("Z", "+00:00"))
        fresh = stamp.utcoffset() is not None and -60 <= (now - stamp).total_seconds() <= 600
    except (KeyError, TypeError, ValueError, AttributeError):
        fresh = False
    check(fresh, "fresh owner observation is missing or stale (maximum ten minutes)")
    target = pinned["target"]
    check(preflight.get("boot_id") == target["boot_id"] and preflight.get("kernel") == target["kernel_release"]
          and preflight.get("node", {}).get("name") == target["hostname"] and preflight.get("node", {}).get("ready") is True,
          "current target/boot/kernel is outside the measured protocol")
    check(preflight.get("swap") is False and type(preflight.get("active_gpu_allocations")) is int
          and preflight.get("active_gpu_allocations") == 0 and type(preflight.get("device_holders")) is int
          and preflight.get("device_holders") == 0 and preflight.get("session_policy_present") is False,
          "fresh resource/session-policy admission requires the existing owner workflow")
    check(observation.get("runtime_versions") == pinned["runtime"], "runtime/compiler package identity differs")
    check(observation.get("source_before") == pinned["source_pins"]
          and observation.get("source_after") == pinned["source_pins"], "installed 48-file source closure is missing or changed")
    check(observation.get("model_before") == pinned["model"]["artifacts"]
          and observation.get("model_after") == pinned["model"]["artifacts"], "model/tokenizer/template 14-file closure is missing or changed")
    compiler = observation.get("compiler_identity", {})
    check(compiler.get("before") == pinned["compiler"] and compiler.get("after") == pinned["compiler"]
          and compiler.get("unchanged") is True, "image-owned compiler before/after identity differs")
    keys = ("module", "path", "sha256", "replay_signature", "torch_version", "image", "math_flags")
    expected_graphs = {key: pinned["torch_graphs"][key] for key in keys}
    for when in ("before", "after"):
        identity = observation.get("torch_graphs_" + when, {}).get("identity", {})
        check({key: identity.get(key) for key in keys} == expected_graphs,
              "Torch graph module/math/TunableOp identity differs " + when)
    images = observation.get("image", {}).get("images", [])
    check(isinstance(images, list) and len(images) == 1 and images[0].get("id") == pinned["config_digest"]
          and pinned["image"] in images[0].get("repoDigests", []), "actual OCI image/config identity differs")
    topology = observation.get("singleton_topology", {})
    check(topology == {"tp_size": 1, "dp_size": 1, "pp_size": 1, "attn_cp_size": 1, "moe_dp_size": 1, "ep_size": 1},
          "actual TP/DP/attention topology differs")
    check(isinstance(topology, dict) and all(type(value) is int for value in topology.values()),
          "actual topology must contain integer ranks/sizes, not Boolean metadata")
    devices = observation.get("launch", {}).get("devices", [])
    expected_devices = {(row["uuid"], row["bdf"]) for row in pinned["devices"]}
    check(isinstance(devices, list) and len(devices) == 2
          and {(row.get("uuid"), row.get("bdf")) for row in devices if isinstance(row, dict)} == expected_devices,
          "allocated GPU UUID/BDF identity differs")
    selected = observation.get("launch", {}).get("selected", {})
    check(selected.get("uuid") == pinned["scope"]["executed_device_uuid"]
          and selected in devices and type(selected.get("index")) is int and selected.get("index") in (0, 1),
          "executed device UUID/BDF/index is missing or unsupported")
    effective = observation.get("resolved_contract", {})
    check(all(effective.get(key) == value and type(effective.get(key)) is type(value)
              for key, value in pinned["effective"].items()), "effective model/precision/serving controls differ")
    check(effective.get("precision") == pinned["precision"], "observed weight/activation/KV precision differs")
    graph = effective.get("cuda_graph_config", {})
    decode = graph.get("decode", {})
    prefill = graph.get("prefill", {})
    check(decode == {"backend": "full", "max_bs": 1, "bs": [1], "tc_compiler": "eager",
                     "full_prefill_max_req": None, "full_prefill_prefix_chunk_tokens": None}
          and prefill.get("backend") == "disabled" and prefill.get("tc_compiler") == "eager",
          "effective baseline is not full-b1 with disabled prefill")
    policy = observation.get("policy_before", {})
    check(policy.get("uid") == pinned["policy"]["uid"] and policy.get("spec") == {
        "podSelector": {"matchLabels": {"workstation-serving": "baseline"}}, "policyTypes": ["Ingress", "Egress"]},
          "deny NetworkPolicy identity/spec differs")
    check(observation.get("effective_policy_before") == [{"name": pinned["policy"]["name"],
          "uid": pinned["policy"]["uid"], "ingress_rules": 0, "egress_rules": 0}],
          "effective selecting NetworkPolicy differs")
    return failures


def plan(deployment, workload, observation, *, experimental=False, now=None):
    """Emit a guarded opt-in or an unchanged incumbent; never promote a model."""
    if experimental is not True:
        raise ValueError("bf16-full-b4 requires explicit --experimental")
    pinned = profile()
    validate_workload(workload, pinned)
    if not isinstance(observation, dict):
        observation = {}
    template = deployment.get("spec", {}).get("template", {})
    spec = template.get("spec")
    if not isinstance(spec, dict) or not isinstance(spec.get("containers"), list):
        raise ValueError("require a rendered Pod template")
    indexes = [index for index, row in enumerate(spec["containers"]) if isinstance(row, dict) and row.get("name") == "sglang"]
    if len(indexes) != 1:
        raise ValueError("require exactly one SGLang container")
    container = spec["containers"][indexes[0]]
    try:
        receipt_failures = _receipt_failures(pinned, observation, now or datetime.now(timezone.utc))
    except (AttributeError, KeyError, TypeError):
        receipt_failures = ["owner receipt schema is incomplete or malformed"]
    failures = []
    if _mapping(deployment.get("metadata")).get("namespace", "default") != "default":
        failures.append("rendered Deployment namespace is outside the fixed default owner/policy contract")
    security, pod_security = container.get("securityContext", {}), spec.get("securityContext", {})
    if (pod_security.get("runAsNonRoot") is not True or pod_security.get("runAsUser") != 1000
            or pod_security.get("runAsGroup") != 1000 or pod_security.get("fsGroup") != 1000
            or pod_security.get("seccompProfile") != {"type": "RuntimeDefault"}
            or security.get("readOnlyRootFilesystem") is not True or security.get("allowPrivilegeEscalation") is not False
            or security.get("capabilities", {}).get("drop") != ["ALL"] or security.get("capabilities", {}).get("add")
            or security.get("privileged") not in (None, False) or security.get("procMount") not in (None, "Default")
            or any(security.get(name, expected) != expected for name, expected in
                   (("runAsNonRoot", True), ("runAsUser", 1000), ("runAsGroup", 1000)))
            or security.get("seccompProfile", {"type": "RuntimeDefault"}) != {"type": "RuntimeDefault"}
            or spec.get("automountServiceAccountToken") is not False or spec.get("hostAliases")
            or spec.get("hostNetwork") is True or spec.get("hostPID") is True or spec.get("hostIPC") is True):
        failures.append("manifest does not preserve the tested non-root/read-only/seccomp/credential/isolation controls")
    if template.get("metadata", {}).get("labels", {}).get("workstation-serving") != "baseline":
        failures.append("manifest is outside the verified deny-policy selector")
    resources = container.get("resources", {})
    if resources.get("requests") != pinned["resources"] or resources.get("limits") != pinned["resources"]:
        failures.append("manifest is outside the tested Guaranteed 2GPU/8CPU/40Gi/4Gi envelope")
    if container.get("image") != pinned["image"] or container.get("imagePullPolicy") != "Never":
        failures.append("manifest image is not the tested immutable local image")
    command = container.get("command")
    if command not in (["/opt/workstation/sglang/bin/python3", "-m", "sglang.launch_server"],
                       ["/opt/workstation/sglang/bin/python3", "-B", "-m", "sglang.launch_server"]):
        failures.append("manifest must launch the image-owned SGLang module without an unreviewed wrapper")
    pod = _mapping(observation.get("pod"))
    pod_spec, pod_metadata = _mapping(pod.get("spec")), _mapping(pod.get("metadata"))
    observed_rows = pod_spec.get("containers", [])
    observed_containers = [row for row in observed_rows if isinstance(row, dict) and row.get("name") == "sglang"] if isinstance(observed_rows, list) else []
    if (not pod_metadata.get("uid") or pod_metadata.get("namespace") != "default"
            or pod_spec.get("nodeName") != pinned["target"]["hostname"]
            or len(observed_containers) != 1 or observed_containers[0] != container
            or pod_spec.get("securityContext") != pod_security
            or _mapping(pod_metadata.get("labels")).get("workstation-serving") != "baseline"):
        receipt_failures.append("rendered container/security does not match the fresh observed baseline Pod")
    entries = container.get("env", [])
    if not isinstance(entries, list) or any(not isinstance(row, dict) for row in entries):
        raise ValueError("invalid rendered environment")
    names = [row.get("name") for row in entries]
    if any(not isinstance(name, str) or not name for name in names) or len(names) != len(set(names)):
        raise ValueError("duplicate or invalid rendered environment names")
    if container.get("envFrom") or any(set(row) != {"name", "value"} or not isinstance(row.get("value"), str)
                                        or "$" in row["value"] for row in entries):
        failures.append("flatten and review environment references before graph selection")
    values = {row["name"]: row.get("value") for row in entries}
    if any(values.get(name) != value for name, value in ENVIRONMENT.items()):
        failures.append("manifest offline/backend/compiler/TunableOp environment differs")
    if any(name in values and values[name] not in ("0", "false")
           for name in ("SGLANG_USE_AITER_AR", "SGLANG_ROCM_FUSED_DECODE_MLA")):
        failures.append("manifest enables an unreviewed AITER/MLA backend")
    safe_backend_names = set(ENVIRONMENT) | set(CACHE_NAMES) | {"SGLANG_USE_AITER_AR", "SGLANG_ROCM_FUSED_DECODE_MLA"}
    if (set(names) & FORBIDDEN_ENV or any(name.startswith(("NCCL_", "RCCL_", "TORCH", "PYTORCH_", "SGLANG_", "TRITON_", "HSA_", "HIP_", "CUDA_", "ROCBLAS_", "HIPBLASLT_"))
                                        and name not in safe_backend_names for name in names)):
        failures.append("manifest has unreviewed visibility/communication/compilation overrides")
    mounts = container.get("volumeMounts", [])
    cache_mounts = [row for row in mounts if row.get("mountPath") == "/cache"]
    model_mounts = [row for row in mounts if row.get("mountPath") == "/models"]
    if (len(cache_mounts) != 1 or cache_mounts[0].get("readOnly") is True or cache_mounts[0].get("subPath")
            or len(model_mounts) != 1 or model_mounts[0].get("readOnly") is not True):
        failures.append("reuse the reviewed writable cache and read-only model mounts")
    if any(row.get("mountPath") in ("/", "/dev", "/proc", "/sys")
           or str(row.get("mountPath", "")).startswith(("/opt/workstation/sglang", "/usr/local/lib/python3.14")) for row in mounts):
        failures.append("manifest mounts override image-owned source/runtime or device isolation")
    try:
        parse_arguments(container.get("args"), pinned, _mapping(_mapping(observation.get("launch")).get("selected")).get("index", 0))
    except ValueError as error:
        failures.append(str(error))
    manifest_compatible = not failures
    key, caches = cache_paths(pinned, observation, workload)
    snapshots = observation.get("caches")
    if (not isinstance(snapshots, dict) or set(snapshots) != set(CACHE_NAMES)
            or any(not isinstance(snapshots.get(name), dict) or type(snapshots[name].get("bytes")) is not int
                   or snapshots[name] != {"root": caches[name], "files": {}, "bytes": 0}
                   for name in CACHE_NAMES)):
        receipt_failures.append("fresh empty compiler/SGJIT/XDG cache snapshots are missing, reused or outside the proposed identity namespace")
    failures = receipt_failures + failures
    # A proposed patch is review material only. It remains separate from the
    # no-op selection when historical/current/cache evidence has not passed.
    proposed = copy.deepcopy(spec)
    if manifest_compatible:
        target = proposed["containers"][indexes[0]]
        args = list(target["args"])
        args[args.index("--cuda-graph-config") + 1] = json.dumps(graph_config(4), separators=(",", ":"))
        target["args"] = args
        target["env"] = [row for row in target.get("env", []) if row["name"] not in CACHE_NAMES]
        target["env"] += [{"name": name, "value": value} for name, value in sorted(caches.items())]
    selected = "incumbent" if failures else pinned["profile"]
    candidate = proposed if not failures else copy.deepcopy(spec)
    patch = [{"op": "test", "path": "/spec/template", "value": copy.deepcopy(template)},
             {"op": "replace", "path": "/spec/template/spec", "value": candidate}]
    candidate_template = {**copy.deepcopy(template), "spec": copy.deepcopy(candidate)}
    rollback = [{"op": "test", "path": "/spec/template", "value": candidate_template},
                {"op": "replace", "path": "/spec/template/spec", "value": copy.deepcopy(spec)}]
    return {"schema": 1, "status": "plan-only-model-unqualified", "requested": pinned["profile"], "selected": selected,
            "fallback_reasons": failures, "whole_model_qualified": False, "automatic_apply": False,
            "profile_sha256": PROFILE_SHA256, "workload_sha256": fingerprint(workload),
            "observation_sha256": fingerprint(observation), "cache_identity": key, "proposed_cache_paths": caches,
            "cache_admission": {"status": "NOT RUN", "reuse": "NOT QUALIFIED",
                "sampled_bound_bytes": pinned["cache"]["sampled_output_cache_bound_bytes"], "hard_filesystem_quota": False,
                "requires": "Existing owner must create private fresh paths, check emptiness/headroom under FD9 and enforce sampled bounds before launch."},
            "patch": patch, "rollback": rollback, "graph_rollback": "Exact full-b1 comparator at unchanged max-running four.",
            "conditional_patch": ([{"op": "test", "path": "/spec/template", "value": copy.deepcopy(template)},
                                   {"op": "replace", "path": "/spec/template/spec", "value": proposed}]
                                  if manifest_compatible and failures else None),
            "conditional_status": "Review only; NOT SELECTED/NOT ADMITTED. Fresh owner evidence and cache admission are required.",
            "eager_selection": {"action": "Remove --cuda-graph-config and --disable-cuda-graph-padding; add --disable-cuda-graph in a fresh owner launch.",
                "max_running_requests": "unchanged; no automatic concurrency change", "qualification": "separate original eager selection, not a C2/C4 gain claim"},
            "provenance": pinned["provenance"], "launch_requires": "Recheck current image/source/model/device/effective settings, policy, lock and cache admission through the existing owner workflow; this offline plan is not launch authority."}


def emit(output, result):
    """Create a private fresh, reviewable configuration package; no OCI changes."""
    output = Path(output)
    output.mkdir(mode=0o700)
    os.chmod(output, 0o700)
    files = {"plan.json": result, "profile.json": profile(), "bf16-full-b4.json": result["patch"],
             "incumbent-rollback.json": result["rollback"],
             "identity.json": {key: result[key] for key in ("profile_sha256", "workload_sha256", "observation_sha256",
                            "selected", "cache_identity", "proposed_cache_paths", "cache_admission")}}
    if result["conditional_patch"] is not None:
        files["conditional-proposal.json"] = {"status": result["conditional_status"], "automatic_apply": False,
                                               "patch": result["conditional_patch"]}
    hashes = {}
    for name, value in files.items():
        raw = canonical(value)
        with (output / name).open("xb") as stream:
            stream.write(raw)
        os.chmod(output / name, 0o600)
        hashes[name] = hashlib.sha256(raw).hexdigest()
    with (output / "SOURCE.sha256").open("x", encoding="ascii") as stream:
        for name, value in sorted(hashes.items()):
            stream.write(f"{value}  {name}\n")
    os.chmod(output / "SOURCE.sha256", 0o600)


def historical_example(output):
    """Write controller-only historical inputs, never current/fresh admission."""
    pinned = profile()
    directory = ROOT / pinned["provenance"]["confirmation"]
    manifest = directory / "FINAL-ARTIFACTS.sha256"
    if hashlib.sha256(manifest.read_bytes()).hexdigest() != pinned["provenance"]["confirmation_manifest_sha256"]:
        raise ValueError("historical graph manifest changed")
    hashes = dict(line.split("  ", 1)[::-1] for line in manifest.read_text(encoding="ascii").splitlines())

    def artifact(name):
        path = directory / name
        value = read_json(path)
        if hashlib.sha256(path.read_bytes()).hexdigest() != hashes.get(name):
            raise ValueError("historical graph input changed: " + name)
        return value

    base = "verified/probe-0/"
    names = {"pod": "evidence/admitted.json", "image": "evidence/image.json", "runtime_versions": "output/runtime-versions.json",
             "source_before": "output/source-before.json", "source_after": "output/source-after.json",
             "model_before": "output/model-before.json", "model_after": "output/model-after.json",
             "compiler_identity": "output/compiler-identity.json", "torch_graphs_before": "output/torch-graphs-before.json",
             "torch_graphs_after": "output/torch-graphs-after.json", "resolved_contract": "output/resolved-contract.json",
             "singleton_topology": "output/singleton-topology.json", "launch": "output/launch.json",
             "policy_before": "evidence/policy-before.json", "effective_policy_before": "evidence/effective-policy-before.json"}
    observation = {name: artifact(base + path) for name, path in names.items()}
    observation["preflight"] = artifact("preflight.json")
    observation["historical_scope"] = "Sealed different-process evidence, NOT CURRENT. No cache snapshots or timestamps are manufactured."
    config = artifact("probe-0-source/probe-config.json")
    frozen = artifact(base + "output/frozen-inputs.json")
    pod = artifact("probe-0-source/pod.json")
    spec = copy.deepcopy(pod["spec"])
    target = spec["containers"][0]
    target["name"] = "sglang"
    target["command"] = ["/opt/workstation/sglang/bin/python3", "-m", "sglang.launch_server"]
    target["args"] = list(config["server_args"]) + ["--base-gpu-id", str(observation["launch"]["selected"]["index"])]
    target["env"] += [{"name": name, "value": value} for name, value in ENVIRONMENT.items()]
    deployment = {"kind": "Deployment", "metadata": {"name": "historical-graph-example-not-for-deployment"},
        "spec": {"replicas": 0, "template": {"metadata": {"labels": copy.deepcopy(pod["metadata"]["labels"]),
        "annotations": {"workstation.ai/qualification": "historical-rendered-projection-not-current"}}, "spec": spec}}}
    corpus_hash = hashes[base + "output/frozen-inputs.json"]
    requests = [{"input_ids": frozen["concurrent_cases"][f"agent-{index}"], "output_tokens": 128,
                 "source_sha256": corpus_hash} for index in range(4)]
    workload = {"schema": 1, "profile": "batch", "model_revision": pinned["model"]["revision"], "prefix_state": "new-prefix",
                "concurrency": [4], "repetitions": 3, "requests_per_worker": 1,
                "cases": [{"context_tokens": 4096, "requests": requests}]}
    output = Path(output)
    output.mkdir(mode=0o700)
    os.chmod(output, 0o700)
    for name, value in (("deployment.json", deployment), ("workload.json", workload), ("graph-observation.json", observation)):
        with (output / name).open("xb") as stream:
            stream.write(canonical(value))
        os.chmod(output / name, 0o600)
