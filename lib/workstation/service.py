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


# Kubernetes reads manifests as YAML 1.1, where bare y/n/yes/no/on/off (any case)
# are booleans; PyYAML only quotes the strings it would itself resolve, so a label
# such as ai-stack/arm: Y reached the API server as true. Quote every such string.
YAML11_SPECIAL = re.compile(r"(?i:y|n|yes|no|on|off|true|false|null|~)\Z")


class KubeDumper(yaml.SafeDumper):
    pass


def _kube_str(dumper, value):
    if "\n" in value:
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|")
    if YAML11_SPECIAL.match(value):
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="'")
    return dumper.represent_str(value)


KubeDumper.add_representer(str, _kube_str)


def kube_yaml(document):
    return yaml.dump(document, Dumper=KubeDumper, sort_keys=False)


def kube_yaml_all(documents):
    return yaml.dump_all(documents, Dumper=KubeDumper, sort_keys=False)

from telemetry import gpu_claims, kustomize, load_objects, new_directory, pod_budget, pod_spec, sha, verify


ROOT = Path(__file__).resolve().parents[2]
CANDIDATE_ALIAS = "Qwen3.8-27B-MXFP4"
LEGACY_ALIAS = "Qwen3.8-27B-FP8"
# Key provisioning allowlist. The MXFP4 alias is listed so revocable keys can be
# issued for it before a contract render routes it; until then the gateway
# refuses requests for it as an unknown model.
MODELS = {LEGACY_ALIAS, "Qwen3.5-9B", CANDIDATE_ALIAS}


def set_gpu_claims(spec, container, count):
    """Use the first `count` DRA GPU claims (gpu-0..) instead of amd.com/gpu."""
    require(type(count) is int and 1 <= count <= 2, "one or two GPUs")
    names = [f"gpu-{index}" for index in range(count)]
    spec["resourceClaims"] = [{"name": name, "resourceClaimName": name} for name in names]
    container["resources"]["claims"] = [{"name": name} for name in names]
    for section in ("requests", "limits"):
        container["resources"][section].pop("amd.com/gpu", None)


def require(condition, message):
    if not condition:
        raise ValueError(message)


# Native admission and gateway admission are different limits. The gateway
# admits four concurrent requests; the selected runtime executes at most two.
GATEWAY_ADMISSION = 4
REQUEST_DEFAULT_KEYS = ("chat_template_kwargs", "reasoning_effort", "temperature", "top_k", "top_p",
                        "max_completion_tokens", "seed")
# Principal application baseline: thinking enabled at the selected effort with
# greedy decoding, the recipe whose natural-stop answers were graded (operation
# 232, now the rollback arm). The selected arm's own measured sampling profile
# is reported separately and treated as an alternative recipe.
BASELINE_SAMPLING = {"temperature": 0.0, "top_k": 1, "top_p": 1.0}
SAMPLING_KEYS = ("temperature", "top_p", "top_k")
HEX = re.compile(r"[a-f0-9]{64}")


def _digest_field(record, *path):
    value = record
    for key in path:
        require(isinstance(value, dict) and key in value, "candidate evidence lacks " + "/".join(path))
        value = value[key]
    require(isinstance(value, str) and HEX.fullmatch(value) is not None, "candidate digest is invalid: " + "/".join(path))
    return value


def _bounded_file(path, limit):
    path = Path(path)
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= limit,
            "candidate evidence must be a bounded regular file: " + path.name)
    return path.read_bytes()


def package_evidence(root, record):
    """Authenticate allowlisted identities through pointer -> MANIFEST -> review files.

    Only digests, versions and request scalars leave this function; machine
    paths, CLI values, environment and private request text do not.
    """
    import zlib, base64
    root = Path(root)
    manifest_raw = _bounded_file(root / "MANIFEST.json", 2 << 20)
    require(hashlib.sha256(manifest_raw).hexdigest() == record["candidate_package_base"]["manifest_sha256"],
            "package MANIFEST.json does not match the selected pointer; supply the selected package-v2 root")
    manifest = json.loads(manifest_raw)
    files = manifest.get("package_files")
    require(isinstance(files, dict) and manifest.get("launch_authorized") is False, "unexpected package manifest")

    def member(name, limit):
        raw = _bounded_file(root / name, limit)
        require(HEX.fullmatch(str(files.get(name, ""))) is not None
                and hashlib.sha256(raw).hexdigest() == files[name], "package member changed: " + name)
        return raw

    inventory = {row["name"]: row["sha256"] for row in json.loads(member("review/MODEL-INVENTORY.json", 65536))}
    draft = json.loads(member("review/DRAFT-MODEL.json", 65536))["config"]
    stack = json.loads(member("review/COMMON-STACK-RECEIPT.json", 8 << 20))
    resources = json.loads(member("review/RESOURCE-MANIFEST.json", 8 << 20))
    bundle = json.loads(zlib.decompress(member("review/FINAL-REQUESTS.zlib", 64 << 20), bufsize=1 << 20))
    rows = json.loads(zlib.decompress(base64.b64decode(bundle["CLIENT-BINDING.zlib"])))["rows"]
    # Report each measured request parameter once; a parameter that varied
    # across measured requests (for example a per-request seed) is "varies".
    measured = {}
    for key in REQUEST_DEFAULT_KEYS:
        values = {json.dumps(row["body"].get(key), sort_keys=True) for row in rows}
        measured[key] = json.loads(values.pop()) if len(values) == 1 else "varies"
    require(measured["chat_template_kwargs"] == {"enable_thinking": True}
            and measured["reasoning_effort"] in {"low", "medium", "xhigh"},
            "measured request profile is not thinking-enabled at a template-supported effort")
    environment = {}
    for argv in resources.get("workload_argv", {}).values():
        environment = {argv[i + 1]: argv[i + 2] for i in range(len(argv) - 2) if argv[i] == "--setenv"}
        break
    components, target = stack.get("components", {}), stack.get("target", {})
    require(inventory.get("config.json") == record["selected_runtime"]["resolved_contract"].get("model_config_sha256"),
            "model inventory config differs from the selected resolved contract")
    require(draft.get("dflash_config", {}).get("block_size") == record["selected_runtime"].get("dflash_width"),
            "draft model block size differs from the selected DFlash width")
    return {
        "package_manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "model_files": {name: inventory[name] for name in sorted(inventory)
                        if name.endswith((".json", ".jinja", ".txt")) or name == "model.safetensors"},
        # The checkpoint ships no chat_template.jinja; the template is carried by
        # tokenizer_config.json, so its digest is the template identity.
        "chat_template_source": "chat_template.jinja" if "chat_template.jinja" in inventory else "tokenizer_config.json",
        "draft": {"architectures": draft.get("architectures"), "block_size": draft["dflash_config"]["block_size"],
                  "dtype": draft.get("dtype")},
        "stack": {"sglang": target.get("sglang"), "sglang_revision": target.get("source_revision"),
                  "rocm_sdk": components.get("sdk", {}).get("version"),
                  "torch_receipt": components.get("torch", {}).get("version"),
                  "python": components.get("python", {}).get("version"),
                  "base_image_manifest": stack.get("image_manifest"), "base_image_config": stack.get("image_config"),
                  "composition_argv_sha256": resources.get("workload_argv_sha256"),
                  "composition_note": "The measured launch overlays separately built site-packages trees on the base image; "
                                      "the version receipt alone does not identify them."},
        "runtime_file_count": len(manifest.get("runtime_files", {})),
        "runtime_environment": {key: environment[key] for key in (
            "LLVM_PATH", "TORCH_BLAS_PREFER_HIPBLASLT", "ROCBLAS_USE_HIPBLASLT", "SGLANG_USE_AITER",
            "SGLANG_DISABLE_DRAFT_EXTEND_CUDA_GRAPH", "HF_HUB_OFFLINE") if key in environment},
        "package_request_profile": measured,
        "measured_requests": len(rows),
    }


