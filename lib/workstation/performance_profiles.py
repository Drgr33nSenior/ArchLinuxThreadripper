"""Compare sealed workstation experiment evidence and select a profile offline.

This module only reads owner-prepared evidence bundles.  It does not start a
Pod, run a benchmark, mutate a Deployment, or make a selected profile active.
The JSON report is deliberately a source artifact: Bridge may validate the
same sealed bundle and invoke this module, but must not reimplement its rules.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import statistics

import coding_eval
import model_kernels


MAX_FILE = 16 * 1024 * 1024
IDENTITY = ("model_revision", "tokenizer_sha256", "workload_sha256", "hardware_sha256",
            "software_sha256", "launch_sha256", "quantization")
VARIABLES = frozenset(("compiler_backend", "image", "concurrency", "loading_strategy",
                       "loading_threads", "engine_queue", "interactive_priority",
                       "model", "tokenizer", "workload", "hardware", "software", "launch",
                       "resources", "quantization", "coding_corpus", "generation", "profile", "prefix_state"))
NON_EQUIVALENT_VARIABLES = frozenset(("model", "tokenizer", "workload", "hardware", "quantization",
                                      "resources", "concurrency", "coding_corpus", "generation", "profile", "prefix_state"))
PRODUCER_CONDITIONS_SCHEMA = 2
PRODUCER_CONDITION_KEYS = frozenset(("schema", "runtime_sha256", "model_files_sha256", "model_settings_sha256",
                                     "observed_launch_sha256", "launch_settings_sha256", "resources_sha256"))
OBSERVED_LAUNCH_OPTIONS = frozenset(("--model-path", "--revision", "--served-model-name", "--dtype", "--tp", "--tp-size",
                                    "--context-length", "--mem-fraction-static", "--max-running-requests", "--max-queued-requests",
                                    "--model-loader-extra-config", "--schedule-policy", "--chunked-prefill-size", "--attention-backend",
                                    "--stream-interval", "--torch-compile-max-bs", "--cuda-graph-backend-decode",
                                    "--cuda-graph-backend-prefill", "--cuda-graph-tc-compiler", "--enable-torch-compile",
                                    "--cuda-graph-bs-decode", "--cuda-graph-bs-prefill"))
OBSERVED_LAUNCH_LIST_OPTIONS = frozenset(("--cuda-graph-bs-decode", "--cuda-graph-bs-prefill"))


def digest_bytes(data):
    return hashlib.sha256(data).hexdigest()


def canonical_digest(value):
    """Match producer identity hashes that use canonical JSON, not file bytes."""
    return digest_bytes(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())


def read_json(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE:
        raise ValueError("evidence must be a regular bounded JSON file")
    data = path.read_bytes()
    try:
        return json.loads(data), digest_bytes(data)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("evidence is not valid JSON") from error


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def safe_id(value, name="identifier"):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", value):
        raise ValueError(f"invalid {name}")
    return value


def relative_file(bundle, name):
    if not isinstance(name, str) or name.startswith("/"):
        raise ValueError("bundle artifact must be a relative path")
    supplied = bundle / name
    if supplied.is_symlink():
        raise ValueError("bundle artifact must not be a symlink")
    result = supplied.resolve()
    try:
        result.relative_to(bundle.resolve())
    except ValueError as error:
        raise ValueError("bundle artifact escapes the evidence directory") from error
    return result


def median_summary(value):
    """Normalise a serving summary without inventing a missing tail value."""
    if value is None:
        return {"status": "unknown"}
    if not isinstance(value, dict) or type(value.get("n")) is not int or value["n"] < 1:
        raise ValueError("invalid measurement summary")
    result = {"status": "observed", "samples": value["n"]}
    for key in ("min", "median", "max", "stdev"):
        if not finite(value.get(key)) or value[key] < 0:
            raise ValueError("invalid measurement value")
        result[key] = value[key]
    # A p95 based on fewer than 20 samples is an order statistic, not a useful
    # tail estimate for a profile decision.
    if value["n"] >= 20:
        if not finite(value.get("p95")) or value["p95"] < 0:
            raise ValueError("invalid tail measurement")
        result["p95"] = value["p95"]
    else:
        result["p95"] = None
        result["tail_status"] = "unknown-insufficient-samples"
    return result


def serving_metrics(result):
    if not isinstance(result, dict) or result.get("schema") != 1:
        return {"status": "incomplete", "reason": "serving result is malformed"}
    source_status = result.get("status")
    if source_status == "measured-awaiting-provenance":
        raise ValueError("serving result has not completed provenance verification")
    if source_status != "measured-not-qualified":
        return {"status": "incomplete", "source_status": source_status,
                "reason": "serving result is not a completed measurement"}
    rows = result.get("repeat_summary")
    if not isinstance(rows, list) or not rows:
        return {"status": "incomplete", "source_status": source_status, "reason": "repeat summary unavailable"}
    cases = []
    seen = set()
    for row in rows:
        if (not isinstance(row, dict) or type(row.get("failures")) is not int or row["failures"] < 0
                or type(row.get("context_tokens")) is not int or row["context_tokens"] < 1
                or type(row.get("concurrency")) is not int or row["concurrency"] < 1):
            return {"status": "incomplete", "source_status": source_status,
                    "reason": "serving repeat summary is malformed"}
        key = (row["context_tokens"], row["concurrency"])
        if key in seen:
            return {"status": "incomplete", "source_status": source_status,
                    "reason": "serving repeat summary has duplicate workload cases"}
        seen.add(key)
        case = {"context_tokens": key[0], "concurrency": key[1], "failures": row["failures"]}
        for source, target in (("throughput_across_repetitions", "throughput"),
                               ("request_latency_seconds", "latency"), ("ttft_seconds", "ttft")):
            try:
                case[target] = median_summary(row.get(source))
            except ValueError:
                return {"status": "incomplete", "source_status": source_status,
                        "reason": "serving repeat summary has invalid measurements"}
        cases.append(case)
    # Multiple workload cases remain distinct.  A single aggregate could hide
    # a regression at the long context or interactive concurrency.
    return {"status": "observed", "source_status": source_status, "cases": cases}


def tokenizer_digest(runtime):
    """Derive tokenizer identity from retained verified model files only."""
    files = runtime.get("model_files") if isinstance(runtime, dict) else None
    if not isinstance(files, dict):
        raise ValueError("runtime model files are unavailable")
    names = ("tokenizer.json", "tokenizer_config.json", "special_tokens_map.json", "added_tokens.json",
             "vocab.json", "merges.txt")
    selected = {name: files[name] for name in sorted(files) if Path(name).name in names}
    if not selected:
        raise ValueError("runtime tokenizer files are unavailable")
    for name, record in selected.items():
        if (not isinstance(record, dict) or type(record.get("bytes")) is not int or record["bytes"] < 0
                or not isinstance(record.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", record["sha256"])):
            raise ValueError(f"invalid tokenizer file identity: {name}")
    return canonical_digest(selected)


def model_files_digest(runtime):
    """Validate and identify every retained model file, not only tokenizer files."""
    files = runtime.get("model_files") if isinstance(runtime, dict) else None
    if not isinstance(files, dict) or not files:
        raise ValueError("runtime model files are unavailable")
    for name, record in files.items():
        if not isinstance(name, str):
            raise ValueError("invalid model file identity")
        path = Path(name)
        if (path.is_absolute() or ".." in path.parts
                or not isinstance(record, dict) or type(record.get("bytes")) is not int or record["bytes"] < 0
                or not isinstance(record.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", record["sha256"])):
            raise ValueError("invalid model file identity")
    return canonical_digest(files)


def normalized_observed_launch(runtime):
    """Validate the allowlisted observed engine command without Pod template data."""
    launch = runtime.get("launch") if isinstance(runtime, dict) else None
    if not isinstance(launch, list) or len(launch) != 1 or not isinstance(launch[0], dict):
        raise ValueError("observed runtime launch is unavailable")
    normalized = {}
    for option, value in launch[0].items():
        if option not in OBSERVED_LAUNCH_OPTIONS:
            raise ValueError("observed runtime launch contains an unsupported option")
        if option == "--enable-torch-compile":
            if type(value) is not bool:
                raise ValueError("observed runtime launch compiler flag is malformed")
        elif option in OBSERVED_LAUNCH_LIST_OPTIONS:
            if (not isinstance(value, list) or not value
                    or any(not isinstance(item, str) or not item for item in value)):
                raise ValueError("observed runtime launch batch option is malformed")
        elif not isinstance(value, str) or not value:
            raise ValueError("observed runtime launch option is malformed")
        normalized[option] = value
    if "--enable-torch-compile" not in normalized:
        raise ValueError("observed runtime launch compiler state is unavailable")
    return normalized


def observed_launch_digest(runtime):
    """Hash the observed engine command separately from the Pod template hash."""
    return canonical_digest(normalized_observed_launch(runtime))


def producer_identity(serving, retained_runtime, runtime_digest):
    """Derive every manifest identity from the finalized serving producer."""
    if not isinstance(serving, dict):
        raise ValueError("serving producer is malformed")
    runtime, pod = serving.get("runtime"), serving.get("pod")
    if not isinstance(runtime, dict) or not isinstance(pod, dict) or not isinstance(runtime.get("settings"), dict):
        raise ValueError("serving runtime or Pod identity is unavailable")
    model_revision = runtime["settings"].get("MODEL_REVISION")
    quantization = runtime.get("model_contract", {}).get("quant_method") if isinstance(runtime.get("model_contract"), dict) else None
    workload = serving.get("workload_sha256")
    runtime_hash = serving.get("runtime_sha256")
    launch_hash = pod.get("launch_spec_sha256")
    devices = runtime.get("devices")
    resources = pod.get("resources")
    node, image, image_id = pod.get("node"), pod.get("image"), pod.get("image_id")
    packages, hip = runtime.get("packages"), runtime.get("hip")
    if (not isinstance(model_revision, str) or not model_revision
            or not isinstance(quantization, str) or not quantization
            or not isinstance(workload, str) or not re.fullmatch(r"[a-f0-9]{64}", workload)
            or not isinstance(runtime_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", runtime_hash)
            or not isinstance(launch_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", launch_hash)
            or not isinstance(node, str) or not node
            or not isinstance(image, str) or not re.search(r"@sha256:[a-f0-9]{64}$", image)
            or not isinstance(image_id, str) or not re.search(r"sha256:[a-f0-9]{64}$", image_id)
            or not isinstance(resources, dict) or not isinstance(resources.get("requests"), dict)
            or not isinstance(resources.get("limits"), dict) or not resources["requests"]
            or resources["requests"] != resources["limits"]
            or not isinstance(packages, dict) or not packages
            or not {"sglang", "torch", "triton", "pytorch-triton-rocm", "aiter", "transformers"} <= set(packages)
            or not isinstance(packages.get("torch"), str) or not packages["torch"]
            or not isinstance(hip, str) or not hip
            or not isinstance(devices, list) or not devices):
        raise ValueError("serving producer identity is incomplete")
    identities = []
    for device in devices:
        if (not isinstance(device, dict) or not isinstance(device.get("uuid"), str) or not device["uuid"]
                or device["uuid"].lower() in ("unknown", "none")
                or not isinstance(device.get("gfx"), str) or device["gfx"].split(":", 1)[0] != "gfx1201"):
            raise ValueError("serving producer GPU identity is incomplete")
        identities.append(device["uuid"])
    if len(identities) != len(set(identities)):
        raise ValueError("serving producer GPU identities are not distinct")
    if runtime != retained_runtime or runtime_hash != runtime_digest:
        raise ValueError("serving runtime hash does not match retained runtime evidence")
    return {"model_revision": model_revision, "tokenizer_sha256": tokenizer_digest(runtime),
            "workload_sha256": workload, "hardware_sha256": canonical_digest({"node": pod.get("node"), "devices": devices}),
            "software_sha256": canonical_digest({"image": pod.get("image"), "image_id": pod.get("image_id"),
                                                   "packages": runtime.get("packages"), "hip": runtime.get("hip")}),
            "launch_sha256": launch_hash, "quantization": quantization}


def producer_conditions(serving, runtime_digest):
    """Keep source-level conditions visible without changing the stable manifest identity."""
    runtime, pod = serving["runtime"], serving["pod"]
    settings = runtime.get("settings")
    if not isinstance(settings, dict) or not settings:
        raise ValueError("runtime settings are unavailable")
    model_setting_names = {"MODEL_PATH", "MODEL_REVISION", "MODEL_REPOSITORY", "SERVED_MODEL_NAME", "MODEL_DTYPE"}
    if "MODEL_DTYPE" not in settings:
        raise ValueError("runtime model dtype is unavailable")
    model_settings = {key: value for key, value in settings.items() if key in model_setting_names}
    launch_settings = {key: value for key, value in settings.items() if key not in model_setting_names}
    return {"schema": PRODUCER_CONDITIONS_SCHEMA,
            "runtime_sha256": runtime_digest, "model_files_sha256": model_files_digest(runtime),
            "model_settings_sha256": canonical_digest(model_settings),
            "observed_launch_sha256": observed_launch_digest(runtime),
            "launch_settings_sha256": canonical_digest(launch_settings),
            "resources_sha256": canonical_digest(pod["resources"])}


def producer_condition_details(serving):
    """Private, bounded values that explain a changed observed launch digest."""
    return {"observed_launch": normalized_observed_launch(serving["runtime"])}


def valid_producer_conditions(value):
    """Accept only the normalized source conditions persisted with a selection."""
    return (isinstance(value, dict) and set(value) == PRODUCER_CONDITION_KEYS
            and value.get("schema") == PRODUCER_CONDITIONS_SCHEMA
            and all(isinstance(value[key], str) and re.fullmatch(r"[a-f0-9]{64}", value[key])
                    for key in PRODUCER_CONDITION_KEYS - {"schema"}))


def checked_run_matches_profile(run, identity, conditions):
    """Revalidate the selected quality run without trusting a stored status."""
    if not isinstance(run, dict) or not valid_producer_conditions(conditions):
        return False
    try:
        runtime, pod, run_identity = run["identity"]["runtime"], run["pod"], run["identity"]
        derived = kernel_run_identity(run)
        run_conditions = producer_conditions({"runtime": runtime, "pod": pod}, conditions["runtime_sha256"])
    except (KeyError, TypeError, ValueError):
        return False
    return (derived == identity
            and run_identity.get("resources") == pod.get("resources")
            and all(run_conditions[key] == conditions[key]
                    for key in PRODUCER_CONDITION_KEYS - {"runtime_sha256"}))


def startup_matches_serving(rows, serving):
    """Bind successful startup observations to the serving image and launch."""
    pod = serving.get("pod") if isinstance(serving, dict) else None
    if not isinstance(pod, dict):
        raise ValueError("serving Pod identity is unavailable")
    for row in rows:
        if not isinstance(row, dict) or row.get("status") != "observed-not-qualified":
            continue
        observed = row.get("pod")
        if (not isinstance(observed, dict)
                or any(observed.get(key) != pod.get(key) for key in
                       ("node", "image", "image_id", "launch_spec_sha256", "resources"))):
            raise ValueError("startup observation does not match retained serving runtime identity")


def kernel_run_identity(run):
    """Derive the profile-relevant identity from one finalized checked run."""
    if not isinstance(run, dict) or not isinstance(run.get("identity"), dict) or not isinstance(run.get("pod"), dict):
        raise ValueError("checked kernel run identity is unavailable")
    record = {"runtime": run["identity"].get("runtime"), "pod": run["pod"],
              "runtime_sha256": run.get("runtime_sha256"), "workload_sha256": run.get("workload_sha256")}
    runtime = record["runtime"]
    if not isinstance(runtime, dict):
        raise ValueError("checked kernel run runtime is unavailable")
    # Kernel-run records retain the runtime object, but not its original raw
    # file. Validate the stable fields against the already verified serving
    # source; raw-file equality remains the serving/runtime source check above.
    return {"model_revision": runtime.get("settings", {}).get("MODEL_REVISION"),
            "tokenizer_sha256": tokenizer_digest(runtime), "workload_sha256": record["workload_sha256"],
            "hardware_sha256": canonical_digest({"node": record["pod"].get("node"), "devices": runtime.get("devices")}),
            "software_sha256": canonical_digest({"image": run["identity"].get("image"),
                                                   "image_id": run["identity"].get("image_id"),
                                                   "packages": runtime.get("packages"), "hip": runtime.get("hip")}),
            "launch_sha256": record["pod"].get("launch_spec_sha256"),
            "quantization": runtime.get("model_contract", {}).get("quant_method")}


def startup_metrics(rows):
    result = {"cold": {"values": [], "attempts": 0, "failed": 0, "unknown": 0},
              "warm": {"values": [], "attempts": 0, "failed": 0, "unknown": 0}}
    for row in rows:
        if (not isinstance(row, dict) or row.get("schema") != 1
                or row.get("cache_state") not in result):
            raise ValueError("unsupported startup record")
        phase = result[row["cache_state"]]
        phase["attempts"] += 1
        elapsed = row.get("elapsed_seconds")
        if row.get("status") == "observed-not-qualified" and finite(elapsed) and elapsed >= 0:
            phase["values"].append(elapsed)
        elif isinstance(row.get("status"), str) and (row["status"] == "failed"
                                                       or row["status"].startswith("failed-")
                                                       or row["status"] in ("cancelled", "timeout", "timed-out",
                                                                            "recovery-required")):
            phase["failed"] += 1
        else:
            phase["unknown"] += 1
    answer = {}
    for phase, values in result.items():
        known = values["values"]
        state = "observed" if known and not values["failed"] and not values["unknown"] else "incomplete"
        answer[phase] = {"status": state, "attempts": values["attempts"], "samples": len(known),
                         "failed": values["failed"], "unknown": values["unknown"],
                         "median_seconds": statistics.median(known) if known else None}
    return answer


def memory_metrics(plan):
    if plan is None:
        return {"status": "unknown"}
    if not isinstance(plan, dict) or plan.get("schema") != 1 or plan.get("kind") != "sglang-host-memory-plan":
        raise ValueError("unsupported memory plan")
    budget = plan.get("budget")
    if not isinstance(budget, dict):
        raise ValueError("memory budget unavailable")
    values = {key: budget.get(key) for key in ("observed_envelope_bytes", "headroom_bytes", "shm_limit_mib")}
    if any(not isinstance(value, int) or value < 0 for value in values.values()):
        raise ValueError("invalid memory budget")
    source_status = plan.get("status", "unknown")
    if not isinstance(source_status, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", source_status):
        raise ValueError("invalid memory plan status")
    result = {"status": "observed" if source_status == "plan-only-unqualified" else "failed-or-unknown",
              "source_status": source_status, **values,
              "candidate_mib": plan.get("candidate_mib"), "baseline_mib": plan.get("baseline_mib"),
              "scope": "candidate envelope and reserve; not an average RSS measurement"}
    # These fields are optional.  The current planner does not manufacture a
    # pressure counter, but a future sealed plan may report one explicitly.
    # Preserve it so an observed failure cannot be hidden by its envelope.
    for key in ("failure_status", "pressure_status"):
        if key in plan:
            value = plan[key]
            if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", value):
                raise ValueError(f"invalid memory {key}")
            result[key] = value
    return result


def quality_metrics(value):
    if value is None:
        return {"status": "unknown"}
    if value.get("schema") != 1:
        raise ValueError("unsupported numerical quality result")
    return {"status": "passed" if value.get("status") == "sampled-quality-passed-not-qualified" else "failed-or-unknown",
            "source_status": value.get("status")}


def coding_metrics(value):
    if value is None:
        return {"status": "unknown"}
    if value.get("schema") != 1 or value.get("kind") != "workstation-coding-evaluation":
        raise ValueError("unsupported coding evaluation")
    templates = value.get("template_sha256")
    if (not isinstance(value.get("corpus_sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", value["corpus_sha256"])
            or not isinstance(templates, dict) or not 8 <= len(templates) <= 12
            or any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", name)
                   or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
                   for name, digest in templates.items())):
        raise ValueError("coding evaluation provenance is incomplete")
    coding_eval.generation_settings(value.get("generation"))
    return {"status": value.get("status", "unknown"), "successes": value.get("successes"),
            "failures": value.get("failures"), "unavailable": value.get("unavailable"),
            "corpus_sha256": value["corpus_sha256"], "template_sha256": value["template_sha256"],
            "generation": value["generation"]}


def power_metrics(value):
    """Summarise available hwmon power sensors without claiming wall power."""
    if value is None:
        return {"status": "unknown", "scope": "device-power evidence unavailable"}
    snapshots = value if isinstance(value, list) else [value]
    readings = {}
    for snapshot in snapshots:
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("hwmon"), dict):
            raise ValueError("unsupported device-power evidence")
        for device, item in snapshot["hwmon"].items():
            if not isinstance(device, str) or not isinstance(item, dict) or not isinstance(item.get("values"), dict):
                raise ValueError("invalid hwmon device-power evidence")
            for name, raw in item["values"].items():
                if not isinstance(name, str) or not name.startswith(("power", "energy")):
                    continue
                try:
                    number = float(raw)
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(number) or number < 0:
                    raise ValueError("invalid device-power reading")
                # Snapshot ABI reports power in micro-Watts and energy in
                # micro-Joules. Keep energy as an observation, not a power rate.
                unit = "W" if name.startswith("power") else "uJ"
                value = number / 1e6 if unit == "W" else number
                readings.setdefault(f"{device}:{name}", {"unit": unit, "values": []})["values"].append(value)
    if not readings:
        return {"status": "unknown", "scope": "no readable device-power sensors"}
    sensors = {}
    for name, row in readings.items():
        sensors[name] = {"samples": len(row["values"]), "median": statistics.median(row["values"]),
                         "unit": row["unit"], "p95": sorted(row["values"])[math.ceil(.95 * len(row["values"])) - 1]
                         if len(row["values"]) >= 20 else None}
    return {"status": "observed", "sensors": sensors,
            "scope": "hwmon device sensor readings; not wall power and not necessarily GPU-BDF attributed"}


def inspect(bundle_path):
    """Read a sealed bundle and return bounded normalized comparison evidence.

    The manifest contains relative paths only.  It is safe for Bridge to use
    this function while checking an administrator-approved bundle directory.
    """
    bundle = Path(bundle_path)
    if bundle.is_symlink() or not bundle.is_dir():
        raise ValueError("evidence bundle must be a real directory")
    manifest, manifest_hash = read_json(bundle / "manifest.json")
    if (manifest.get("schema") != 1 or manifest.get("kind") != "workstation-performance-evidence"
            or manifest.get("status") not in ("measured-not-qualified", "failed", "incomplete")):
        raise ValueError("unsupported performance evidence manifest")
    profile_id = safe_id(manifest.get("profile_id"), "profile ID")
    identity = manifest.get("identity")
    if not isinstance(identity, dict) or set(identity) != set(IDENTITY):
        raise ValueError("performance identity must contain exactly the supported fields")
    for key, value in identity.items():
        if not isinstance(value, str) or not value:
            raise ValueError(f"missing identity {key}")
    experiment = manifest.get("experiment", {})
    if (not isinstance(experiment, dict) or set(experiment) != {"variables", "profile", "prefix_state"}
            or not isinstance(experiment["variables"], dict)):
        raise ValueError("invalid experiment variables")
    if set(experiment["variables"]) - VARIABLES or any(not isinstance(value, str) for value in experiment["variables"].values()):
        raise ValueError("unsupported experiment variable")
    if (not isinstance(experiment["profile"], str) or not experiment["profile"]
            or experiment["prefix_state"] not in ("new-prefix", "warm-prefix")):
        raise ValueError("invalid experiment serving labels")
    sources = manifest.get("sources")
    if not isinstance(sources, dict) or set(sources) - {"serving", "runtime", "startup", "memory", "quality", "kernel_runs", "coding", "power"}:
        raise ValueError("invalid performance evidence sources")
    if not {"serving", "runtime", "startup"} <= set(sources):
        raise ValueError("serving, runtime and startup evidence are required")
    artifact_hashes, decoded = {}, {}
    for kind, location in sources.items():
        locations = location if kind in ("startup", "kernel_runs") else [location]
        if not isinstance(locations, list) or not locations:
            raise ValueError("startup evidence must be a nonempty list")
        decoded[kind] = []
        for item in locations:
            value, digest = read_json(relative_file(bundle, item))
            decoded[kind].append(value)
            artifact_hashes[f"{kind}:{item}"] = digest
    # Serving is one result.  Requiring exactly one makes a failed run visible
    # rather than letting a convenient result overwrite it in an aggregate.
    if (len(decoded["serving"]) != 1 or len(decoded["runtime"]) != 1
            or any(len(decoded[key]) != 1 for key in ("memory", "quality", "coding", "power") if key in decoded)
            or ("quality" in decoded and ("kernel_runs" not in decoded or len(decoded["kernel_runs"]) != 2))):
        raise ValueError("invalid performance evidence source cardinality")
    runtime_location = sources["runtime"]
    runtime_digest = artifact_hashes[f"runtime:{runtime_location}"]
    derived_identity = producer_identity(decoded["serving"][0], decoded["runtime"][0], runtime_digest)
    conditions = producer_conditions(decoded["serving"][0], runtime_digest)
    condition_details = producer_condition_details(decoded["serving"][0])
    if identity != derived_identity:
        raise ValueError("manifest identity does not match retained serving producer evidence")
    startup_matches_serving(decoded["startup"], decoded["serving"][0])
    if (experiment["profile"] != decoded["serving"][0].get("profile")
            or experiment["prefix_state"] != decoded["serving"][0].get("prefix_state")):
        raise ValueError("manifest experiment labels do not match retained serving producer evidence")
    if "quality" in decoded:
        quality = decoded["quality"][0]
        runs = decoded["kernel_runs"]
        try:
            if any(run.get("schema") != 1 for run in runs):
                raise ValueError("kernel-run schema is unsupported")
            memory_only = quality.get("comparison") == "host-memory-only" if isinstance(quality, dict) else False
            tolerances = quality.get("quality") if isinstance(quality, dict) else None
            expected_quality = model_kernels.compare_quality(runs[0], runs[1], tolerances["atol"], tolerances["rtol"], memory_only)
            selected_run = runs[1]
            selected_matches_serving = (
                selected_run.get("pod") == decoded["serving"][0]["pod"]
                and selected_run.get("workload_sha256") == decoded["serving"][0]["workload_sha256"]
                and selected_run.get("identity", {}).get("runtime") == decoded["serving"][0]["runtime"]
                and checked_run_matches_profile(selected_run, derived_identity, conditions))
        except (KeyError, TypeError, ValueError):
            raise ValueError("numerical quality does not bind retained checked kernel runs") from None
        if (quality != expected_quality
                or quality["baseline_sha256"] != canonical_digest(runs[0])
                or quality["candidate_sha256"] != canonical_digest(runs[1])
                or not selected_matches_serving):
            raise ValueError("numerical quality does not bind retained checked kernel runs")
    else:
        quality, selected_run = None, None
    return {"schema": 1, "kind": "workstation-performance-evidence-inspection", "status": manifest["status"],
            "profile_id": profile_id, "identity": identity, "producer_conditions": conditions,
            "producer_condition_details": condition_details, "experiment": experiment,
            "quality_result": quality, "quality_run": selected_run,
            "quality_run_sha256": canonical_digest(selected_run) if selected_run else None,
            "quality_run_artifact_sha256": artifact_hashes[f"kernel_runs:{sources['kernel_runs'][1]}"] if selected_run else None,
            "quality_run_source": sources["kernel_runs"][1] if selected_run else None,
            "quality_baseline_run": runs[0] if selected_run else None,
            "quality_baseline_run_sha256": canonical_digest(runs[0]) if selected_run else None,
            "quality_baseline_run_artifact_sha256": artifact_hashes[f"kernel_runs:{sources['kernel_runs'][0]}"] if selected_run else None,
            "quality_baseline_run_source": sources["kernel_runs"][0] if selected_run else None,
            "manifest_sha256": manifest_hash, "artifact_sha256": artifact_hashes,
            "metrics": {"serving": serving_metrics(decoded["serving"][0]),
                        "startup": startup_metrics(decoded["startup"]),
                        "memory": memory_metrics(decoded.get("memory", [None])[0]),
                        "numerical_quality": quality_metrics(decoded.get("quality", [None])[0]),
                        "coding": coding_metrics(decoded.get("coding", [None])[0]),
                        "device_power": power_metrics(decoded.get("power", [None])[0])},
            "limitations": ["Evidence remains unqualified and is not a deployment instruction.",
                            "Failed and incomplete inputs are retained, not converted to zero measurements.",
                            "Device power is not wall power."]}


def policy(value):
    if not isinstance(value, dict) or value.get("schema") != 1 or set(value) != {"schema", "minimum_practical_gain_percent", "maximum_regression_percent", "noise_percent"}:
        raise ValueError("unsupported comparison policy")
    result = {key: value[key] for key in value if key != "schema"}
    if any(not finite(number) or not 0 <= number <= 100 for number in result.values()):
        raise ValueError("comparison thresholds must be finite percentages in 0..100")
    return result


def differences(first, second, declared):
    if not isinstance(declared, list) or len(set(declared)) != len(declared) or set(declared) - VARIABLES:
        raise ValueError("declared variables are unsupported or duplicated")
    all_differences = []
    for key in IDENTITY:
        if first["identity"][key] != second["identity"][key]:
            declaration = {"model_revision": "model", "tokenizer_sha256": "tokenizer", "workload_sha256": "workload",
                           "hardware_sha256": "hardware", "software_sha256": "software", "launch_sha256": "launch",
                           "quantization": "quantization"}[key]
            all_differences.append({"field": key, "baseline": first["identity"][key], "candidate": second["identity"][key],
                                    "declared_as": declaration, "declared": declaration in declared})
    # The raw runtime hash remains provenance, not a comparison variable: it
    # includes legitimate model/launch variation. These normalized conditions
    # expose the substantive retained source changes that its digest covers.
    for key, declaration in (("model_files_sha256", "model"), ("model_settings_sha256", "model"),
                             ("observed_launch_sha256", "launch"), ("launch_settings_sha256", "launch"),
                             ("resources_sha256", "resources")):
        left, right = first["producer_conditions"][key], second["producer_conditions"][key]
        if left != right:
            if key == "observed_launch_sha256":
                left = first.get("producer_condition_details", {}).get("observed_launch")
                right = second.get("producer_condition_details", {}).get("observed_launch")
            all_differences.append({"field": f"producer.{key}", "baseline": left, "candidate": right,
                                    "declared_as": declaration, "declared": declaration in declared})
    keys = set(first["experiment"]["variables"]) | set(second["experiment"]["variables"])
    for key in sorted(keys):
        left, right = first["experiment"]["variables"].get(key), second["experiment"]["variables"].get(key)
        if left != right:
            all_differences.append({"field": key, "baseline": left, "candidate": right,
                                    "declared_as": key, "declared": key in declared})
    for key in ("profile", "prefix_state"):
        left, right = first["experiment"][key], second["experiment"][key]
        if left != right:
            all_differences.append({"field": key, "baseline": left, "candidate": right,
                                    "declared_as": key, "declared": key in declared})
    if any(not row["declared"] for row in all_differences):
        raise ValueError("comparison has undeclared or incompatible differences")
    if "model" in declared and "hardware" in declared:
        raise ValueError("different-model evidence cannot be called GPU scaling")
    return all_differences


def coding_differences(first, second, declared):
    """Bind task-quality results before treating serving throughput as equivalent."""
    left, right = first["metrics"]["coding"], second["metrics"]["coding"]
    result = []
    for field, declaration in (("corpus_sha256", "coding_corpus"), ("template_sha256", "coding_corpus"),
                               ("generation", "generation")):
        if left.get(field) != right.get(field):
            if declaration not in declared:
                raise ValueError("coding evaluation provenance differs without a declared experiment variable")
            result.append({"field": f"coding.{field}", "baseline": left.get(field), "candidate": right.get(field),
                           "declared_as": declaration, "declared": True})
    return result


def retained_quality_run(record):
    """Verify report-carried checked-run bindings before profile selection."""
    run, baseline, quality = record.get("quality_run"), record.get("quality_baseline_run"), record.get("quality_result")
    if not isinstance(run, dict) or not isinstance(baseline, dict) or not isinstance(quality, dict):
        return None
    artifact, baseline_artifact = record.get("quality_run_artifact_sha256"), record.get("quality_baseline_run_artifact_sha256")
    source, baseline_source = record.get("quality_run_source"), record.get("quality_baseline_run_source")
    artifacts = record.get("artifact_sha256")
    details = record.get("producer_condition_details")
    conditions = record.get("producer_conditions")
    if (record.get("quality_run_sha256") != canonical_digest(run)
            or record.get("quality_baseline_run_sha256") != canonical_digest(baseline)
            or quality.get("candidate_sha256") != canonical_digest(run)
            or quality.get("baseline_sha256") != canonical_digest(baseline)
            or not isinstance(artifact, str) or not isinstance(baseline_artifact, str)
            or not isinstance(source, str) or not isinstance(baseline_source, str)
            or not isinstance(artifacts, dict)
            or artifact != artifacts.get(f"kernel_runs:{source}")
            or baseline_artifact != artifacts.get(f"kernel_runs:{baseline_source}")
            or not isinstance(details, dict)
            or not isinstance(conditions, dict)
            or details.get("observed_launch") != normalized_observed_launch(run["identity"]["runtime"])
            or conditions.get("observed_launch_sha256")
            != canonical_digest(details["observed_launch"])
            or not checked_run_matches_profile(run, record.get("identity"), conditions)):
        return None
    try:
        tolerances = quality["quality"]
        if model_kernels.compare_quality(baseline, run, tolerances["atol"], tolerances["rtol"],
                                         quality.get("comparison") == "host-memory-only") != quality:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return run


def cross_profile_quality(first, second, declared_differences):
    """Check the runs that actually represent the two compared profiles."""
    baseline, candidate = retained_quality_run(first), retained_quality_run(second)
    source = (first.get("quality_result"), second.get("quality_result"))
    if not isinstance(baseline, dict) or not isinstance(candidate, dict) or not all(isinstance(value, dict) for value in source):
        return {"status": "incomplete", "reason": "matched cross-profile kernel runs are unavailable"}
    if any(item["declared_as"] in NON_EQUIVALENT_VARIABLES for item in declared_differences):
        return {"status": "declared-variant", "reason": "declared non-equivalent conditions require separate quality review"}
    try:
        left, right = (value["quality"] for value in source)
        if left.get("atol") != right.get("atol") or left.get("rtol") != right.get("rtol"):
            raise ValueError("quality tolerances differ")
        result = model_kernels.compare_quality(baseline, candidate, left["atol"], left["rtol"])
    except (KeyError, TypeError, ValueError):
        return {"status": "incomplete", "reason": "matched cross-profile kernel runs or tolerances differ"}
    return {"status": "passed", "baseline_sha256": canonical_digest(baseline),
            "candidate_sha256": canonical_digest(candidate), "result": result}


def align_cases(first, second):
    """Pair only identical workload labels; never rely on list ordering alone."""
    left_metrics, right_metrics = first["metrics"]["serving"], second["metrics"]["serving"]
    if left_metrics["status"] != "observed" or right_metrics["status"] != "observed":
        return {"status": "incomplete", "reason": "serving evidence is incomplete or malformed",
                "baseline_status": left_metrics["status"], "candidate_status": right_metrics["status"],
                "baseline_cases": [], "candidate_cases": []}, []
    left, right = left_metrics["cases"], right_metrics["cases"]
    by_left = {(row["context_tokens"], row["concurrency"]): row for row in left}
    by_right = {(row["context_tokens"], row["concurrency"]): row for row in right}
    keys = sorted(set(by_left) | set(by_right))
    report = {"baseline_cases": [{"context_tokens": row["context_tokens"], "concurrency": row["concurrency"]}
                                 for row in left],
              "candidate_cases": [{"context_tokens": row["context_tokens"], "concurrency": row["concurrency"]}
                                  for row in right]}
    if set(by_left) != set(by_right):
        report.update(status="incompatible", reason="serving context/concurrency cases differ")
        return report, []
    report["status"] = "matching"
    return report, [(key, by_left[key], by_right[key]) for key in keys]


def percent_regression(baseline, candidate):
    """Return an honest percentage change, or unknown for a zero baseline."""
    if not finite(baseline) or not finite(candidate) or baseline <= 0:
        return None
    return (candidate - baseline) * 100 / baseline


def startup_gate(first, second, maximum_regression):
    result = {"status": "passed", "comparisons": [], "incomplete": []}
    for phase in ("cold", "warm"):
        baseline = first["metrics"]["startup"][phase]
        candidate = second["metrics"]["startup"][phase]
        if (baseline["status"] != "observed" or candidate["status"] != "observed"
                or baseline["failed"] or candidate["failed"]
                or baseline["unknown"] or candidate["unknown"]):
            result["incomplete"].append({"phase": phase, "baseline_status": baseline["status"],
                                         "candidate_status": candidate["status"],
                                         "baseline_failed": baseline["failed"], "candidate_failed": candidate["failed"],
                                         "baseline_unknown": baseline["unknown"], "candidate_unknown": candidate["unknown"]})
            continue
        regression = percent_regression(baseline["median_seconds"], candidate["median_seconds"])
        if regression is None:
            result["incomplete"].append({"phase": phase, "reason": "zero-or-unknown-baseline-median"})
            continue
        result["comparisons"].append({"phase": phase, "baseline_median_seconds": baseline["median_seconds"],
                                      "candidate_median_seconds": candidate["median_seconds"],
                                      "regression_percent": regression})
    if result["incomplete"]:
        result["status"] = "incomplete"
    elif any(row["regression_percent"] > maximum_regression for row in result["comparisons"]):
        result["status"] = "regressed"
    return result


def serving_latency_gate(paired_cases, maximum_regression):
    """Gate latency and TTFT independently from throughput.

    A missing median is not converted to a zero.  A short tail remains
    explicitly unavailable; where both tails exist, it receives the same
    regression tolerance as the median.
    """
    result = {"status": "passed", "comparisons": [], "incomplete": []}
    for key, baseline, candidate in paired_cases:
        label = {"context_tokens": key[0], "concurrency": key[1]}
        for metric in ("latency", "ttft"):
            left, right = baseline[metric], candidate[metric]
            if left["status"] != "observed" or right["status"] != "observed":
                result["incomplete"].append({**label, "metric": metric,
                                             "baseline_status": left["status"], "candidate_status": right["status"]})
                continue
            regression = percent_regression(left["median"], right["median"])
            if regression is None:
                result["incomplete"].append({**label, "metric": metric,
                                             "reason": "zero-or-unknown-baseline-median"})
                continue
            result["comparisons"].append({**label, "metric": metric, "statistic": "median",
                                          "baseline": left["median"], "candidate": right["median"],
                                          "regression_percent": regression})
            if left.get("p95") is not None and right.get("p95") is not None:
                regression = percent_regression(left["p95"], right["p95"])
                if regression is None:
                    result["incomplete"].append({**label, "metric": metric,
                                                 "statistic": "p95", "reason": "zero-baseline-p95"})
                else:
                    result["comparisons"].append({**label, "metric": metric, "statistic": "p95",
                                                  "baseline": left["p95"], "candidate": right["p95"],
                                                  "regression_percent": regression})
    if result["incomplete"]:
        result["status"] = "incomplete"
    elif any(row["regression_percent"] > maximum_regression for row in result["comparisons"]):
        result["status"] = "regressed"
    return result


def known_adverse_memory_status(value):
    return value not in (None, "unknown", "none", "not-observed")


def memory_gate(first, second):
    """Reject a retained plan that explicitly records failed/pressure evidence.

    Memory evidence is optional, so an absent plan remains unknown rather than
    becoming a fabricated healthy measurement.  A plan refusal, or an explicit
    future pressure/failure field, is known adverse evidence and cannot support
    selecting an otherwise fast candidate.
    """
    adverse = []
    for side, record in (("baseline", first), ("candidate", second)):
        metrics = record["metrics"]["memory"]
        if metrics["status"] == "unknown":
            continue
        for key in ("source_status", "failure_status", "pressure_status"):
            value = metrics.get(key)
            if key == "source_status":
                unhealthy = value not in ("plan-only-unqualified", "unknown")
            else:
                unhealthy = known_adverse_memory_status(value)
            if unhealthy:
                adverse.append({"side": side, "field": key, "status": value})
    return {"status": "failed-or-pressure-observed" if adverse else "passed", "adverse": adverse}


def throughput_gate(paired_cases, configured_noise):
    """Require repeated, non-zero observations and account for observed spread."""
    result = {"status": "passed", "cases": [], "incomplete": []}
    for key, baseline, candidate in paired_cases:
        left, right = baseline["throughput"], candidate["throughput"]
        label = {"context_tokens": key[0], "concurrency": key[1]}
        if (left["status"] != "observed" or right["status"] != "observed"
                or left["samples"] < 3 or right["samples"] < 3):
            result["incomplete"].append({**label, "baseline_status": left["status"],
                                         "candidate_status": right["status"],
                                         "baseline_samples": left.get("samples"),
                                         "candidate_samples": right.get("samples")})
            continue
        if left["median"] <= 0 or right["median"] <= 0:
            result["incomplete"].append({**label, "reason": "nonpositive-throughput-median"})
            continue
        gain = percent_regression(left["median"], right["median"])
        if gain is None:
            result["incomplete"].append({**label, "reason": "zero-or-unknown-baseline-median"})
            continue
        variation = 100 * (left["stdev"] / left["median"] + right["stdev"] / right["median"])
        result["cases"].append({**label, "gain_percent": gain,
                                "observed_variation_percent": variation,
                                "required_gain_over_noise_percent": max(configured_noise, variation)})
    if result["incomplete"]:
        result["status"] = "incomplete"
    return result


def comparison_record(first, second, declared_variables, thresholds):
    """Compute the canonical report from already-inspected evidence records."""
    if first["profile_id"] == second["profile_id"]:
        raise ValueError("baseline and candidate profile IDs must differ")
    declared_differences = differences(first, second, declared_variables)
    declared_differences += coding_differences(first, second, declared_variables)
    cross_quality = cross_profile_quality(first, second, declared_differences)
    limits = policy(thresholds)
    base = first["metrics"]["serving"]
    candidate = second["metrics"]["serving"]
    outcome, recommendation = "inconclusive", "retain-baseline"
    gain = None
    case_alignment, paired_cases = align_cases(first, second)
    equivalent_variables = not any(row["declared_as"] in NON_EQUIVALENT_VARIABLES for row in declared_differences)
    gates = {"baseline": {"numerical": first["metrics"]["numerical_quality"]["status"],
                          "coding": first["metrics"]["coding"]["status"]},
             "candidate": {"numerical": second["metrics"]["numerical_quality"]["status"],
                           "coding": second["metrics"]["coding"]["status"]},
             "cross_profile": cross_quality,
             "provenance": "matching" if not coding_differences(first, second, declared_variables) else "declared-variant"}
    quality_passed = all(value == "passed" for side in (gates["baseline"], gates["candidate"])
                         for value in side.values()) and gates["cross_profile"]["status"] == "passed" and gates["provenance"] == "matching"
    performance_gates = {"startup": startup_gate(first, second, limits["maximum_regression_percent"]),
                         "serving_latency": serving_latency_gate(paired_cases, limits["maximum_regression_percent"]),
                         "memory": memory_gate(first, second),
                         "throughput": throughput_gate(paired_cases, limits["noise_percent"])}
    measurements_ready = (first["status"] == second["status"] == "measured-not-qualified"
                          and base.get("source_status") == candidate.get("source_status") == "measured-not-qualified"
                          and all(row["failures"] == 0 for _key, row, _other in paired_cases)
                          and all(row["failures"] == 0 for _key, _other, row in paired_cases)
                          and all(row["throughput"]["status"] == "observed" for _key, left, right in paired_cases for row in (left, right)))
    if measurements_ready and paired_cases:
        gains = performance_gates["throughput"]["cases"]
        gain = min((row["gain_percent"] for row in gains), default=None)
        if case_alignment["status"] != "matching" or not equivalent_variables:
            outcome = "inconclusive-incomparable-conditions"
        elif not quality_passed:
            outcome = "inconclusive-quality-evidence"
        elif performance_gates["startup"]["status"] == "regressed":
            outcome = "candidate-regresses-startup"
        elif performance_gates["startup"]["status"] != "passed":
            outcome = "inconclusive-incomplete-startup-evidence"
        elif performance_gates["serving_latency"]["status"] == "regressed":
            outcome = "candidate-regresses-serving-latency-or-ttft"
        elif performance_gates["serving_latency"]["status"] != "passed":
            outcome = "inconclusive-incomplete-serving-latency-evidence"
        elif performance_gates["memory"]["status"] != "passed":
            outcome = "inconclusive-adverse-memory-evidence"
        elif performance_gates["throughput"]["status"] != "passed":
            outcome = "inconclusive-incomplete-serving-throughput-evidence"
        elif gain is None:
            outcome = "inconclusive-incomplete-serving-throughput-evidence"
        elif any(abs(row["gain_percent"]) <= row["required_gain_over_noise_percent"] for row in gains):
            outcome = "inconclusive-noisy"
        elif gain >= limits["minimum_practical_gain_percent"]:
            outcome, recommendation = "candidate-improves-throughput", "candidate"
        elif gain <= -limits["maximum_regression_percent"]:
            outcome = "candidate-regresses-throughput"
    elif case_alignment["status"] == "incompatible":
        outcome = "inconclusive-incompatible-cases"
    elif case_alignment["status"] == "incomplete":
        outcome = "inconclusive-incomplete-serving-evidence"
    return {"schema": 1, "kind": "workstation-performance-comparison", "status": "comparison-not-qualified",
            "baseline": first, "candidate": second, "declared_variables": declared_variables,
            "differences": declared_differences, "case_alignment": case_alignment, "thresholds": limits, "outcome": outcome,
            "minimum_case_throughput_gain_percent": gain, "quality_gates": gates,
            "performance_gates": performance_gates,
            "recommendation": recommendation,
            "limitations": ["Small/noisy changes are inconclusive; do not select by a single token-rate maximum.",
                            "A candidate recommendation requires complete startup and serving latency/TTFT evidence, and no known adverse memory evidence.",
                            "Cold/warm startup, load/JIT/warmup and steady serving remain separately reported where evidence exists.",
                            "A selected profile is configuration only and never deploys or restarts a workload."]}


def compare(baseline_bundle, candidate_bundle, declared_variables, thresholds):
    return comparison_record(inspect(baseline_bundle), inspect(candidate_bundle), declared_variables, thresholds)


def validate_comparison(comparison):
    """Reject a free-form selection input that only imitates a recommendation.

    The comparison retains inspected evidence identities and artifact hashes.
    Recomputing its canonical report binds its threshold, declared differences,
    case alignment and quality gates before a selection can cite those records.
    It cannot make owner-supplied evidence a hardware qualification.
    """
    if not isinstance(comparison, dict):
        raise ValueError("unsupported comparison record")
    try:
        thresholds = {"schema": 1, **comparison["thresholds"]} if isinstance(comparison["thresholds"], dict) else None
        expected = comparison_record(comparison["baseline"], comparison["candidate"],
                                     comparison["declared_variables"], thresholds)
        actual = json.dumps(comparison, sort_keys=True, separators=(",", ":"), allow_nan=False)
        canonical = json.dumps(expected, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("comparison record is malformed") from error
    if actual != canonical:
        raise ValueError("comparison record does not match its inspected evidence and declared policy")
    return expected


def select(comparison, owner, candidate_id, previous=None):
    comparison = validate_comparison(comparison)
    owner, candidate_id = safe_id(owner, "owner"), safe_id(candidate_id, "candidate ID")
    if candidate_id != comparison["candidate"]["profile_id"]:
        raise ValueError("selection must name the comparison candidate exactly")
    if comparison.get("recommendation") != "candidate":
        raise ValueError("candidate is not eligible for selection from this comparison")
    previous_record = None
    if previous is not None:
        if previous.get("schema") != 1 or previous.get("kind") != "workstation-measured-profile-selection":
            raise ValueError("unsupported previous selection")
        previous_record = {"profile_id": previous["selected_profile_id"], "selection_sha256": digest_bytes(json.dumps(previous, sort_keys=True, separators=(",", ":")).encode())}
    return {"schema": 1, "kind": "workstation-measured-profile-selection", "status": "selected-unqualified",
            "owner": owner, "selected_profile_id": candidate_id, "baseline_profile_id": comparison["baseline"]["profile_id"],
            "previous_selection": previous_record, "comparison_sha256": digest_bytes(json.dumps(comparison, sort_keys=True, separators=(",", ":")).encode()),
            "runtime_identity": comparison["candidate"]["identity"], "evidence": {"candidate_manifest_sha256": comparison["candidate"]["manifest_sha256"],
                                                                            "baseline_manifest_sha256": comparison["baseline"]["manifest_sha256"]},
            "producer_conditions": comparison["candidate"]["producer_conditions"],
            "selection_reason": comparison["recommendation"],
            "limitations": ["Owner selection retains the prior baseline and does not apply a source patch or start a workload.",
                            "Qualification is stale after a relevant runtime, model, launch, software, or hardware identity change."]}


def selection_status(selection, current_identity, current_conditions=None):
    if selection.get("schema") != 1 or selection.get("kind") != "workstation-measured-profile-selection":
        raise ValueError("unsupported selection")
    if not isinstance(current_identity, dict) or set(current_identity) != set(IDENTITY):
        raise ValueError("current identity is incomplete")
    changed = [key for key in IDENTITY if current_identity[key] != selection["runtime_identity"].get(key)]
    if changed:
        return {"schema": 1, "kind": "workstation-measured-profile-status", "status": "stale",
                "selected_profile_id": selection["selected_profile_id"], "changed_identity_fields": changed,
                "changed_condition_fields": [],
                "reason": "selected profile identity changed: " + ", ".join(changed)}
    selected_conditions = selection.get("producer_conditions")
    if not valid_producer_conditions(selected_conditions):
        return {"schema": 1, "kind": "workstation-measured-profile-status", "status": "unknown",
                "selected_profile_id": selection["selected_profile_id"], "changed_identity_fields": changed,
                "reason": "selected profile lacks normalized source conditions"}
    if not valid_producer_conditions(current_conditions):
        return {"schema": 1, "kind": "workstation-measured-profile-status", "status": "unknown",
                "selected_profile_id": selection["selected_profile_id"], "changed_identity_fields": changed,
                "reason": "current identity observation lacks normalized source conditions"}
    condition_changed = [key for key in sorted(selected_conditions) if current_conditions[key] != selected_conditions[key]]
    return {"schema": 1, "kind": "workstation-measured-profile-status",
            "status": "stale" if condition_changed else "selected-unqualified-current",
            "selected_profile_id": selection["selected_profile_id"], "changed_identity_fields": changed,
            "changed_condition_fields": condition_changed,
            "reason": ("current identity matches the selected unqualified profile" if not condition_changed
                       else "selected profile source conditions changed: " + ", ".join(condition_changed))}


def write(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("x") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")


def readable_report(comparison):
    lines = ["Workstation performance comparison", f"Status: {comparison['status']}",
             f"Baseline: {comparison['baseline']['profile_id']}", f"Candidate: {comparison['candidate']['profile_id']}",
             f"Outcome: {comparison['outcome']}", f"Recommendation: {comparison['recommendation']}"]
    if comparison["minimum_case_throughput_gain_percent"] is None:
        lines.append("Minimum case throughput gain: unknown")
    else:
        lines.append(f"Minimum case throughput gain: {comparison['minimum_case_throughput_gain_percent']:.2f}%")
    lines.append("Declared differences: " + (", ".join(row["field"] for row in comparison["differences"]) or "none"))
    lines.append("Case alignment: " + comparison["case_alignment"]["status"])
    lines.append("Baseline numerical/coding quality: " + comparison["quality_gates"]["baseline"]["numerical"] + "/" + comparison["quality_gates"]["baseline"]["coding"])
    lines.append("Candidate numerical/coding quality: " + comparison["quality_gates"]["candidate"]["numerical"] + "/" + comparison["quality_gates"]["candidate"]["coding"])
    lines.append("Startup/latency/memory gates: " + comparison["performance_gates"]["startup"]["status"] + "/"
                 + comparison["performance_gates"]["serving_latency"]["status"] + "/"
                 + comparison["performance_gates"]["memory"]["status"])
    lines.append("This report is not a deployment or hardware qualification.")
    return "\n".join(lines) + "\n"


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    check = sub.add_parser("inspect")
    check.add_argument("bundle")
    compare_parser = sub.add_parser("compare")
    compare_parser.add_argument("baseline_bundle")
    compare_parser.add_argument("candidate_bundle")
    compare_parser.add_argument("policy_json")
    compare_parser.add_argument("output")
    compare_parser.add_argument("--declare", action="append", default=[])
    choose = sub.add_parser("select")
    choose.add_argument("comparison")
    choose.add_argument("owner")
    choose.add_argument("candidate_id")
    choose.add_argument("output")
    choose.add_argument("--previous")
    state = sub.add_parser("status")
    state.add_argument("selection")
    state.add_argument("current_identity")
    state.add_argument("--conditions", help="owner-prepared normalized source conditions JSON")
    args = parser.parse_args()
    if args.action == "inspect":
        print(json.dumps(inspect(args.bundle), indent=2))
    elif args.action == "compare":
        values, _ = read_json(args.policy_json)
        result = compare(args.baseline_bundle, args.candidate_bundle, args.declare, values)
        output = Path(args.output)
        output.mkdir(mode=0o700)
        write(output / "comparison.json", result)
        (output / "report.txt").write_text(readable_report(result), encoding="utf-8")
    elif args.action == "select":
        comparison, _ = read_json(args.comparison)
        previous = read_json(args.previous)[0] if args.previous else None
        write(args.output, select(comparison, args.owner, args.candidate_id, previous))
    else:
        selection, _ = read_json(args.selection)
        current, _ = read_json(args.current_identity)
        conditions = read_json(args.conditions)[0] if args.conditions else None
        print(json.dumps(selection_status(selection, current, conditions), indent=2))


if __name__ == "__main__":
    main()
