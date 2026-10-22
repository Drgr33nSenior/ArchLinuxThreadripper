"""Package and select the source-bound M=1 GEMM profile. Never deploy."""

from __future__ import annotations

import argparse
import base64
import copy
import csv
import hashlib
import importlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import platform
import re
import sys
import tarfile
import tempfile


SITE = "opt/workstation/sglang/lib/python3.14/site-packages/"
SHARE = "opt/workstation/sglang/share/workstation/gemm-m1/"
RECORD = "sglang-0.5.18.dist-info/RECORD"
TABLE_DIR = "sglang/kernels/ops/quantization/configs/"
ROOT = Path(__file__).resolve().parents[2]
PROFILE_DIR = ROOT / "infrastructure/sglang/gemm-m1"
VISIBLE_ENV = ("HSA_OVERRIDE_GFX_VERSION", "HIP_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES",
               "CUDA_VISIBLE_DEVICES", "GPU_DEVICE_ORDINAL")
COMMUNICATION_ENV = ("NCCL_CUMEM_ENABLE", "NCCL_MIN_NCHANNELS", "NCCL_MAX_NCHANNELS",
                     "NCCL_NCHANNELS", "RCCL_ENABLE_CONTEXT_TRACKING", "HSA_FORCE_FINE_GRAIN_PCIE")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def fingerprint(value):
    return sha(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def bounded_bytes(path, maximum=1024 * 1024):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= maximum:
        raise ValueError("profile input must be a bounded regular file")
    return path.read_bytes()


def validate_tables(profile, directory):
    """The M=2 guard prevents nearest-M lookup from selecting the M=1 tile."""
    if (profile.get("schema") != 1 or profile.get("profile") != "gemm-m1"
            or profile.get("communication") != {} or profile.get("moe") != "incumbent"
            or [row.get("n") for row in profile.get("tables", [])] != [2048, 2560]
            or profile.get("m_grid") != [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096]):
        raise ValueError("unsupported GEMM profile")
    results = {}
    for row in profile["tables"]:
        expected = f"N={row['n']},K=2048,device_name=AMD_Radeon_AI_PRO_R9700,dtype=fp8_w8a8,block_shape=[128, 128].json"
        if row.get("name") != expected or row.get("k") != 2048:
            raise ValueError("unsupported GEMM shape or filename")
        raw = bounded_bytes(Path(directory) / row["name"])
        if sha(raw) != row["sha256"]:
            raise ValueError("GEMM table identity changed")
        values = json.loads(raw)
        if set(values) != {str(m) for m in profile["m_grid"]}:
            raise ValueError("GEMM table must retain the full incumbent M grid")
        if values["1"] != profile["single_token"] or any(values[str(m)] != profile["incumbent"] for m in profile["m_grid"] if m >= 2):
            raise ValueError("GEMM table changed M>=2 or the accepted M=1 tile")
        results[row["name"]] = raw
    return results


def parent_record(profile, record_path=None, layer_path=None):
    if (record_path is None) == (layer_path is None):
        raise ValueError("supply exactly one parent RECORD or retained sampler layer")
    if record_path is not None:
        raw = bounded_bytes(record_path)
    else:
        raw_layer = bounded_bytes(layer_path, 2 * 1024 * 1024)
        with tarfile.open(fileobj=io.BytesIO(raw_layer)) as archive:
            members = [item for item in archive.getmembers() if item.name == SITE + RECORD]
            if len(members) != 1 or not members[0].isfile() or members[0].size > 1024 * 1024:
                raise ValueError("parent layer must contain exactly one regular RECORD")
            raw = archive.extractfile(members[0]).read()
    if sha(raw) != profile["parent_record_sha256"]:
        raise ValueError("parent RECORD differs from the packaged sampler image")
    rows = list(csv.reader(io.StringIO(raw.decode())))
    sampler = [row for row in rows if row[0] == "sglang/srt/layers/sampler.py"]
    wanted = "sha256=" + base64.urlsafe_b64encode(bytes.fromhex(profile["sources"]["srt/layers/sampler.py"])).decode().rstrip("=")
    if len(sampler) != 1 or sampler[0][1] != wanted:
        raise ValueError("parent RECORD does not retain the singleton sampler")
    return raw, rows


def package(output, *, record_path=None, layer_path=None, profile_dir=PROFILE_DIR):
    """Create a deterministic OCI delta using the existing regctl layer interface."""
    profile = load(Path(profile_dir) / "profile.json")
    tables = validate_tables(profile, profile_dir)
    raw_record, rows = parent_record(profile, record_path, layer_path)
    names = {TABLE_DIR + name for name in tables}
    if any(row[0] in names for row in rows):
        raise ValueError("parent already contains a selected tuning table")
    extra = io.StringIO(newline="")
    writer = csv.writer(extra, lineterminator="\r\n")
    for name, data in sorted(tables.items()):
        checksum = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
        writer.writerow([TABLE_DIR + name, "sha256=" + checksum, len(data)])
    if not raw_record.endswith(b"\n"):
        raise ValueError("parent RECORD has no final newline")
    revised = raw_record + extra.getvalue().encode()
    helper = Path(__file__).read_bytes()
    files = {SITE + TABLE_DIR + name: data for name, data in tables.items()}
    files[SITE + RECORD] = revised
    files[SHARE + "profile.json"] = canonical(profile)
    files[SHARE + "gemm_profile.py"] = helper
    output = Path(output)
    output.mkdir(mode=0o700)
    layer = output / "gemm-layer.tar"
    with tarfile.open(layer, "x", format=tarfile.USTAR_FORMAT) as archive:
        for name, data in sorted(files.items()):
            member = tarfile.TarInfo(name)
            member.size, member.mode = len(data), 0o644
            member.uid = member.gid = member.mtime = 0
            archive.addfile(member, io.BytesIO(data))
    manifest = {"schema": 1, "profile": "gemm-m1", "status": "packaged-not-built-model-unqualified",
                "parent_image": profile["parent_image"], "parent_config_digest": profile["parent_config_digest"],
                "source_profile_sha256": sha(bounded_bytes(Path(profile_dir) / "profile.json")),
                "profile_sha256": sha(files[SHARE + "profile.json"]), "layer_sha256": sha(layer.read_bytes()),
                "record_sha256": sha(revised), "members": {name: sha(data) for name, data in sorted(files.items())},
                "sources_unchanged": profile["sources"], "runtime": profile["runtime"],
                "rollback": {"image": profile["parent_image"], "profile": "incumbent", "remove_table_mounts": True},
                "scope": "Only two table files, RECORD, profile and verifier. Preserve the parent image layers/configuration and sampler."}
    for name, value in (("layer.json", manifest), ("profile.json", profile)):
        with (output / name).open("xb") as stream:
            stream.write(canonical(value))
    # A Docker build is optional. The retained owner workflow adds gemm-layer.tar
    # to its verified local OCI parent with regctl; it does not pull or publish.
    dockerfile = (f"FROM {profile['parent_image']}\n"
                  "ADD gemm-layer.tar /\n"
                  f"LABEL workstation.ai.gemm.profile=gemm-m1 workstation.ai.gemm.layer.sha256={manifest['layer_sha256']}\n")
    (output / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    return manifest


def parse_args(values):
    """Require the frozen arguments; reject extra controls, aliases and duplicates."""
    profile = load(PROFILE_DIR / "profile.json") if (PROFILE_DIR / "profile.json").is_file() else load(Path(__file__).with_name("profile.json"))
    wanted, result, index = profile["settings"], {}, 0
    while index < len(values):
        option = values[index]
        index += 1
        if option not in wanted:
            raise ValueError("unsupported GEMM profile argument: " + option)
        if option in result:
            raise ValueError("duplicate GEMM profile argument")
        if wanted[option] is True:
            result[option] = True
        else:
            if index >= len(values) or values[index].startswith("--"):
                raise ValueError("missing GEMM profile argument")
            result[option] = values[index]
            index += 1
    return result


def mismatches(profile, observation):
    failures = []
    if not isinstance(observation, dict):
        return ["runtime observation unavailable"]
    if observation.get("runtime") != profile["runtime"]:
        failures.append("runtime/compiler package identity differs")
    if observation.get("sources") != profile["sources"]:
        failures.append("installed source identity differs")
    devices = observation.get("devices")
    if (not isinstance(devices, list) or any(not isinstance(row, dict) for row in devices)
            or sorted(devices, key=lambda row: str(row.get("uuid", ""))) != sorted(profile["devices"], key=lambda row: row["uuid"])):
        failures.append("GPU UUID/BDF/target identity differs")
    if observation.get("settings") != profile["settings"]:
        failures.append("model/precision/placement/serving scope differs")
    if observation.get("model_files") != profile["model_files"]:
        failures.append("model/tokenizer/template identity differs")
    if observation.get("visibility_overrides") != []:
        failures.append("visibility or architecture override present")
    if observation.get("communication_overrides") != []:
        failures.append("unreviewed communication setting present")
    if observation.get("environment") != profile["environment"]:
        failures.append("backend/offline environment differs")
    host = observation.get("host")
    if (not isinstance(host, dict) or not isinstance(host.get("kernel_release"), str) or not host["kernel_release"]
            or not re.fullmatch(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}", str(host.get("boot_id")))):
        failures.append("current-boot identity unavailable")
    return failures


def observe(settings):
    import torch
    import triton
    import sglang
    root = Path(sglang.__file__).resolve().parent
    profile = load(Path(__file__).with_name("profile.json")) if Path(__file__).with_name("profile.json").is_file() else load(PROFILE_DIR / "profile.json")
    devices = []
    for index in range(torch.cuda.device_count()):
        props = torch.cuda.get_device_properties(index)
        devices.append({"uuid": str(props.uuid), "bdf": f"{props.pci_domain_id:04x}:{props.pci_bus_id:02x}:{props.pci_device_id:02x}.0",
                        "gfx": props.gcnArchName.split(":", 1)[0], "name": props.name})
    distributions = []
    for name in ("triton", "pytorch-triton-rocm"):
        try:
            distributions.append((name, importlib.metadata.version(name)))
        except importlib.metadata.PackageNotFoundError:
            pass
    if len(distributions) != 1:
        raise ValueError("require one known Triton compiler distribution")
    distribution_name, distribution_version = distributions[0]
    model = Path(settings.get("--model-path", ""))
    if not model.is_absolute() or model.is_symlink() or model.resolve() != model or not model.is_dir():
        raise ValueError("model must be a canonical existing revision directory")
    model_files = {}
    for name in profile["model_files"]:
        path = model / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("model artifact is unavailable or symlinked: " + name)
        before = path.stat()
        with path.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        after = path.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError("model artifact changed during verification: " + name)
        model_files[name] = {"bytes": before.st_size, "sha256": checksum}
    return {"runtime": {"python_minor": f"{sys.version_info.major}.{sys.version_info.minor}",
                        "sglang": importlib.metadata.version("sglang"), "torch": torch.__version__,
                        "hip": torch.version.hip, "triton": triton.__version__,
                        "triton_distribution_name": distribution_name, "triton_distribution": distribution_version},
            "sources": {name: sha(bounded_bytes(root / name)) for name in profile["sources"]},
            "devices": devices, "settings": settings,
            "model_files": model_files,
            "environment": {name: ("0" if name == "SGLANG_USE_AITER" and os.environ.get(name) in ("0", "false") else os.environ.get(name))
                            for name in profile["environment"]},
            "communication_overrides": [name for name in COMMUNICATION_ENV if name in os.environ],
            "host": {"kernel_release": platform.release(), "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip()},
            "visibility_overrides": [name for name in VISIBLE_ENV if name in os.environ]}


def verify(profile, settings, *, observation=None, module=None):
    observation = observe(settings) if observation is None else observation
    failures = mismatches(profile, observation)
    receipt = {"schema": 1, "profile": "gemm-m1", "status": "incumbent-required" if failures else "effective-not-model-qualified",
               "failures": failures, "observation": observation, "profile_sha256": sha(canonical(profile)),
               "rollback_image": profile["parent_image"], "communication": {}, "moe": "incumbent", "loaded": []}
    if failures:
        return receipt
    module = module or importlib.import_module("sglang.kernels.ops.quantization.fp8_kernel")
    try:
        tables = validate_tables(profile, Path(module.__file__).resolve().parent / "configs")
        import torch
        for index in range(len(observation["devices"])):
            with torch.cuda.device(index):
                module.get_w8a8_block_fp8_configs.cache_clear()
                for row in profile["tables"]:
                    values = module.get_w8a8_block_fp8_configs(row["n"], row["k"], 128, 128)
                    expected = {int(key): value for key, value in json.loads(tables[row["name"]]).items()}
                    if values != expected:
                        raise ValueError("effective GEMM loader differs from the packaged table")
                    receipt["loaded"].append({"uuid": observation["devices"][index]["uuid"], "name": row["name"],
                                              "sha256": row["sha256"], "single_token": values[1], "larger_m": "incumbent"})
    except (OSError, ValueError, TypeError) as error:
        receipt["status"] = "incumbent-required"
        receipt["failure_stage"] = "effective-table-loading"
        receipt["failures"].append(str(error))
    return receipt


def plan(deployment, observation, built, selection="gemm-m1", profile_dir=PROFILE_DIR):
    """Emit a complete profile selection and an exact rollback, with no apply."""
    if selection not in ("gemm-m1", "incumbent"):
        raise ValueError("GEMM selection must be gemm-m1 or incumbent")
    profile = load(Path(profile_dir) / "profile.json")
    validate_tables(profile, profile_dir)
    spec = deployment["spec"]["template"]["spec"]
    indexes = [index for index, row in enumerate(spec["containers"]) if row.get("name") == "sglang"]
    if len(indexes) != 1:
        raise ValueError("expected one SGLang container")
    index = indexes[0]
    container = spec["containers"][index]
    pod_security, security = spec.get("securityContext", {}), container.get("securityContext", {})
    if (pod_security.get("runAsNonRoot") is not True or pod_security.get("runAsUser") != 1000
            or pod_security.get("runAsGroup") != 1000 or pod_security.get("seccompProfile") != {"type": "RuntimeDefault"}
            or security.get("readOnlyRootFilesystem") is not True or security.get("allowPrivilegeEscalation") is not False
            or security.get("capabilities", {}).get("drop") != ["ALL"] or spec.get("automountServiceAccountToken") is not False):
        raise ValueError("preserve the reviewed non-root, read-only, seccomp and credential controls")
    resources = container["resources"]
    if resources.get("requests") != resources.get("limits") or str(resources["limits"].get("amd.com/gpu")) != "2":
        raise ValueError("preserve a Guaranteed two-GPU allocation")
    failures = mismatches(profile, observation) if selection == "gemm-m1" else []
    entries = container.get("env", [])
    names = [entry.get("name") for entry in entries]
    if len(names) != len(set(names)) or any(not isinstance(name, str) or not name for name in names):
        raise ValueError("rendered environment has duplicate or invalid names")
    if selection == "gemm-m1":
        if container.get("envFrom"):
            failures.append("manifest envFrom requires a flattened reviewed configuration")
        if set(names) & set(VISIBLE_ENV + COMMUNICATION_ENV):
            failures.append("manifest contains visibility or communication overrides")
        for entry in entries:
            if entry["name"] in profile["environment"] and entry.get("value") not in (
                    profile["environment"][entry["name"]], "false" if entry["name"] == "SGLANG_USE_AITER" else "1"):
                failures.append("manifest backend/offline environment differs")
    if selection == "gemm-m1" and parse_args(container.get("args", [])) != profile["settings"]:
        failures.append("rendered server arguments differ from the observed profile")
    if not any(row.get("mountPath") == "/cache" for row in container.get("volumeMounts", [])):
        raise ValueError("reuse the existing /cache mount")
    if container.get("image") not in (profile["parent_image"], built.get("image")):
        raise ValueError("selection requires the incumbent or its reviewed GEMM child")
    if selection == "gemm-m1":
        if (built.get("parent_image") != profile["parent_image"] or built.get("profile_sha256") != sha(canonical(profile))
                or built.get("profile") != "gemm-m1" or not re.fullmatch(r"localhost/workstation/sglang@sha256:[a-f0-9]{64}", str(built.get("image")))
                or not re.fullmatch(r"sha256:[a-f0-9]{64}", str(built.get("config_digest")))):
            failures.append("built image/profile identity is unavailable or stale")
    selected = "incumbent" if failures else selection
    candidate = copy.deepcopy(spec)
    target = candidate["containers"][index]
    table_paths = {"/" + SITE + TABLE_DIR + row["name"] for row in profile["tables"]}
    removed = {row["name"] for row in target.get("volumeMounts", []) if row["mountPath"] in table_paths}
    target["volumeMounts"] = [row for row in target.get("volumeMounts", []) if row["name"] not in removed]
    if removed:
        candidate["volumes"] = [row for row in candidate.get("volumes", []) if row["name"] not in removed]
    key = fingerprint({"profile": selected, "identity": observation, "image": built.get("image") if selected == "gemm-m1" else profile["parent_image"]})
    env = {**profile["environment"], "WORKSTATION_GEMM_PROFILE": selected, "WORKSTATION_GEMM_CACHE_IDENTITY": key,
           "TRITON_CACHE_DIR": f"/cache/triton/workstation/{key}", "TORCHINDUCTOR_CACHE_DIR": f"/cache/torchinductor/workstation/{key}",
           "SGLANG_JIT_CACHE_DIR": f"/cache/sglang-jit/workstation/{key}", "XDG_CACHE_HOME": f"/cache/xdg/workstation/{key}"}
    target["env"] = [row for row in target.get("env", []) if row["name"] not in env and row["name"] not in VISIBLE_ENV + COMMUNICATION_ENV] + [{"name": name, "value": value} for name, value in sorted(env.items())]
    target["image"] = built["image"] if selected == "gemm-m1" else profile["parent_image"]
    target["imagePullPolicy"] = "Never"
    target["command"] = (["/opt/workstation/sglang/bin/python3", "-I", "-B", "/" + SHARE + "gemm_profile.py", "launch"]
                         if selected == "gemm-m1" else ["/opt/workstation/sglang/bin/python3", "-m", "sglang.launch_server"])
    # A second test restores the exact original template after this specific
    # candidate; it cannot erase a subsequent unrelated manifest change.
    patch = [{"op": "test", "path": "/spec/template/spec", "value": copy.deepcopy(spec)},
             {"op": "replace", "path": "/spec/template/spec", "value": candidate}]
    rollback = [{"op": "test", "path": "/spec/template/spec", "value": copy.deepcopy(candidate)},
                {"op": "replace", "path": "/spec/template/spec", "value": copy.deepcopy(spec)}]
    return {"schema": 1, "status": "plan-only-model-unqualified", "requested": selection, "selected": selected,
            "fallback_reasons": failures, "profile_sha256": sha(canonical(profile)), "cache_identity": key,
            "patch": patch, "rollback": rollback,
            "scope": "Runtime verification fails closed before launch. Unsupported/stale observations select the incumbent image without table mounts. No model qualification is implied."}


def main():
    os.umask(0o077)
    if len(sys.argv) > 1 and sys.argv[1] == "launch":
        profile = load(Path(__file__).with_name("profile.json"))
        try:
            receipt = verify(profile, parse_args(sys.argv[2:]))
        except (OSError, ValueError, RuntimeError, ImportError) as error:
            receipt = {"schema": 1, "profile": "gemm-m1", "status": "incumbent-required",
                       "failures": ["runtime verification could not complete"], "error_class": type(error).__name__,
                       "rollback_image": profile["parent_image"]}
        # /tmp survives container restarts. Retain each launch's verification
        # without overwriting old receipts or following pre-existing symlinks.
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir="/tmp",
                                         prefix="gemm-profile-receipt-", suffix=".json", delete=False) as stream:
            json.dump(receipt, stream, sort_keys=True, indent=2)
        print("GEMM profile receipt: " + stream.name, file=sys.stderr, flush=True)
        if receipt["failures"]:
            raise ValueError("GEMM identity changed; select incumbent image " + profile["parent_image"])
        os.execv(sys.executable, [sys.executable, "-m", "sglang.launch_server", *sys.argv[2:]])
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    p = sub.add_parser("package")
    p.add_argument("output")
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--parent-record")
    source.add_argument("--parent-layer")
    p = sub.add_parser("plan")
    for name in ("deployment", "observation", "built_image", "output"):
        p.add_argument(name)
    p.add_argument("--profile", choices=("gemm-m1", "incumbent"), default="gemm-m1")
    p = sub.add_parser("verify")
    for name in ("profile", "server_args", "output"):
        p.add_argument(name)
    args = parser.parse_args()
    if args.mode == "package":
        package(args.output, record_path=args.parent_record, layer_path=args.parent_layer)
    elif args.mode == "plan":
        result = plan(load(args.deployment), load(args.observation), load(args.built_image), args.profile)
        output = Path(args.output)
        output.mkdir(mode=0o700)
        for name, value in (("plan.json", result), ("selection.json", result["patch"]), ("rollback.json", result["rollback"])):
            with (output / name).open("xb") as stream:
                stream.write(canonical(value))
    else:
        profile = load(args.profile)
        try:
            result = verify(profile, parse_args(load(args.server_args)))
        except (OSError, ValueError, RuntimeError, ImportError) as error:
            result = {"schema": 1, "profile": "gemm-m1", "status": "incumbent-required",
                      "failures": ["runtime verification could not complete"], "error_class": type(error).__name__,
                      "rollback_image": profile["parent_image"]}
        with Path(args.output).open("xb") as stream:
            stream.write(canonical(result))
        if result["failures"]:
            raise ValueError("GEMM runtime identity changed; use the recorded incumbent selection")


if __name__ == "__main__":
    main()