def candidate_contract(pointer, expected_sha, package_root=None, selection=None):
    """Read selected metadata only; never import or launch a candidate packet.

    ``selection`` is the hash-pinned public finalization summary. The pointer
    must be the one it names: an older pointer is stale even when its own
    digest is valid.
    """
    path = Path(pointer)
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 131072,
            "candidate pointer must be a bounded regular file")
    data = path.read_bytes()
    require(isinstance(expected_sha, str) and HEX.fullmatch(expected_sha) is not None
            and hashlib.sha256(data).hexdigest() == expected_sha, "candidate pointer digest differs")
    if selection is None:
        from stack_inventory import serving_project
        selection = serving_project(ROOT)["selection"]
    require(expected_sha not in selection.get("superseded_pointer_sha256", [selection["previous_pointer_sha256"]]),
            f"stale candidate selection: pointer {expected_sha[:12]} was superseded by "
            f"{selection['pointer_sha256'][:12]}; supply the current pointer named by the imported selection record")
    require(expected_sha == selection["pointer_sha256"],
            "pointer is not the selection named by infrastructure/sglang/serving-project.json; "
            "refresh the serving import if the owner changed the selection")
    record = json.loads(data)
    require(isinstance(record, dict) and record.get("schema") == 1
            and record.get("candidate_selected") is True, "supply an explicitly selected candidate pointer")
    require(record.get("deployment") is False, "pointer claims deployment; this planner only accepts private selection")
    runtime = record.get("selected_runtime", {})
    require(isinstance(runtime, dict), "invalid selected runtime")
    cli = runtime.get("canonical_server_cli")
    require(isinstance(cli, list) and len(cli) <= 512
            and all(isinstance(arg, str) and len(arg) <= 4096 for arg in cli), "invalid candidate arguments")
    require(cli == selection["canonical_server_cli"], "pointer CLI differs from the finalization record's selected CLI")
    resolved = runtime.get("resolved_contract", {})
    require(isinstance(resolved, dict) and resolved == selection["resolved_contract"],
            "pointer resolved contract differs from the finalization record")

    def option(name, optional=False):
        positions = [i for i, arg in enumerate(cli) if arg == name]
        if optional and not positions:
            return None
        require(len(positions) == 1 and positions[0] + 1 < len(cli), "missing or duplicate candidate option " + name)
        value = cli[positions[0] + 1]
        require(not value.startswith("--"), "candidate option lacks a value: " + name)
        return value

    model = option("--served-model-name")
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:-]{0,199}", model) is not None and ".." not in model,
            "invalid served model identifier")
    revision = option("--revision")
    draft_revision = option("--speculative-draft-model-revision")
    require(all(re.fullmatch(r"[a-f0-9]{40}", value) is not None for value in (revision, draft_revision)),
            "candidate model and draft revisions must be pinned")
    numbers = {}
    for flag, key, low, high in (("--context-length", "context_length", 2048, 262144),
            ("--max-total-tokens", "max_total_tokens", 2048, 1048576),
            ("--max-running-requests", "max_running_requests", 1, 8),
            ("--tp-size", "tp_size", 1, 2),
            ("--chunked-prefill-size", "chunked_prefill_size", 256, 65536)):
        value = option(flag)
        require(re.fullmatch(r"[1-9][0-9]{0,6}", value) is not None, "invalid candidate token/resource bound")
        number = int(value)
        require(low <= number <= high and type(resolved.get(key)) is int and resolved[key] == number,
                f"candidate CLI and resolved contract differ for {key}")
        numbers[key] = number
    require(numbers["max_total_tokens"] >= numbers["context_length"], "candidate pool is below one context")
    states = option("--mamba-max-states-per-path")
    require(resolved.get("prefix_cache", {}).get("mamba_max_states_per_path") == int(states),
            "candidate recurrent-state cap differs from the resolved contract")
    width = int(option("--speculative-num-draft-tokens"))
    require(width == runtime.get("dflash_width") == int(option("--speculative-dflash-block-size")),
            "candidate DFlash width is inconsistent")
    package = record.get("candidate_package_base", {})
    require(isinstance(package, dict), "candidate package is missing")
    for key in selection["package"]:
        require(_digest_field(package, key) == selection["package"][key], "pointer package differs from finalization: " + key)
    arm = package.get("selected_arm")
    require(isinstance(arm, dict) and all(arm.get(key) == runtime.get(runtime_key) and arm.get(key) is not None
            for key, runtime_key in (("logical", "logical_arm"), ("physical", "physical_arm"),
                                     ("start", "start"), ("operation", "operation")))
            and {key: arm[key] for key in ("logical", "physical", "start")} == selection["selected_arm"]
            and arm["operation"] == selection["operation"], "candidate arm selection differs (mixed arms)")
    rollback = record.get("rollback", {})
    require(isinstance(rollback, dict) and rollback.get("launch_authorized") is False
            and {"operation": rollback.get("operation"), "logical": rollback.get("logical_arm"),
                 "physical": rollback.get("physical_arm"), "start": rollback.get("start")}
            == {k: selection["rollback"][k] for k in ("operation", "logical", "physical", "start")},
            "candidate rollback differs from the finalization record")
    quantization, kv_dtype = option("--quantization"), option("--kv-cache-dtype")
    draft_kv = option("--speculative-draft-kv-cache-dtype")
    require(quantization == "quark" and kv_dtype == "bf16" == resolved.get("kv_cache_dtype")
            and draft_kv == "bf16" == resolved.get("speculative_draft_kv_cache_dtype"),
            "this plan requires the Quark weights with explicit BF16 target and draft KV")
    reasoning, tools = option("--reasoning-parser", True), option("--tool-call-parser", True)
    require(reasoning in {None, "qwen3"} and tools in {None, "qwen3_coder"}, "unreviewed candidate parser")
    evidence = package_evidence(package_root, record) if package_root else None
    missing = [] if evidence else ["selected package-v2 root (MANIFEST.json and review/) for model, tokenizer, "
                                   "template, stack and request-default identities"]
    # Only allowlisted metadata leaves the pointer. CLI keys, paths and values
    # for authentication, environment or arbitrary loader options are omitted.
    return {"schema": 2, "pointer_sha256": expected_sha,
            "previous_pointer_sha256": selection["previous_pointer_sha256"],
            "operation": arm["operation"], "selected_arm": dict(selection["selected_arm"]),
            "package_sha256": package["package_sha256"],
            "package": {key: package[key] for key in ("archive_sha256", "manifest_sha256", "source_manifest_sha256",
                                                      "worker_source_manifest_sha256")},
            "runtime_identity_sha256": _digest_field(runtime, "runtime_identity", "sha256"),
            "http_binding_sha256": _digest_field(runtime, "http_binding", "sha256"),
            "served_model": model, "client_alias": CANDIDATE_ALIAS,
            "model_revision": revision, "draft_model_revision": draft_revision,
            "model_config_sha256": resolved.get("model_config_sha256"), **numbers,
            "quantization": quantization, "weight_format": "mxfp4" if "MXFP4" in str(record.get("candidate")) else None,
            "kv_cache_dtype": kv_dtype, "draft_kv_cache_dtype": draft_kv,
            "speculative": {"algorithm": option("--speculative-algorithm"), "width": width},
            "mamba_max_states_per_path": int(states),
            "reasoning_parser": reasoning, "tool_call_parser": tools,
            "parser_status": "absent-from-selected-cli; effective runtime parsing not observed",
            # The packaged frozen request binding and the profile the selection
            # record says the selected arm was measured with can differ (a
            # BetterBench run samples); both are reported, neither is merged.
            "package_request_profile": evidence["package_request_profile"] if evidence else None,
            "selection_measured_profile": selection.get("measured_request_profile"),
            "measured_output_tokens": evidence["package_request_profile"]["max_completion_tokens"] if evidence else None,
            # Baseline recipe projected into clients: the measured thinking switch
            # and effort of the selected arm with greedy decoding.
            "request_defaults": {"chat_template_kwargs": evidence["package_request_profile"]["chat_template_kwargs"],
                                 "reasoning_effort": evidence["package_request_profile"]["reasoning_effort"],
                                 **BASELINE_SAMPLING} if evidence else None,
            "members": list(selection.get("members", [])),
            "frozen_verdict": selection.get("frozen_verdict"),
            "identity": {key: evidence[key] for key in ("package_manifest_sha256", "model_files", "chat_template_source",
                                                       "draft", "stack", "runtime_file_count", "runtime_environment")}
                        if evidence else None,
            "tested_deviations": list(runtime.get("tested_deviations", [])),
            "limits": list(selection["limits_and_deviations"]),
            "rollback": dict(selection["rollback"]),
            "missing_evidence": missing, "installed": False, "runtime_observed": False}


def client_profile(mode):
    require(mode in {"large", "split", "shared"}, "unknown client mode")
    # shared renders always carry a serving contract, which replaces these values
    return ({"AGENT_MODEL": "Qwen3.8-27B-FP8", "AGENT_CONTEXT_TOKENS": 32768, "AGENT_MAX_OUTPUT_TOKENS": 4096}
            if mode in {"large", "shared"} else
            {"AGENT_MODEL": "Qwen3.5-9B", "AGENT_CONTEXT_TOKENS": 4096, "AGENT_MAX_OUTPUT_TOKENS": 1024})


def candidate_client_profile(contract, output_tokens):
    """Client values projected from the explicit candidate contract only."""
    require(type(output_tokens) is int and 128 <= output_tokens <= contract["context_length"] // 2,
            "output allowance must be independent and at most half the context")
    values = {"AGENT_MODEL": contract["client_alias"], "AGENT_CONTEXT_TOKENS": contract["context_length"],
              "AGENT_MAX_OUTPUT_TOKENS": output_tokens}
    defaults = contract.get("request_defaults")
    if defaults:
        values.update({"AGENT_ENABLE_THINKING": defaults["chat_template_kwargs"]["enable_thinking"],
                       "AGENT_REASONING_EFFORT": defaults["reasoning_effort"]})
        # Sampling is projected only when the contract fixes it; otherwise the
        # server applies the checkpoint's generation defaults.
        values.update({"AGENT_" + key.upper(): defaults[key] for key in SAMPLING_KEYS if key in defaults})
    return values


