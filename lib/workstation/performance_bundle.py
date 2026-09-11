"""Seal and run one typed offline workstation performance-analysis bundle.

Bridge validates the transport manifest independently.  This dispatcher keeps
the algorithms in their installer modules and accepts no command strings,
network endpoints, client paths, executable names, or environment maps.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat

import coding_eval
import inference_cache
import performance_profiles
import serving_runtime
from model_kernels import evidence as kernel_evidence


MAX_FILE = 64 * 1024 * 1024
MAX_TOTAL = 256 * 1024 * 1024
MAX_OUTPUT_FILE = 512 * 1024
MAX_FILES = 256
MAX_ARTIFACTS = 100
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
DIGEST = re.compile(r"[a-f0-9]{64}\Z")
KINDS = frozenset(("comparison", "coding-eval", "loading", "queue", "warm-status", "cache", "profile-selection", "profile-status"))
MAX_PROFILE_IDENTITY_AGE_SECONDS = 15 * 60
BOOT_ID = re.compile(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\Z")


def sha(path):
    data = Path(path).read_bytes()
    return hashlib.sha256(data).hexdigest(), len(data)


def json_read(path, maximum=MAX_FILE):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise ValueError("bundle JSON must be a regular bounded file")
    try:
        return json.loads(path.read_text())
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("bundle JSON is invalid") from error


def rel(value):
    if not isinstance(value, str) or not value or len(value) > 240 or value.startswith("/"):
        raise ValueError("bundle paths must be bounded relative paths")
    path = Path(value)
    if any(part in ("", ".", "..") or not NAME.fullmatch(part) for part in path.parts):
        raise ValueError("bundle path is unsafe")
    return path


def child(root, value):
    result = root / rel(value)
    if result.is_symlink():
        raise ValueError("bundle path is a symlink")
    resolved = result.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise ValueError("bundle path escapes its root") from error
    return result


def private(info):
    return stat.S_IMODE(info.st_mode) & 0o077 == 0


def tree(root, include_manifest=False, output=False):
    root = Path(root)
    if root.is_symlink() or not root.is_dir() or not private(root.stat()):
        raise ValueError("bundle tree must be a private real directory")
    entries, total = {}, 0
    for directory, names, files in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        info = directory_path.stat(follow_symlinks=False)
        if directory_path.is_symlink() or not stat.S_ISDIR(info.st_mode) or not private(info):
            raise ValueError("bundle directory is unsafe or not private")
        for name in names + files:
            if not NAME.fullmatch(name):
                raise ValueError("bundle tree has an unsafe name")
        for name in files:
            path = directory_path / name
            relative = path.relative_to(root).as_posix()
            if relative == "manifest.json" and not include_manifest:
                continue
            info = path.stat(follow_symlinks=False)
            if path.is_symlink() or not stat.S_ISREG(info.st_mode) or not private(info):
                raise ValueError("bundle file is unsafe or not private")
            if info.st_size > (MAX_OUTPUT_FILE if output else MAX_FILE):
                raise ValueError("bundle file exceeds its size bound")
            total += info.st_size
            if total > (MAX_OUTPUT_FILE * MAX_ARTIFACTS if output else MAX_TOTAL):
                raise ValueError("bundle tree exceeds its total size bound")
            entries[relative] = sha(path)[0]
    if len(entries) > (MAX_ARTIFACTS if output else MAX_FILES):
        raise ValueError("bundle tree has too many files")
    return entries


def spec_schema(kind, value):
    """Validate small typed parameters before a canonical method sees them."""
    if not isinstance(value, dict) or value.get("schema") != 1 or value.get("kind") != kind:
        raise ValueError("spec kind/schema differs from bundle manifest")
    if kind == "comparison":
        expected = {"schema", "kind", "baseline_bundle", "candidate_bundle", "policy_json", "declared_variables"}
        if set(value) != expected or not isinstance(value["declared_variables"], list):
            raise ValueError("invalid comparison spec")
        for key in ("baseline_bundle", "candidate_bundle", "policy_json"):
            rel(value[key])
        if len(value["declared_variables"]) > 16 or any(not isinstance(item, str) for item in value["declared_variables"]):
            raise ValueError("invalid declared comparison variables")
    elif kind == "coding-eval":
        if set(value) != {"schema", "kind", "responses", "corpus"}:
            raise ValueError("invalid coding evaluation spec")
        rel(value["responses"]); rel(value["corpus"])
    elif kind == "loading":
        if set(value) != {"schema", "kind", "deployment", "evidence", "threads", "reserve_mib", "per_thread_mib"}:
            raise ValueError("invalid loading spec")
        rel(value["deployment"]); rel(value["evidence"])
        if not isinstance(value["threads"], list) or not value["threads"] or any(type(v) is not int for v in value["threads"]):
            raise ValueError("invalid loading threads")
        if type(value["reserve_mib"]) is not int or type(value["per_thread_mib"]) is not int:
            raise ValueError("invalid loading budget")
    elif kind == "queue":
        if set(value) != {"schema", "kind", "deployment", "evidence", "maximum_queued"}:
            raise ValueError("invalid queue spec")
        rel(value["deployment"]); rel(value["evidence"])
        if type(value["maximum_queued"]) is not int:
            raise ValueError("invalid queue maximum")
    elif kind == "warm-status":
        if set(value) != {"schema", "kind", "evidence", "warmup", "lifecycle"}:
            raise ValueError("invalid warm-status spec")
        rel(value["evidence"])
        if value["warmup"] is not None:
            rel(value["warmup"])
        if value["lifecycle"] is not None:
            rel(value["lifecycle"])
    elif kind == "cache":
        if value.get("mode") == "inventory":
            expected = {"schema", "kind", "mode", "registry"}
            rel(value.get("registry"))
        elif value.get("mode") == "plan":
            expected = {"schema", "kind", "mode", "inventory"}
            rel(value.get("inventory"))
        else:
            raise ValueError("cache mode is unsupported")
        if set(value) != expected:
            raise ValueError("invalid cache spec")
    elif kind == "profile-selection":
        expected = {"schema", "kind", "comparison", "candidate_id", "previous_selection"}
        if set(value) != expected:
            raise ValueError("invalid profile selection spec")
        rel(value["comparison"])
        if not NAME.fullmatch(value.get("candidate_id", "")) or value["previous_selection"] is not None and not isinstance(value["previous_selection"], str):
            raise ValueError("invalid profile selection identity")
        if value["previous_selection"] is not None:
            rel(value["previous_selection"])
    else:
        expected = {"schema", "kind", "selection", "current_identity"}
        if set(value) != expected:
            raise ValueError("invalid profile status spec")
        rel(value["selection"]); rel(value["current_identity"])
    return value


def seal(root, kind, target, source_revision):
    root = Path(root)
    if kind not in KINDS or not NAME.fullmatch(target) or not DIGEST.fullmatch(source_revision):
        raise ValueError("invalid performance bundle identity")
    if (root / "manifest.json").exists() or (root / "manifest.json").is_symlink():
        raise ValueError("refuse to overwrite an existing performance manifest")
    spec_schema(kind, json_read(root / "spec.json"))
    files = tree(root)
    if "spec.json" not in files:
        raise ValueError("performance bundle is missing spec.json")
    manifest = {"schema": 1, "kind": kind, "target": target, "source_revision": source_revision, "files": files}
    with (root / "manifest.json").open("x") as stream:
        json.dump(manifest, stream, indent=2, sort_keys=True)
        stream.write("\n")
    os.chmod(root / "manifest.json", 0o600)
    return manifest


def inspect(root, manifest_sha):
    root = Path(root)
    manifest_path = root / "manifest.json"
    manifest = json_read(manifest_path, 64 * 1024)
    actual, _ = sha(manifest_path)
    if not DIGEST.fullmatch(manifest_sha) or actual != manifest_sha:
        raise ValueError("performance manifest hash changed")
    if (manifest.get("schema") != 1 or manifest.get("kind") not in KINDS or not NAME.fullmatch(manifest.get("target", ""))
            or not DIGEST.fullmatch(manifest.get("source_revision", "")) or not isinstance(manifest.get("files"), dict)
            or not 1 <= len(manifest["files"]) <= MAX_FILES):
        raise ValueError("performance manifest is unsupported")
    observed = tree(root)
    if observed != manifest["files"]:
        raise ValueError("performance bundle file hashes or exact tree changed")
    expected_dirs = {"."}
    for name in manifest["files"]:
        parent = Path(name).parent
        while str(parent) != ".":
            expected_dirs.add(parent.as_posix())
            parent = parent.parent
    actual_dirs = set()
    for directory, _names, _files in os.walk(root, followlinks=False):
        path = Path(directory)
        info = path.stat(follow_symlinks=False)
        if path.is_symlink() or not stat.S_ISDIR(info.st_mode) or not private(info):
            raise ValueError("performance evidence directory is unsafe")
        actual_dirs.add(path.relative_to(root).as_posix())
    if actual_dirs != expected_dirs:
        raise ValueError("performance bundle directory tree changed")
    spec = spec_schema(manifest["kind"], json_read(child(root, "spec.json")))
    return manifest, spec, actual


def inspected_summary(manifest, digest):
    """Return a Bridge-safe result for a validated but unanalysed bundle.

    Inspection proves only the private bundle identity.  It deliberately does
    not interpret observations or establish a workload qualification.
    """
    return {
        "schema": 1,
        "evidence_id": "",
        "sha256": digest,
        "kind": manifest["kind"],
        "status": "sealed-unanalysed",
        "reason": "analysis_not_executed",
        "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "fields": [
            {"name": "bundle_kind", "value": manifest["kind"], "unit": "", "state": "observed"},
            {"name": "target", "value": manifest["target"], "unit": "", "state": "observed"},
            {"name": "source_revision", "value": manifest["source_revision"], "unit": "", "state": "observed"},
        ],
        "limitations": [
            "Owner export is required before analysis; sealed evidence is unqualified.",
            "Inspection does not deploy, restart, delete cache data, or qualify hardware.",
        ],
        "preconditions": {
            "bundle_manifest_sha256": digest,
            "target": manifest["target"],
            "source_revision": manifest["source_revision"],
        },
        "artifacts": [],
    }


def write_json(path, value):
    with Path(path).open("x") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")


def current_profile_identity(value, now=None):
    """Validate a fresh, boot-bound observation without inferring live state.

    The Bridge helper obtains this record from its scoped live adapters. The
    offline dispatcher can only report the record's timestamp, never turn it
    into a claim that the workstation remains unchanged after export.
    """
    required = {"schema", "kind", "status", "observed_at", "boot_id", "identity"}
    if not isinstance(value, dict) or set(value) != required:
        return None, "current identity observation is missing required fields"
    if (value.get("schema") != 1 or value.get("kind") != "workstation-performance-profile-identity-observation"
            or value.get("status") != "observed-not-qualified" or not isinstance(value.get("observed_at"), str)
            or not isinstance(value.get("boot_id"), str) or not BOOT_ID.fullmatch(value["boot_id"])):
        return None, "current identity observation is malformed"
    identity = value.get("identity")
    if (not isinstance(identity, dict) or set(identity) != set(performance_profiles.IDENTITY)
            or any(not isinstance(item, str) or not item for item in identity.values())):
        return None, "current identity observation is incomplete"
    try:
        observed_at = datetime.fromisoformat(value["observed_at"].replace("Z", "+00:00"))
    except ValueError:
        return None, "current identity observation timestamp is malformed"
    if observed_at.tzinfo is None:
        return None, "current identity observation timestamp lacks timezone"
    current = datetime.now(timezone.utc) if now is None else now
    if current.tzinfo is None:
        raise ValueError("profile identity validation requires an aware UTC time")
    age = (current - observed_at.astimezone(timezone.utc)).total_seconds()
    if age < 0 or age > MAX_PROFILE_IDENTITY_AGE_SECONDS:
        return None, "current identity observation is older than 15 minutes or from the future"
    return {"identity": identity, "observed_at": value["observed_at"], "boot_id": value["boot_id"]}, None


def profile_status(selection, current):
    """Return a wrapper-safe status while preserving selection_status semantics."""
    observed, reason = current_profile_identity(current)
    if observed is None:
        return {"schema": 1, "kind": "workstation-measured-profile-status", "status": "unknown",
                "reason": reason, "scope": "read-only identity observation; not live qualification"}
    result = performance_profiles.selection_status(selection, observed["identity"])
    result["observed_at"] = observed["observed_at"]
    result["boot_id"] = observed["boot_id"]
    result["scope"] = "read-only status as of observed_at; not live qualification"
    return result


def run_kind(root, kind, spec, output, owner, cache_root, reserve_mib):
    """Invoke one imported canonical method; no interpreter or shell dispatch."""
    if kind == "comparison":
        policy = json_read(child(root, spec["policy_json"]))
        result = performance_profiles.compare(child(root, spec["baseline_bundle"]), child(root, spec["candidate_bundle"]),
                                              spec["declared_variables"], policy)
        private = output / "comparison"
        private.mkdir(mode=0o700)
        write_json(private / "comparison.json", result)
        (private / "report.txt").write_text(performance_profiles.readable_report(result), encoding="utf-8")
        return result["status"], result["outcome"], [("recommendation", result["recommendation"], "", "observed"),
                                                        ("throughput_gain", str(result["minimum_case_throughput_gain_percent"]), "%", "unknown" if result["minimum_case_throughput_gain_percent"] is None else "observed")]
    if kind == "coding-eval":
        result = coding_eval.evaluate(child(root, spec["corpus"]), child(root, spec["responses"]))
        private = output / "coding-eval"
        private.mkdir(mode=0o700)
        write_json(private / "result.json", result)
        return result["status"], result["execution"], [("successes", str(result["successes"]), "tasks", "observed"),
                                                          ("unavailable", str(result["unavailable"]), "tasks", "observed")]
    if kind in ("loading", "queue", "warm-status"):
        observed = kernel_evidence(child(root, spec["evidence"]))
        private = output / kind
        private.mkdir(mode=0o700)
        if kind == "loading":
            result = serving_runtime.loading_plan(json_read(child(root, spec["deployment"])), observed, spec["threads"], spec["reserve_mib"], spec["per_thread_mib"])
            write_json(private / "plan.json", result)
            return result["status"], "plan only", [("maximum_threads_per_rank", str(result["maximum_threads_per_rank"]), "threads", "observed")]
        if kind == "queue":
            result = serving_runtime.queue_plan(json_read(child(root, spec["deployment"])), observed, spec["maximum_queued"])
            write_json(private / "plan.json", result)
            return result["status"], result["priority"]["status"], [("maximum_queued_requests", str(result["maximum_queued_requests"]), "requests", "observed")]
        warmup = json_read(child(root, spec["warmup"])) if spec["warmup"] else None
        lifecycle = json_read(child(root, spec["lifecycle"])) if spec["lifecycle"] else None
        result = serving_runtime.warm_status(observed, warmup, lifecycle)
        # A sealed input has no current Pod observation. Do not let an offline
        # review of retained evidence appear to be a live lifecycle answer.
        result["scope"] = "historical sealed-evidence warm-status analysis; not a current Pod status"
        write_json(private / "status.json", result)
        return result["status"], "historical: " + result.get("reason", ""), [("kubernetes_readiness", result.get("kubernetes_readiness", "unknown"), "", "observed")]
    if kind == "cache":
        private = output / "cache"
        if spec["mode"] == "inventory":
            if cache_root is None:
                raise ValueError("cache root is required from root-owned policy")
            result = inference_cache.inventory(cache_root, child(root, spec["registry"]), private)
            return result["status"], "inventory only; no deletion", [("available", str(result["filesystem"]["available_bytes"]), "bytes", "observed")]
        if reserve_mib is None:
            raise ValueError("cache free-space reserve is required from root-owned policy")
        code = inference_cache.prune_plan(child(root, spec["inventory"]), reserve_mib, private)
        result = json_read(private / "prune-plan.json")
        return result["status"], "plan only; no deletion", [("disposable", str(result["disposable_bytes"]), "bytes", "observed"),
                                                               ("result_code", str(code), "", "observed")]
    if kind == "profile-status":
        selection = json_read(child(root, spec["selection"]))
        current = json_read(child(root, spec["current_identity"]))
        result = profile_status(selection, current)
        private = output / "profile-status"
        private.mkdir(mode=0o700)
        write_json(private / "status.json", result)
        summary_status = "current-unqualified" if result["status"] == "selected-unqualified-current" else result["status"]
        return summary_status, result.get("reason", ""), [("observed_at", result.get("observed_at", "unknown"), "", "unknown" if result["status"] == "unknown" else "observed"),
                                                               ("boot_id", result.get("boot_id", "unknown"), "", "unknown" if result["status"] == "unknown" else "observed")]
    comparison = json_read(child(root, spec["comparison"]))
    previous = json_read(child(root, spec["previous_selection"])) if spec["previous_selection"] else None
    result = performance_profiles.select(comparison, owner, spec["candidate_id"], previous)
    private = output / "profile-selection"
    private.mkdir(mode=0o700)
    write_json(private / "selection.json", result)
    return result["status"], result["selection_reason"], [("selected_profile", result["selected_profile_id"], "", "observed")]


def output_artifacts(output, source_revision):
    entries = tree(output, output=True)
    artifacts = []
    for name, checksum in sorted(entries.items()):
        if name == "summary.json":
            continue
        path = output / name
        artifacts.append({"name": name, "sha256": checksum, "size": path.stat().st_size,
                          "source_revision": source_revision, "qualification": "analysis-output-not-qualified"})
    return artifacts


def make_private_output(root):
    """The new output tree is ours; normalize its artifacts before export."""
    for directory, names, files in os.walk(root, followlinks=False):
        path = Path(directory)
        info = path.stat(follow_symlinks=False)
        if path.is_symlink() or not stat.S_ISDIR(info.st_mode):
            raise ValueError("generated output directory is unsafe")
        os.chmod(path, 0o700)
        for name in names + files:
            if not NAME.fullmatch(name):
                raise ValueError("generated output name is unsafe")
        for name in files:
            artifact = path / name
            info = artifact.stat(follow_symlinks=False)
            if artifact.is_symlink() or not stat.S_ISREG(info.st_mode):
                raise ValueError("generated output artifact is unsafe")
            os.chmod(artifact, 0o600)


def failure_only_output(output, reason):
    """Retain one bounded failure report from an output tree created by this run.

    `output` is proven new by run().  Removing partial analysis artifacts here
    avoids exporting an incomplete candidate patch or report after a refusal.
    This is intentionally not a cleanup operation over caller-owned evidence.
    """
    for directory, names, files in os.walk(output, topdown=False, followlinks=False):
        path = Path(directory)
        for name in files:
            child = path / name
            info = child.stat(follow_symlinks=False)
            if child.is_symlink() or not stat.S_ISREG(info.st_mode):
                raise ValueError("generated failure output is unsafe")
            child.unlink()
        for name in names:
            child = path / name
            info = child.stat(follow_symlinks=False)
            if child.is_symlink() or not stat.S_ISDIR(info.st_mode):
                raise ValueError("generated failure output is unsafe")
            child.rmdir()
    write_json(Path(output) / "failure.json", {"schema": 1, "kind": "workstation-performance-analysis-failure",
                                                "status": "failed", "reason": reason[:300]})


def run(root, manifest_sha, output, target, source_revision, owner, cache_root=None, cache_free_reserve_mib=None):
    manifest, spec, actual = inspect(root, manifest_sha)
    if target != manifest["target"] or source_revision != manifest["source_revision"] or not NAME.fullmatch(owner):
        raise ValueError("performance execution identity differs from sealed bundle")
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError("performance output must be a new private directory")
    output.mkdir(mode=0o700)
    status = reason = ""
    fields = []
    try:
        status, reason, fields = run_kind(Path(root), manifest["kind"], spec, output, owner, cache_root, cache_free_reserve_mib)
    except Exception as error:
        status, reason = "failed", f"{type(error).__name__}: analysis input was refused or unavailable"
        # A failed result remains inspectable, but no exception text or source
        # content reaches the management API.
    if status == "failed":
        failure_only_output(output, reason)
        fields = []
    make_private_output(output)
    summary = {"schema": 1, "evidence_id": "", "sha256": actual, "kind": manifest["kind"], "status": status,
               "reason": reason[:300], "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
               "fields": [{"name": name, "value": str(value)[:256], "unit": unit, "state": state} for name, value, unit, state in fields][:40],
               "limitations": ["Installer analysis is plan/report only; it does not deploy, restart, delete cache data, or qualify hardware.",
                               "Source content, prompts, environment values and raw diagnostics are not exported in this summary."],
               "preconditions": {"bundle_manifest_sha256": actual, "target": target, "source_revision": source_revision},
               "artifacts": output_artifacts(output, source_revision)}
    write_json(output / "summary.json", summary)
    os.chmod(output / "summary.json", 0o600)
    return summary


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    seal_parser = sub.add_parser("seal")
    seal_parser.add_argument("directory"); seal_parser.add_argument("--kind", required=True)
    seal_parser.add_argument("--target", required=True); seal_parser.add_argument("--source-revision", required=True)
    inspect_parser = sub.add_parser("inspect")
    inspect_parser.add_argument("directory"); inspect_parser.add_argument("manifest_sha256")
    runner = sub.add_parser("run")
    runner.add_argument("directory"); runner.add_argument("manifest_sha256"); runner.add_argument("output")
    runner.add_argument("--target", required=True); runner.add_argument("--source-revision", required=True); runner.add_argument("--owner", required=True)
    runner.add_argument("--cache-root"); runner.add_argument("--cache-free-reserve-mib", type=int)
    args = parser.parse_args()
    if args.action == "seal":
        print(json.dumps(seal(args.directory, args.kind, args.target, args.source_revision), indent=2))
    elif args.action == "inspect":
        manifest, _spec, digest = inspect(args.directory, args.manifest_sha256)
        print(json.dumps(inspected_summary(manifest, digest), indent=2))
    else:
        print(json.dumps(run(args.directory, args.manifest_sha256, args.output, args.target, args.source_revision,
                             args.owner, args.cache_root, args.cache_free_reserve_mib), indent=2))


if __name__ == "__main__":
    main()