def candidate_render(contract, output_tokens, deadline_seconds, reserve_tokens):
    """Render inactive gateway, router, browser and client values for one candidate.

    Every layer is derived from the same contract so the checks below can
    compare them. Nothing here changes the active profile or its sources.
    """
    client = candidate_client_profile(contract, output_tokens)
    require(type(deadline_seconds) is int and 30 <= deadline_seconds <= 3600, "deadline must be 30..3600 seconds")
    require(type(reserve_tokens) is int and 0 <= reserve_tokens <= 4096, "scheduler reserve must be 0..4096 tokens")
    context, alias = contract["context_length"], contract["client_alias"]
    route = {"model_name": alias,
             "litellm_params": {"model": "openai/" + contract["served_model"], "api_base": "http://sglang:30000/v1",
                                "api_key": "local-internal-route", "timeout": deadline_seconds - 10, "max_retries": 0},
             "model_info": {"mode": "chat", "max_tokens": context, "max_output_tokens": output_tokens,
                            "max_input_tokens": context - output_tokens - reserve_tokens,
                            "input_cost_per_token": 0, "output_cost_per_token": 0}}
    gateway = {"chat_models": {alias: [context, output_tokens]}, "request_timeout": deadline_seconds,
               "admission_limit": GATEWAY_ADMISSION}
    params = {"function_calling": "native"}
    defaults = contract.get("request_defaults")
    if defaults:
        # Source-controlled DEFAULT_MODEL_PARAMS; Open WebUI copies these to the
        # top level of each request. The output cap is left to the gateway
        # default because this variable also applies to the 9B model.
        params.update({key: defaults[key] for key in ("temperature", "top_k", "top_p") if key in defaults})
        params.update({"reasoning_effort": defaults["reasoning_effort"],
                       "custom_params": {"chat_template_kwargs": defaults["chat_template_kwargs"]}})
    webui = {"DEFAULT_MODELS": alias, "DEFAULT_MODEL_PARAMS": json.dumps(params, separators=(",", ":"))}
    request = {"model": alias, "max_tokens": output_tokens,
               **({key: params[key] for key in ("temperature", "top_k", "top_p", "reasoning_effort") if key in params}
                  if defaults else {}),
               **(params["custom_params"] if defaults else {})}
    native = {"served_model": contract["served_model"], "context_length": context,
              "max_total_tokens": contract["max_total_tokens"], "max_running_requests": contract["max_running_requests"]}
    checks = [
        {"id": "alias-to-native", "matches": route["litellm_params"]["model"] == "openai/" + native["served_model"]},
        {"id": "router-context", "matches": route["model_info"]["max_tokens"] == context == client["AGENT_CONTEXT_TOKENS"]},
        {"id": "gateway-context", "matches": gateway["chat_models"][alias][0] == context},
        {"id": "output-cap", "matches": gateway["chat_models"][alias][1] == route["model_info"]["max_output_tokens"]
                                       == client["AGENT_MAX_OUTPUT_TOKENS"] == request["max_tokens"]},
        {"id": "input-budget", "matches": route["model_info"]["max_input_tokens"] + output_tokens + reserve_tokens == context},
        {"id": "per-request-within-pool", "matches": context <= native["max_total_tokens"]},
        {"id": "deadline-order", "matches": route["litellm_params"]["timeout"] < gateway["request_timeout"]},
        {"id": "request-defaults", "matches": bool(defaults) and request.get("reasoning_effort") == client.get("AGENT_REASONING_EFFORT")
                                              and request.get("chat_template_kwargs") == defaults["chat_template_kwargs"]},
    ]
    capacity = {"gateway_admission": GATEWAY_ADMISSION, "native_running": native["max_running_requests"],
                "queued_at_native_when_full": max(0, GATEWAY_ADMISSION - native["max_running_requests"]),
                "aggregate_token_pool": native["max_total_tokens"],
                "note": "Gateway admission and native execution are separate limits; requests beyond two wait in the "
                        "native scheduler queue and count against the client deadline."}
    return {"route": route, "gateway": gateway, "webui": webui, "client": client, "request": request,
            "capacity": capacity, "checks": checks}


# Response parsing for the application profile. These are the parsers the
# existing service profile uses; the pinned template's thinking/tool format
# matches them. They change response shape, not kernels, and are a labelled
# deviation from the measured campaign CLI that the assistant API suite must
# qualify natively before activation.
APPLICATION_PARSERS = ["--reasoning-parser", "qwen3", "--tool-call-parser", "qwen3_coder"]
CAMPAIGN_ONLY = {"--host": "0.0.0.0", "--port": "30000"}
# Reviewed launch flags of the selected runtime. A selection that adds any other
# flag (for example a credential option) needs review before it is rendered.
LAUNCH_FLAGS = frozenset((
    "--attention-backend",
    "--chunked-prefill-size",
    "--context-length",
    "--cuda-graph-backend-decode",
    "--cuda-graph-backend-prefill",
    "--cuda-graph-bs-decode",
    "--cuda-graph-max-bs-decode",
    "--decode-attention-backend",
    "--decode-log-interval",
    "--disable-custom-all-reduce",
    "--disable-overlap-schedule",
    "--dtype",
    "--enable-fp32-lm-head",
    "--enable-linear-replayssm-spec",
    "--enable-metrics",
    "--host",
    "--kv-cache-dtype",
    "--load-format",
    "--mamba-max-states-per-path",
    "--mamba-radix-cache-strategy",
    "--max-mamba-cache-size",
    "--max-running-requests",
    "--max-total-tokens",
    "--mem-fraction-static",
    "--model-loader-extra-config",
    "--model-path",
    "--page-size",
    "--port",
    "--prefill-attention-backend",
    "--quantization",
    "--random-seed",
    "--revision",
    "--served-model-name",
    "--speculative-algorithm",
    "--speculative-dflash-block-size",
    "--speculative-draft-kv-cache-dtype",
    "--speculative-draft-model-path",
    "--speculative-draft-model-quantization",
    "--speculative-draft-model-revision",
    "--speculative-eagle-topk",
    "--speculative-num-draft-tokens",
    "--speculative-num-steps",
    "--startup-weight-load-mode",
    "--tokenizer-worker-num",
    "--tp-size",
    "--weight-loader-drop-cache-after-load",
))


def candidate_runtime(contract, objects, runtime_image=None):
    """Inactive persistent SGLang Deployment for the selected candidate.

    It reuses the existing sglang Deployment (probes, security context, cache
    and model volumes, Recreate strategy, termination grace) and replaces only
    the runtime image, launch arguments, environment and GPU count. Paths are
    the existing /models/<name>/<revision> convention. Replicas stay zero.
    """
    base = next(o for o in objects if o["kind"] == "Deployment" and o["metadata"]["name"] == "sglang")
    deployment = copy.deepcopy(base)
    from stack_inventory import serving_project
    cli = list(serving_project(ROOT)["selection"]["canonical_server_cli"])
    paths = {"--model-path": f"/models/Qwen3.8-27B-MXFP4/{contract['model_revision']}",
             "--speculative-draft-model-path": f"/models/Qwen3.8-27B-DFlash2-draft/{contract['draft_model_revision']}",
             **CAMPAIGN_ONLY}
    for flag, value in paths.items():
        position = cli.index(flag)
        cli[position + 1] = value
    require(not any(flag in cli for flag in APPLICATION_PARSERS[::2]), "selected CLI already binds a parser")
    unknown = sorted({arg for arg in cli if arg.startswith("--")} - LAUNCH_FLAGS)
    require(not unknown, "unreviewed launch flag in the selected CLI: " + ", ".join(unknown))
    image_ok = isinstance(runtime_image, str) and re.fullmatch(r"localhost/[a-z0-9./_-]+@sha256:[a-f0-9]{64}", runtime_image)
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    container["image"] = runtime_image if image_ok else "registry.invalid/ai-home-lab/sglang-candidate:UNBUILT"
    container["imagePullPolicy"] = "Never"
    container["command"] = ["python3", "-m", "sglang.launch_server"]
    container["args"] = cli + APPLICATION_PARSERS
    container.pop("envFrom", None)
    environment = (contract.get("identity") or {}).get("runtime_environment", {})
    kept = [e for e in container.get("env", []) if e["name"] not in environment]
    container["env"] = kept + [{"name": key, "value": value} for key, value in sorted(environment.items())]
    set_gpu_claims(deployment["spec"]["template"]["spec"], container, int(contract["tp_size"]))
    deployment["spec"]["replicas"] = 0
    deployment["metadata"].setdefault("annotations", {}).update({
        "ai-home-lab.local/qualification": "candidate-inactive-not-qualified",
        "ai-home-lab.local/candidate-pointer-sha256": contract["pointer_sha256"],
        "ai-home-lab.local/candidate-package-sha256": contract["package_sha256"]})
    deviations = [{"id": "application-parsers", "flags": APPLICATION_PARSERS,
                   "reason": "separate reasoning and structured tool calls for browser and coding clients"},
                  {"id": "listen-address", "flags": ["--host", "0.0.0.0", "--port", "30000"],
                   "reason": "persistent ClusterIP service instead of the campaign loopback port"}]
    blockers = []
    if not image_ok:
        blockers.append("runtime image: build an OCI image reproducing the measured composition (base manifest "
                        f"{(contract.get('identity') or {}).get('stack', {}).get('base_image_manifest', 'unknown')}, the "
                        "site-packages, devel and composition trees and the package overlays) and pass its local digest")
    blockers += ["model assets: stage the MXFP4 checkpoint and DFlash draft at the rendered /models paths and verify "
                 "them against the authenticated model inventory",
                 "startup numerical probes: the package's probes run inside the finite campaign worker; a persistent "
                 "entry that runs them without the campaign client and shutdown needs an owner-reviewed serving change"]
    return {"deployment": deployment, "deviations": deviations, "blockers": blockers,
            "readiness": ["runtime file digests match the package manifest before launch",
                          "/health passes (existing startup and readiness probes)",
                          "startup numerical probes pass",
                          "gateway phase set ready for the candidate alias by the root session owner"],
            "shutdown": "drain through service_session (180 s wait, then cancel), then scale to zero; "
                        "terminationGracePeriodSeconds is inherited from the active Deployment",
            "rollback": "re-apply the previously rendered active sglang Deployment and gateway configuration; "
                        "the native rollback arm is operation " + str(contract["rollback"]["operation"])}


def candidate_plan(args):
    """Prepare an offline integration comparison and a frozen quality menu."""
    contract = candidate_contract(args.pointer, args.pointer_sha256, getattr(args, "package_root", None))
    rendered = candidate_render(contract, args.output_tokens, getattr(args, "deadline_seconds", 180),
                                getattr(args, "reserve_tokens", 16))
    objects = kustomize(ROOT / "apps/overlays/assistant")
    native = candidate_runtime(contract, objects, getattr(args, "runtime_image", None))
    runtime = next(o for o in objects if o["kind"] == "ConfigMap"
                   and "boundary.py" in o.get("data", {}))
    # Inspect the rendered source without executing it or inspecting credentials.
    tree = ast.parse(runtime["data"]["boundary.py"])
    models = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id == "CHAT_MODELS" for target in node.targets))
    fields = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id == "CHAT_FIELDS" for target in node.targets))
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
        {"id": "gateway-request-fields", "matches": set(rendered["request"]) <= set(fields) | {"model"},
         "current": sorted(set(rendered["request"]) - set(fields)), "candidate": sorted(rendered["request"])},
    ]
    menu_path = ROOT / "infrastructure/sglang/assistant-quality.json"
    menu = json.loads(menu_path.read_text())
    pending = ["Package the selected native runtime and weights for a persistent authenticated endpoint.",
               "Verify model identity and actual tokenizer accounting, including templates, tools and output reserve.",
               "Qualify native reasoning and tool-call parsers through the gateway and browser.",
               "Measure the requested output allowance and client/gateway deadlines on the candidate.",
               "Complete the quality menu and fresh host/GPU capacity admission before activation."]
    pending[:0] = ["Supply missing evidence: " + item for item in contract["missing_evidence"]] + native["blockers"]
    render_ok = all(check["matches"] for check in rendered["checks"])
    output = new_directory(args.output)
    (output / "candidate-contract.json").write_text(json.dumps(contract, indent=2) + "\n")
    (output / "client-values.json").write_text(json.dumps({**rendered["client"],
        "status": "proposed-not-qualified", "endpoint_required": True}, indent=2) + "\n")
    (output / "candidate-render.json").write_text(json.dumps({**rendered, "status": "inactive-candidate-render",
        "rollback": {"active_profile": {"model": selected, **client, "overlay": "apps/overlays/assistant"},
                     "native": contract["rollback"],
                     "procedure": "Do not apply this render; re-render the active profile with service render."}},
        indent=2) + "\n")
    (output / "quality-menu.json").write_text(json.dumps(menu, indent=2) + "\n")
    (output / "candidate-sglang.yaml").write_text(kube_yaml(native["deployment"]))
    (output / "candidate-runtime.json").write_text(json.dumps({key: native[key] for key in
        ("deviations", "blockers", "readiness", "shutdown", "rollback")}, indent=2) + "\n")
    status = ("blocked-missing-evidence" if contract["missing_evidence"]
              else "blocked-render-inconsistent" if not render_ok else "blocked-integration")
    report = {"schema": 2, "status": status, "deployment_ready": False,
              "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "candidate_pointer_sha256": contract["pointer_sha256"], "checks": checks,
              "render_checks": rendered["checks"], "render_consistent": render_ok,
              "evidence_levels": {"imported_metadata": True, "package_evidence_verified": not contract["missing_evidence"],
                                  "fixture_verification": "see tests/workstation/test_shared_service.py",
                                  "runtime_observed": False},
              "assistant_source_sha256": hashlib.sha256(yaml.safe_dump_all(objects, sort_keys=True).encode()).hexdigest(),
              "planner_sha256": sha(Path(__file__)),
              "current_public_model": selected, "current_output_cap": models[selected][1],
              "proposed_output_cap": args.output_tokens, "output_cap_qualified": False,
              "capacity": rendered["capacity"],
              "quality_menu_sha256": sha(menu_path), "pending": pending,
              "files": {name: sha(output / name) for name in
                        ("candidate-contract.json", "client-values.json", "candidate-render.json", "quality-menu.json",
                         "candidate-sglang.yaml", "candidate-runtime.json")}}
    (output / "integration-plan.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"Candidate integration prepared ({status}); not deployed:", output)


# --- Serving contract for the AI-stack serving profile -----------------------
# One explicit record that every consumer (persistent SGLang Deployment, gateway
# policy, LiteLLM route, Open WebUI defaults, Qwen Code client values and the
# service-tools contract metric) is rendered from. Inputs are read, never
# edited: the serving profile and model catalogue under infrastructure/ai-stack
# belong to the serving owner, and their render.py builds the server argv and
# environment, so the persistent Deployment launches exactly what the
# qualification Job launches. The selected arm is the profile's default arm
# unless one is named; no arm letter is fixed here.
SERVING_PROFILE = ROOT / "infrastructure/ai-stack/serving/qwen3.8-27b-mxfp4-tp2.json"
CONTRACT_SCHEMA = "workstation-serving-contract/1"
CONTRACT_ID = re.compile(r"[A-Za-z0-9._:-]{1,64}")
# Reasoning parameters read from the checkpoint template itself, keyed by the
# template's git blob id as the model catalogue records it. Blob c0c686f9…
# (chat_template.jinja, 8952 bytes): thinking is on unless enable_thinking is
# false; reasoning_effort defaults to xhigh and any value other than xhigh,
# medium or low raises a template error (lines 46-49); preserve_thinking
# controls whether earlier reasoning is replayed (line 116). A template not
# listed here leaves the contract incomplete until it is read the same way.
VERIFIED_TEMPLATES = {
    "c0c686f9c38d70d179fb7b5f5aa7530bc913dda3": {
        "reads": ["enable_thinking", "reasoning_effort", "preserve_thinking"],
        "reasoning_efforts": ["low", "medium", "xhigh"], "default_effort": "xhigh",
        "rejects": "any other effort, including high and numeric efforts, raises a template error"},
}
# Template switches a client may send. preserve_thinking changes which earlier
# reasoning is replayed and is not reviewed for clients, so it stays refused.
CLIENT_TEMPLATE_KWARGS = ("enable_thinking",)
# Only "candidate" is a known not-promoted catalogue state today. The promoted
# value is an assumption until the serving owner names it; any other value is
# refused rather than guessed.
CATALOGUE_STATES = {"candidate": False, "selected": True}
# Per-request output allowance for the gateway and coding clients. 4096 truncated
# single-file tool writes (Qwen Code refused a cut-off write_file, 2026-11-11); thinking
# shares the same budget. 12288 tokens decode in ~105-135 s (113-117 tok/s at 32-64K,
# ~92 at 120K, measured), inside the route's 170 s timeout and the 180 s deadline.
DEFAULT_OUTPUT_TOKENS = 12288
# Gateway deadline per mode (the route times out 10 s earlier). Large mode serves 262K
# contexts: a cold 235K-token prompt took 238.7 s to its first token (arm UQL, 2026-11-11),
# so 300 s leaves ~60 s of answer for it. Shared mode's 81,888-token context prefills in
# under 30 s and keeps 180 s, so a stuck request frees its slot sooner beside the images.
DEADLINE_SECONDS = {"large": 300, "shared": 180}


def ai_stack_render():
    """The serving owner's render.py, loaded as a module (it never contacts a cluster)."""
    import importlib.util
    path = ROOT / "infrastructure/ai-stack/serving/render.py"
    spec = importlib.util.spec_from_file_location("ai_stack_serving_render", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _last_values(argv, flag):
    """Values after the last occurrence of a flag (argparse keeps the last one)."""
    positions = [i for i, arg in enumerate(argv) if arg == flag]
    if not positions:
        return None
    values = []
    for arg in argv[positions[-1] + 1:]:
        if arg.startswith("--"):
            break
        values.append(arg)
    return values


def _catalogue_file(catalog, name):
    row = next((f for f in catalog["model"]["files"] if f["path"] == name), None)
    require(row is not None, f"model catalogue lacks {name}")
    return {key: row[key] for key in ("size", "git_sha1", "sha256") if key in row}


def client_efforts(accepted):
    """Client effort names mapped to template-accepted values.

    OpenAI-style clients offer "high"; a template that accepts xhigh but not
    high would raise, so high is sent as the nearest accepted level.
    """
    efforts = {effort: effort for effort in accepted}
    if "high" not in accepted and "xhigh" in accepted:
        efforts["high"] = "xhigh"
    return efforts


HOST_CPU = re.compile(r"[1-9][0-9]?\Z")
HOST_MEMORY = re.compile(r"[1-9][0-9]{0,2}Gi\Z")


def qualification_record(profile, arm_name):
    """The profile's qualification record for this arm, or None.

    `qualification` is one record or a list of them, one per qualified arm (Y for the
    large mode, UQS for shared mode). Each record names the arm and image it covers.
    """
    value = profile.get("qualification")
    records = value if isinstance(value, list) else [value]
    return next((r for r in records if isinstance(r, dict) and r.get("arm") == arm_name), None)


def shared_default_arm(profile):
    """The arm shared mode renders when no --arm is given; the general default otherwise."""
    marked = [name for name, spec in profile["arms"].items() if spec.get("shared_default")]
    require(len(marked) <= 1, "at most one arm may be the shared-mode default")
    return marked[0] if marked else None


def host_resources(profile, arm_name):
    """The arm's qualification record's measured SGLang CPU/RAM requests and limits, validated, or None."""
    record = qualification_record(profile, arm_name)
    value = record.get("host_resources") if isinstance(record, dict) else None
    if value is None:
        return None
    require(isinstance(value, dict) and set(value) >= {"requests", "limits", "evidence"} and value["evidence"],
            "qualification host_resources needs requests, limits and evidence")
    out = {}
    for section in ("requests", "limits"):
        entry = value[section]
        require(isinstance(entry, dict) and set(entry) == {"cpu", "memory"} and HOST_CPU.match(str(entry["cpu"]))
                and HOST_MEMORY.match(str(entry["memory"])), f"qualification host_resources.{section} must be cpu N and memory NGi")
        out[section] = {"cpu": str(entry["cpu"]), "memory": str(entry["memory"])}
    require(int(out["requests"]["cpu"]) <= int(out["limits"]["cpu"]) and
            int(out["requests"]["memory"][:-2]) <= int(out["limits"]["memory"][:-2]), "host_resources requests exceed limits")
    return {**out, "evidence": value["evidence"]}


def serving_contract(profile_path=SERVING_PROFILE, arm=None, runtime_image=None,
                     output_tokens=DEFAULT_OUTPUT_TOKENS, deadline_seconds=None, reserve_tokens=16, mode="large"):
    """Build the serving contract from the owner's profile, catalogue and stack lock."""
    if deadline_seconds is None:
        deadline_seconds = DEADLINE_SECONDS.get(mode, 180)
    render_module = ai_stack_render()
    profile_path = Path(profile_path).resolve()
    profile, catalog = render_module.load(profile_path)
    arm_source = "explicit" if arm else "profile-default"
    if arm is None and mode == "shared" and shared_default_arm(profile):
        arm, arm_source = shared_default_arm(profile), "profile-shared-default"
    arm_name, _ = render_module.arm_of(profile, arm)
    argv = render_module.server_argv(profile, catalog, arm_name)
    environment = render_module.environment(profile, arm_name)
    # render.py reads the catalogue beside itself; the lock and model claim are read from the same tree.
    stack_root = Path(render_module.__file__).resolve().parent.parent
    catalogue_path = stack_root / "models" / f"{profile['model']}.json"
    lock_path = stack_root / "stack.lock"
    lock = dict(line.split("=", 1) for line in lock_path.read_text().splitlines()
                if line and not line.startswith("#") and "=" in line)

    def single(flag, optional=False):
        values = _last_values(argv, flag)
        if optional and values is None:
            return None
        require(values is not None and len(values) == 1, f"serving profile must set exactly one value for {flag}")
        return values[0]

    numbers = {}
    for flag, key in (("--context-length", "context_length"), ("--max-total-tokens", "max_total_tokens"),
                      ("--max-running-requests", "max_running_requests"), ("--tp-size", "tp_size")):
        value = single(flag)
        require(re.fullmatch(r"[1-9][0-9]{0,6}", value) is not None, f"invalid {flag} in the serving profile")
        numbers[key] = int(value)
    require(numbers["max_total_tokens"] >= numbers["context_length"], "serving pool is below one context")
    served = single("--served-model-name")
    require(served == profile["served_model_name"], "served model name differs from the profile")
    template_kwargs = json.loads(single("--default-chat-template-kwargs") or "{}")
    require(isinstance(template_kwargs, dict) and isinstance(template_kwargs.get("enable_thinking"), bool),
            "the profile must set an explicit boolean enable_thinking server default")
    template_file = _catalogue_file(catalog, "chat_template.jinja")
    template = VERIFIED_TEMPLATES.get(template_file.get("git_sha1"))
    missing, blocking = [], []
    if template is None:
        blocking.append(f"reasoning parameters: chat template blob {template_file.get('git_sha1')} has not been read; "
                        "record its accepted reasoning_effort values in service.VERIFIED_TEMPLATES from the template source")
        accepted = []
    else:
        accepted = template["reasoning_efforts"]
    # The pinned SGLang lets a server-default reasoning_effort override every request's own
    # (serving_chat.py refills it from the defaults after moving the request's value out), so
    # the default effort is a separate profile value the gateway applies to requests without one.
    require("reasoning_effort" not in template_kwargs,
            "reasoning_effort in --default-chat-template-kwargs overrides every per-request effort in the "
            "pinned SGLang; set the profile's request_reasoning_effort instead")
    server_effort = profile.get("request_reasoning_effort", template["default_effort"] if template else None)
    require(template is None or server_effort in accepted,
            f"the profile's request_reasoning_effort {server_effort!r} is not accepted by the checkpoint template")
    image_ok = isinstance(runtime_image, str) and any(re.fullmatch(pattern, runtime_image) for pattern in
                                                      (render_module.IMAGE_RE, render_module.PUBLIC_IMAGE_RE))
    if not image_ok:
        blocking.append(f"runtime image: the serving owner supplies the {lock.get('IMAGE_REPOSITORY', 'lab registry')}"
                        f"@sha256 digest built from stack.lock for arm {arm_name}; pass --runtime-image")
    catalogue_status = catalog.get("status")
    require(catalogue_status in CATALOGUE_STATES,
            f"unknown model catalogue status {catalogue_status!r}; the serving owner must name the promoted value")
    if not CATALOGUE_STATES[catalogue_status]:
        missing.append(f"model promotion: models/{profile['model']}.json status is {catalogue_status!r}")
    if profile.get("status") != "qualified":
        missing.append(f"profile qualification: serving/{profile['profile']}.json status is {profile.get('status')!r}; "
                       "the owner records the qualification Job result first")
    else:
        # Each record qualifies one arm on one image: any other arm or image is provisional.
        record = qualification_record(profile, arm_name) or {}
        if not record:
            value = profile.get("qualification")
            covered = [r.get("arm") for r in (value if isinstance(value, list) else [value]) if isinstance(r, dict)]
            missing.append(f"profile qualification covers arm(s) {covered!r}, not the rendered arm {arm_name!r}")
        if image_ok and record and record.get("image") != runtime_image:
            missing.append(f"profile qualification covers image {record.get('image')!r}, not --runtime-image")
    status = "incomplete" if blocking else ("provisional" if missing else "active")
    identity = f"{profile['profile']}.{arm_name}"
    require(CONTRACT_ID.fullmatch(identity) is not None, "contract identity is not a bounded label value")
    require(type(output_tokens) is int and 128 <= output_tokens <= numbers["context_length"] // 2,
            "output allowance must be 128..half the context")
    request_defaults = {"chat_template_kwargs": {"enable_thinking": template_kwargs["enable_thinking"]},
                        "reasoning_effort": server_effort} if template else None
    model, draft = catalog["model"], catalog["draft"]
    claim = next(o for o in load_objects((stack_root / "k8s/models.yaml").read_text())
                 if o["kind"] == "PersistentVolumeClaim")
    return {
        "schema": CONTRACT_SCHEMA, "contract": identity, "status": status,
        "missing_inputs": blocking + missing,
        "sources": {"profile": str(profile_path.relative_to(ROOT)) if profile_path.is_relative_to(ROOT) else profile_path.name,
                    "profile_sha256": sha(profile_path), "catalogue_sha256": sha(catalogue_path),
                    "stack_lock_sha256": sha(lock_path)},
        "selection": {"profile": profile["profile"], "profile_status": profile.get("status"), "arm": arm_name,
                      "arm_source": arm_source, "catalogue_status": catalogue_status},
        "model": {"alias": served, "served_model": served, "repository": model["repository"],
                  "revision": model["revision"], "draft_repository": draft["repository"],
                  "draft_revision": draft["revision"]},
        "tokenizer": {name: _catalogue_file(catalog, name) for name in ("tokenizer.json", "tokenizer_config.json")},
        "chat_template": {"file": "chat_template.jinja", **template_file, "verified": template is not None,
                          **({key: template[key] for key in ("reads", "reasoning_efforts", "default_effort", "rejects")}
                             if template else {})},
        "runtime": {"image": runtime_image if image_ok else None, "image_repository": lock.get("IMAGE_REPOSITORY"),
                    "sglang_commit": lock.get("SGLANG_COMMIT"), "patchset_sha256": lock.get("SGLANG_PATCHSET_SHA256"),
                    "gpu_target": lock.get("GPU_TARGET"),
                    # Arms can differ only in environment, so both are hashed.
                    "launch_sha256": hashlib.sha256(json.dumps([argv, environment]).encode()).hexdigest(),
                    "environment": sorted(entry["name"] for entry in environment)},
        "parsers": {"reasoning_parser": single("--reasoning-parser", True),
                    "tool_call_parser": single("--tool-call-parser", True),
                    "grammar_backend": single("--grammar-backend", True)},
        "reasoning": {"server_default_template_kwargs": template_kwargs, "request_defaults": request_defaults,
                      "client_efforts": client_efforts(accepted), "client_template_kwargs": list(CLIENT_TEMPLATE_KWARGS),
                      "per_request_effort": "honoured: the server defaults carry no reasoning_effort (the pinned "
                                            "serving_chat.py let a server-default effort override every request's, "
                                            "measured 2026-11-11); the gateway applies the default effort only to "
                                            "requests that name none"},
        "budgets": {**numbers, "output_tokens": output_tokens, "reserve_tokens": reserve_tokens,
                    "input_tokens": numbers["context_length"] - output_tokens - reserve_tokens},
        "concurrency": {"gateway_admission": GATEWAY_ADMISSION, "native_running": numbers["max_running_requests"]},
        "deadlines": {"gateway_seconds": deadline_seconds, "route_seconds": deadline_seconds - 10,
                      "client_milliseconds": deadline_seconds * 1000},
        "assets": {"claim": claim["metadata"]["name"], "storage_class": claim["spec"]["storageClassName"],
                   "access_modes": claim["spec"]["accessModes"],
                   "model_path": single("--model-path"), "draft_path": single("--speculative-draft-model-path"),
                   "verified_marker": "VERIFIED.json",
                   "storage_note": "NAS-backed nas-nfs claim; the NAS storage guide recommends workstation-local "
                                   "caching for active models. Recorded for the serving owner, not changed here."},
        "requirements": {"gpus": numbers["tp_size"], "gpu_groups": list(render_module.GPU_GROUPS),
                         "node_selector": {"ai-home-lab.local/gfx": lock.get("GPU_TARGET")},
                         "pod_security": "restricted",
                         # Measured host CPU/RAM from the qualification record; None keeps the base Deployment's.
                         "host_resources": host_resources(profile, arm_name)},
        "_render": {"argv": argv, "environment": environment, "server_label": render_module.SERVER_LABEL},
    }


def contract_view(contract):
    """The fields candidate_render projects into each consumer layer."""
    budgets = contract["budgets"]
    return {"client_alias": contract["model"]["alias"], "served_model": contract["model"]["served_model"],
            "context_length": budgets["context_length"], "max_total_tokens": budgets["max_total_tokens"],
            "max_running_requests": budgets["max_running_requests"],
            "request_defaults": contract["reasoning"]["request_defaults"]}


def contract_deployment(contract, objects):
    """Persistent SGLang Deployment for the contract; replicas stay zero.

    Keeps the existing Deployment's probes, security context, cache volume,
    strategy and grace period. Replaces the image, launch argv and environment
    with the profile's own, mounts the catalogue's model claim read-only and
    sets the GPU count from the profile.
    """
    base = next(o for o in objects if o["kind"] == "Deployment" and o["metadata"]["name"] == "sglang")
    deployment = copy.deepcopy(base)
    rendered = contract["_render"]
    pod = deployment["spec"]["template"]
    spec = pod["spec"]
    container = spec["containers"][0]
    container["image"] = contract["runtime"]["image"] or "registry.invalid/ai-home-lab/sglang-gfx1201:CONTRACT-INCOMPLETE"
    container["imagePullPolicy"] = "IfNotPresent"
    container["command"] = ["python3", "-m", "sglang.launch_server"]
    container["args"] = list(rendered["argv"])
    container.pop("envFrom", None)
    container["env"] = [dict(entry) for entry in rendered["environment"]]
    set_gpu_claims(spec, container, int(contract["requirements"]["gpus"]))
    measured = contract["requirements"].get("host_resources")
    for section in ("requests", "limits"):
        if measured:
            container["resources"][section].update(measured[section])
    container["securityContext"] = {**container.get("securityContext", {}), "readOnlyRootFilesystem": True}
    spec["securityContext"]["supplementalGroups"] = contract["requirements"]["gpu_groups"]
    spec["nodeSelector"] = {**spec.get("nodeSelector", {}), **contract["requirements"]["node_selector"]}
    for volume in spec["volumes"]:
        if volume["name"] == "models":
            volume["persistentVolumeClaim"] = {"claimName": contract["assets"]["claim"], "readOnly": True}
    # The qualification Job's Service selects this label; carrying it keeps the
    # sglang Service and telemetry pointed at whichever server holds the GPUs.
    pod["metadata"]["labels"].update({rendered["server_label"]: "true",
                                      "ai-stack/profile": contract["selection"]["profile"],
                                      "ai-stack/arm": contract["selection"]["arm"]})
    deployment["spec"]["replicas"] = 0
    deployment["metadata"].setdefault("annotations", {}).update({
        "ai-home-lab.local/qualification": f"serving-contract-{contract['status']}-not-qualified",
        "ai-home-lab.local/serving-contract": contract["contract"],
        "ai-home-lab.local/serving-launch-sha256": contract["runtime"]["launch_sha256"]})
    return deployment


def gateway_policy(contract, rendered, mode="large"):
    """Gateway allowlist read by boundary.apply_policy; the 9B split route is kept.

    Images stay with the one-GPU 9B split profile, plus the contract model in shared
    mode (both GPUs for chat, with the image generators in the headroom it leaves)."""
    image_models = ["Qwen3.5-9B"] + (list(rendered["gateway"]["chat_models"]) if mode == "shared" else [])
    return {"contract": contract["contract"], "status": contract["status"],
            "chat_models": {**rendered["gateway"]["chat_models"], "Qwen3.5-9B": [4096, 1024]},
            "image_models": image_models,
            "reasoning_efforts": contract["reasoning"]["client_efforts"],
            "default_reasoning_effort": (contract["reasoning"]["request_defaults"] or {}).get("reasoning_effort"),
            "template_kwargs": contract["reasoning"]["client_template_kwargs"],
            "request_timeout": contract["deadlines"]["gateway_seconds"],
            "admission_limit": contract["concurrency"]["gateway_admission"]}


def apply_contract(objects, contract, mode, experience):
    """Rewrite every consumer in a rendered composition from one contract."""
    require(mode in {"large", "shared"}, "the serving contract needs both GPUs; render it with --mode large or shared")
    require(experience.startswith("assistant"), "the serving contract applies to the assistant experiences")
    if contract["reasoning"]["request_defaults"] is None:
        # Without verified template parameters, clients carry no reasoning defaults.
        view = dict(contract_view(contract), request_defaults=None)
    else:
        view = contract_view(contract)
    budgets, deadlines = contract["budgets"], contract["deadlines"]
    rendered = candidate_render(view, budgets["output_tokens"], deadlines["gateway_seconds"], budgets["reserve_tokens"])
    alias = view["client_alias"]
    annotation = {"ai-home-lab.local/serving-contract": contract["contract"]}
    result = []
    for obj in objects:
        kind, name = obj["kind"], obj["metadata"]["name"]
        data = obj.get("data", {})
        if kind == "ConfigMap" and "MODEL_REVISION" in data:
            continue  # the legacy sglang-profile; the contract Deployment does not read it
        if kind == "Deployment" and name == "sglang":
            obj = contract_deployment(contract, objects)
        elif kind == "ConfigMap" and "litellm.yaml" in data:
            config = yaml.safe_load(data["litellm.yaml"])
            config["model_list"] = [rendered["route"]] + [route for route in config["model_list"]
                                                          if route["model_name"] not in {LEGACY_ALIAS, alias}]
            # The global fallbacks must not undercut the contract route's own timeout.
            route_timeout = rendered["route"]["litellm_params"]["timeout"]
            config.setdefault("router_settings", {})["timeout"] = route_timeout
            config.setdefault("litellm_settings", {})["request_timeout"] = route_timeout
            data["litellm.yaml"] = yaml.safe_dump(config, sort_keys=False)
            data["gateway-policy.json"] = json.dumps(gateway_policy(contract, rendered, mode), indent=2) + "\n"
        elif kind == "ConfigMap" and data.get("MEMORY_LLM_MODEL") == LEGACY_ALIAS:
            data["MEMORY_LLM_MODEL"] = alias
        elif kind == "Deployment" and name in {"open-webui", "service-tools", "litellm"}:
            template = obj["spec"]["template"]
            template["metadata"].setdefault("annotations", {}).update(annotation)
            container = template["spec"]["containers"][0]
            env = container.setdefault("env", [])
            if name == "open-webui":
                values = rendered["webui"]
            elif name == "service-tools":
                values = {"SERVING_CONTRACT": contract["contract"], "SERVING_CONTRACT_STATUS": contract["status"]}
            else:
                values = {}
            env[:] = [entry for entry in env if entry["name"] not in values]
            env += [{"name": key, "value": value} for key, value in values.items()]
        result.append(obj)
    # The rewritten gateway config keeps the name kustomize hashed from the source file,
    # so a contract change (output cap, policy) would not roll its consumers: LiteLLM
    # reads it only at startup (seen 2026-11-11). Stamp the final contents' hash on them.
    for config in [o for o in result if o["kind"] == "ConfigMap" and "litellm.yaml" in o.get("data", {})]:
        digest = hashlib.sha256(json.dumps(config["data"], sort_keys=True).encode()).hexdigest()
        for obj in result:
            if obj["kind"] == "Deployment" and any(volume.get("configMap", {}).get("name") == config["metadata"]["name"]
                                                   for volume in obj["spec"]["template"]["spec"].get("volumes", [])):
                obj["spec"]["template"]["metadata"].setdefault("annotations", {})[
                    "ai-home-lab.local/gateway-config-sha256"] = digest
    # Configuration only: the gateway's Python source keeps its legacy defaults,
    # which the rendered gateway-policy.json replaces at startup.
    configuration = [dict(o, data={k: v for k, v in o["data"].items() if not k.endswith(".py")})
                     if o["kind"] == "ConfigMap" else o for o in result]
    checks = rendered["checks"] + [
        {"id": "no-legacy-route", "matches": LEGACY_ALIAS not in yaml.safe_dump_all(configuration)},
        {"id": "deployment-argv", "matches": next(o for o in result if o["kind"] == "Deployment"
                                                  and o["metadata"]["name"] == "sglang")["spec"]["template"]["spec"]
                                             ["containers"][0]["args"] == contract["_render"]["argv"]},
    ]
    if contract["reasoning"]["request_defaults"] is None:
        # Not a consistency failure: the missing template verification is listed.
        checks = [check for check in checks if check["id"] != "request-defaults"]
    require(all(check["matches"] for check in checks),
            "contract render is inconsistent: " + ", ".join(c["id"] for c in checks if not c["matches"]))
    return result, rendered, checks


def public_contract(contract):
    return {key: value for key, value in contract.items() if not key.startswith("_")}


def protected(path):
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), "credential must be a regular file")
    meta = path.stat()
    require(meta.st_uid == os.geteuid() and stat.S_IMODE(meta.st_mode) == 0o600,
            "credential must be caller-owned mode 0600")
    return path.read_text().strip()


def site_values(path):
    site = json.loads(Path(path).read_text())
    # "environment" is no longer used; older private site files may still carry it.
    # "webui_default_group" is optional: the Open WebUI group new accounts join.
    require(set(site) - {"environment", "webui_default_group"} == {"context", "node", "node_address", "management_cidr",
                                                                   "client_cidrs", "webui_host", "api_host", "tls_secret"},
            "unexpected/missing site keys")
    group = site.setdefault("webui_default_group", "")
    require(isinstance(group, str) and (group == "" or re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", group) is not None),
        "webui_default_group must be an Open WebUI group UUID or empty")
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
    for key in ("node", "context"):
        require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,200}", site[key]) is not None, "invalid target identifier")
    # A Kubernetes Secret name; the staging certificate uses "<tls_secret>-staging".
    require(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,200}[a-z0-9])?", site["tls_secret"]) is not None,
            "TLS Secret must be a lowercase DNS label")
    return site


def traefik_config(site):
    """Bind the reviewed Traefik values to the site's node address, TCP 443 only."""
    config = yaml.safe_load((ROOT / "infrastructure/service/traefik.yaml").read_text())
    values = yaml.safe_load(config["spec"]["valuesContent"])
    spec = values["service"]["spec"]
    require(spec.get("type") == "ClusterIP" and "externalIPs" not in spec
            and spec.get("externalTrafficPolicy") == "Local"
            and values["ports"]["websecure"].get("exposedPort") == 443
            and values["ports"]["web"]["expose"] == {"default": False}
            and not any("nodePort" in (port or {}) or "hostPort" in (port or {}) or "hostIP" in (port or {})
                        for port in values["ports"].values()),
            "Traefik source must expose only HTTPS 443 through the site address")
    spec["externalIPs"] = [site["node_address"]]
    config["spec"]["valuesContent"] = kube_yaml(values)

    # KubeDumper keeps the embedded chart values reviewable as a block, as in the source.
    return kube_yaml(config)


def certificates(site):
    """ACME staging into a separate Secret first; production into the serving TLS Secret."""
    def certificate(name, secret, issuer):
        return {"apiVersion": "cert-manager.io/v1", "kind": "Certificate",
                "metadata": {"name": name, "namespace": "ai-home-lab"},
                "spec": {"secretName": secret, "dnsNames": [site["webui_host"], site["api_host"]],
                         "privateKey": {"algorithm": "ECDSA", "size": 256, "rotationPolicy": "Always"},
                         "issuerRef": {"group": "cert-manager.io", "kind": "ClusterIssuer", "name": issuer}}}
    return {"certificate-staging.yaml": certificate("ai-home-lab-tls-staging", site["tls_secret"] + "-staging",
                                                    "letsencrypt-staging-cloudflare"),
            "certificate.yaml": certificate("ai-home-lab-tls", site["tls_secret"], "letsencrypt-production-cloudflare")}


def render(args):
    site = site_values(args.site)
    experience = getattr(args, "experience", "service")
    require(experience in {"service", "assistant", "assistant-memory"}, "unknown service experience")
    # shared: the two-GPU contract model plus both image generators; it exists only as a
    # serving contract and only for the assistant experiences.
    require(args.mode != "shared" or (getattr(args, "serving_profile", None) and experience.startswith("assistant")),
            "--mode shared needs --serving-profile and an assistant experience")
    overlay = {"large": experience, "split": experience + "-split", "shared": experience + "-shared"}[args.mode]
    objects = kustomize(ROOT / "apps/overlays" / overlay)
    contract = None
    if getattr(args, "serving_profile", None):
        # Every consumer comes from one contract; an incomplete contract still
        # renders, inactive, with its missing inputs listed in evidence.json.
        contract = serving_contract(args.serving_profile, getattr(args, "arm", None),
                                    getattr(args, "runtime_image", None),
                                    getattr(args, "output_tokens", DEFAULT_OUTPUT_TOKENS), mode=args.mode)
        objects, contract_rendered, contract_checks = apply_contract(objects, contract, args.mode, experience)
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
                {"name": "PUBLIC_WEBUI_BASE_URL", "value": f'https://{site["webui_host"]}'})
        if kind == "Deployment" and name == "open-webui" and site["webui_default_group"]:
            # New accounts join this group; its model read grants make the served
            # models visible. Persistent config is off, so the environment is the source.
            obj["spec"]["template"]["spec"]["containers"][0].setdefault("env", []).append(
                {"name": "DEFAULT_GROUP_ID", "value": site["webui_default_group"]})
        if kind == "Ingress":
            host = site["webui_host"] if name == "open-webui" else site["api_host"]
            obj["spec"]["rules"][0]["host"] = host
            obj["spec"]["tls"] = [{"hosts": [host], "secretName": site["tls_secret"]}]
            annotations = obj["metadata"].setdefault("annotations", {})
            current = annotations.get("traefik.ingress.kubernetes.io/router.middlewares", "")
            annotations["traefik.ingress.kubernetes.io/router.middlewares"] = ",".join(filter(None, [current, "ai-home-lab-private-clients@kubernetescrd"]))
        if kind == "PersistentVolumeClaim":
            obj["spec"]["storageClassName"] = existing.get(name, {}).get("storage_class", "workstation-retain")
            if name in {"webui-rag-qwen3-data", "webui-data", "creative-models", "gateway-postgres", "service-tools-state",
                        "graphiti-neo4j"}:
                obj["metadata"].setdefault("labels", {})["workstation.ai/backup"] = "irreplaceable"
    objects.append({"apiVersion": "traefik.io/v1alpha1", "kind": "Middleware",
                    "metadata": {"name": "private-clients", "namespace": "ai-home-lab"},
                    "spec": {"ipAllowList": {"sourceRange": site["client_cidrs"]}}})
    output = new_directory(args.output)
    (output / "workloads.yaml").write_text(kube_yaml_all(objects))
    (output / "traefik.yaml").write_text(traefik_config(site))
    for name, certificate in certificates(site).items():
        (output / name).write_text(kube_yaml(certificate))
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
    (output / "migration.yaml").write_text(kube_yaml(job))
    (output / "site.json").write_text(json.dumps(site, indent=2) + "\n")
    values = client_profile(args.mode)
    if contract:
        values = {**contract_rendered["client"], "AGENT_SERVING_CONTRACT": contract["contract"]}
    (output / "client.conf").write_text(f'AGENT_BASE_URL=https://{site["api_host"]}/v1\n' +
        "".join(f"{key}={str(value).lower() if type(value) is bool else value}\n" for key, value in values.items()))
    if experience.startswith("assistant"):
        tools = next(o for o in objects if o["kind"] == "Deployment" and o["metadata"]["name"] == "service-tools")
        tools_env = {e["name"]: e.get("value") for e in tools["spec"]["template"]["spec"]["containers"][0].get("env", [])}
        with (output / "client.conf").open("a") as client:
            client.write('AGENT_RETRIEVAL_ENABLED=true\nAGENT_WEB_SEARCH_ENABLED=true\n')
            client.write(f'AGENT_MEMORY_ENABLED={"true" if experience == "assistant-memory" else "false"}\n')
            # Handoff tools follow the rendered service: offered only when it serves them.
            client.write(f'AGENT_HANDOFF_ENABLED={"true" if tools_env.get("HANDOFF_ENABLED") == "true" else "false"}\n')
    files = ["workloads.yaml", "traefik.yaml", "certificate-staging.yaml", "certificate.yaml", "migration.yaml",
             "site.json", "client.conf"]
    if contract:
        (output / "serving-contract.json").write_text(json.dumps(public_contract(contract), indent=2) + "\n")
        files.append("serving-contract.json")
        # Reasoning choices for the Open WebUI model menu (applied with webui_presets.py apply).
        import webui_presets
        (output / "webui-presets.json").write_text(json.dumps(webui_presets.presets(contract), indent=2) + "\n")
        files.append("webui-presets.json")
    (output / "evidence.json").write_text(json.dumps({"status": "source-rendered-not-deployed", "mode": args.mode,
        "experience": experience,
        "existing_claims": existing,
        "serving_contract": ({"contract": contract["contract"], "status": contract["status"],
                              "missing_inputs": contract["missing_inputs"], "checks": contract_checks}
                             if contract else {"contract": None, "note": "legacy source composition; no serving contract"}),
        "files": {n: sha(output / n) for n in files},
        "required": ["TLS certificate issued", "private DNS", "host firewall 443/6443 review", "retained-PV migration",
                     "backup restore acceptance", "database migration", "workload qualification"]
                    + (["serving contract: " + item for item in contract["missing_inputs"]] if contract else [])},
        indent=2) + "\n")
    print("Rendered service source; replicas remain zero:", output)


def capacity(resources, workloads, telemetry, telemetry_stack, mode, build_mib=0, build_cpus=0, vm_mib=0, vm_cpus=0):
    require(resources.get("schema_version") == 1 and resources.get("status") == "offline-plan-not-applied", "use resources plan output")
    require(all(type(n) is int and n >= 0 for n in (build_mib, build_cpus, vm_mib, vm_cpus)), "budgets must be nonnegative integers")
    names = {"sglang", "open-webui", "litellm", "gateway-postgres", "service-tools"}
    if mode == "split":
        names.add("swarmui")
    elif mode == "shared":
        names |= {"swarmui", "comfyui-gpu0"}
    selected = {o["metadata"]["name"]: o for o in workloads if o["kind"] == "Deployment"}
    # Opt-in memory workloads count against the same budget when rendered.
    names |= {"neo4j", "memory-embedding", "graphiti-memory"} & selected.keys()
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
    # Shared claims: SGLang (gpu-0, gpu-1) and SwarmUI (gpu-1) need two cards, not three.
    gpu = len(set().union(*(gpu_claims(selected[name]) for name in names)))
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
              # Optional Graphiti memory: shared by service-tools and graphiti-memory only.
              "memory-service-key": secrets.token_urlsafe(48),
              "memory-neo4j-auth": "neo4j/" + secrets.token_urlsafe(32),
              "mapping.json": json.dumps({"service_key": service_key, "users": {}}),
              "image-users.json": json.dumps({"service_key": service_key, "users": {}})}
    for name, value in values.items():
        path = output / name
        # service-key reaches Open WebUI as an environment variable (secretKeyRef),
        # which keeps every byte; a trailing newline makes its Authorization header
        # invalid. File readers strip, so only this value must be written bare.
        path.write_text(value if name == "service-key" else value + "\n")
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
    config["data"]["prometheus.yml"] = kube_yaml(prometheus)
    rules = yaml.safe_load(config["data"]["alerts.yml"])
    rules["groups"] += yaml.safe_load((ROOT / "infrastructure/service/alerts.yaml").read_text())["groups"]
    config["data"]["alerts.yml"] = kube_yaml(rules)
    objects += load_objects((ROOT / "infrastructure/service/alertmanager.yaml").read_text())
    for obj in objects:
        if obj["kind"] == "NetworkPolicy" and obj["metadata"]["name"] == "service-alertmanager":
            obj["spec"]["egress"] = [{"to": [{"ipBlock": {"cidr": str(network)}}],
                                      "ports": [{"protocol": "TCP", "port": args.receiver_port}]}]
    output = new_directory(args.output)
    (output / "stack.yaml").write_text(kube_yaml_all(objects))
    (output / "evidence.json").write_text(json.dumps({"status": "source-rendered-not-deployed",
        "base_evidence_sha256": sha(Path(args.telemetry) / "evidence.json"), "stack_sha256": sha(output / "stack.yaml")}, indent=2))
    print("Notification composition prepared; receiver Secret and delivery acceptance still required.")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    p = commands.add_parser("render")
    p.add_argument("site"); p.add_argument("output"); p.add_argument("--mode", choices=["large", "split", "shared"], default="large")
    p.add_argument("--experience", choices=["service", "assistant", "assistant-memory"], default="service")
    p.add_argument("--existing-claims", help="operator-collected PVC JSON; preserve immutable storage classes")
    p.add_argument("--serving-profile", help="AI-stack serving profile; renders every consumer from its contract")
    p.add_argument("--arm", help="profile arm; default: the arm the profile marks default "
                   "(shared mode: the arm it marks shared_default, if any)")
    p.add_argument("--runtime-image", help="serving image pinned by digest, supplied by the serving owner")
    p.add_argument("--output-tokens", type=int, default=DEFAULT_OUTPUT_TOKENS,
                   help="client output allowance, at most half the contract context; unqualified")
    p = commands.add_parser("candidate-plan", help="compare a pinned selected native candidate with assistant source; offline only")
    p.add_argument("pointer"); p.add_argument("output")
    p.add_argument("--pointer-sha256", required=True)
    p.add_argument("--output-tokens", type=int, default=4096,
                   help="proposed output allowance, separate from context; remains unqualified")
    p.add_argument("--package-root", help="selected package-v2 directory; authenticates model, template, stack and request identities")
    p.add_argument("--deadline-seconds", type=int, default=180, help="proposed gateway deadline; unmeasured until the menu runs")
    p.add_argument("--reserve-tokens", type=int, default=16, help="scheduler reserve excluded from the router input budget")
    p.add_argument("--runtime-image", help="locally built candidate runtime image, localhost/...@sha256:<digest>")
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
