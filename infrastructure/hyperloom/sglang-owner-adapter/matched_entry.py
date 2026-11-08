"""Worker and parent entrypoints for the matched candidate/control profile (both starts).

Generated into the package as shared_entry.py by build_matched_profile. The builder
fills the immutable dependency pins and workload below. No framework is imported
here, and no hook is installed merely by loading this module.

Order is fixed by the reversed op227 role map: the candidate (owner arm mtp, physical
bf16) is start 1 and the control (owner arm control, physical kv4) is start 2. Both
starts are profiled with the same seven windows. Inside each worker the bundle scope
is already active (outer); this scope installs the graph-inventory producer and then
the shared scheduler observer, and restores them in reverse.
"""
from contextlib import contextmanager
import hashlib
import importlib.util
from importlib.machinery import SourceFileLoader
import os
from pathlib import Path
import sys

MODULE_PINS = None
CONFIG = None
ROLE_OF_ARM = {"mtp": "candidate", "control": "control"}


def modules():
    root = Path(__file__).resolve().parent
    for name, pin in MODULE_PINS.items():
        path = root / (name + ".py")
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != pin:
            raise ValueError("shared dependency source mismatch: " + name)
        module = sys.modules.get(name)
        if module is not None:
            spec = getattr(module, "__spec__", None)
            if (getattr(module, "__file__", None) != str(path) or spec is None
                    or spec.origin != str(path) or type(spec.loader) is not SourceFileLoader):
                raise ValueError("foreign shared dependency: " + name)
            continue
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try: spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    return sys.modules["shared_owner"]


def identity(manifest, worker):
    owner = modules(); core = owner.core
    core.need(manifest["arm"] in ROLE_OF_ARM, "matched owner arm")
    for key in ("arm", "session_sha256", "source_manifest_sha256"):
        core.need(worker[key] == manifest[key], "worker manifest join")
    role = ROLE_OF_ARM[manifest["arm"]]
    row = dict(schema=2, rank=worker["rank"], pid=worker["pid"], start=CONFIG["order"].index(role) + 1,
               nonce=CONFIG["nonces"][manifest["arm"]], control=role == "control", power_cap_watts=235,
               session_sha256=manifest["session_sha256"], source_manifest_sha256=manifest["source_manifest_sha256"],
               worker_identity_sha256=worker["identity_sha256"], role=role,
               members=list(core.MATCHED_ROLES[role]), profiled=True)
    # One authority: identity_check enforces role, members and the frozen start order.
    core.identity_check(row)
    return row


def directory(manifest):
    return Path(manifest["evidence_dir"]).parent / "shared-diagnostic"


def source_descriptors(manifest):
    return {key: {k: manifest["sources"][role][k] for k in ("path", "sha256")}
            for key, role in (("scheduler", "scheduler"), ("stage", "shared_stage"), ("nvtx", "shared_nvtx"))}


def bundle_descriptor(manifest):
    return {k: manifest["sources"]["shared_bundle_hooks"][k] for k in ("path", "sha256")}


BACKEND_MODULE = "sglang.srt.model_executor.runner_backend.full_cuda_graph_backend"


def backend_descriptors(manifest):
    """The producer's callable descriptor: exactly path/sha256, after an explicit module identity check.

    The worker manifest carries {module, path, sha256}; shared_runtime_binding._source accepts only
    {path, sha256}, exactly as source_descriptors() shapes the scheduler/stage/nvtx callables.
    """
    source = manifest["sources"]["backend"]
    modules().core.need(type(source) is dict and source.get("module") == BACKEND_MODULE,
                        "graph backend descriptor module")
    return {"backend": {k: source[k] for k in ("path", "sha256")}}


@contextmanager
def worker(manifest, worker_record, target, gpu_id, load_helper):
    owner = modules(); core = owner.core
    ident = identity(manifest, worker_record)
    core.need(ident["pid"] == os.getpid() and gpu_id == ident["rank"], "live worker identity")
    torch = sys.modules.get("torch")
    core.need(torch is not None, "worker Torch already loaded")
    root = directory(manifest); owner._root(root)
    backend = sys.modules["shared_capture"].KinetoBackend(torch,
        f"/mnt/shared-trace-raw-rank-{ident['rank']}", root, ident)
    def ready(instance):
        # The native Scheduler selected the device before entering its loop.
        gpu = load_helper(manifest["preweight"]["gpu_identity"])
        devices, _ = gpu.physical_devices(torch,
            {bdf: row["uuid"] for bdf, row in manifest["expected_devices"].items()},
            manifest["preweight"]["deadline_monotonic"])
        owner.publish_device(root, ident, torch, devices, gpu_id)
    # Installed before the target builds any graph; raises before model start on mismatch.
    inventory = sys.modules["graph_inventory"].install(
        torch, ident, CONFIG["graph_inventory"], backend_descriptors(manifest), lambda: backend.window,
        binding=sys.modules["shared_runtime_binding"])
    primary = None
    try:
        with owner.target_worker_scope(root, ident, CONFIG["menu"], target, source_descriptors(manifest),
                                       backend, instance_ready=ready, bundle_hooks=bundle_descriptor(manifest),
                                       inventory=inventory):
            yield
    except BaseException as error:
        primary = error
        raise
    finally:
        # Reverse of installation: the shared scope has restored the scheduler hooks.
        # A removal failure never replaces the model's own interrupt or exception.
        try: inventory.restore()
        except BaseException:
            if primary is None: raise


def before_start(material):
    owner = modules()
    root = directory(material["entry_binding"])
    root.mkdir(mode=0o700)
    owner._root(root)


def parent_barrier(material, gate_workers, summary, deadline):
    """After the existing complete-summary validator, before server teardown."""
    owner = modules(); core = owner.core
    binding = material["entry_binding"]; root = directory(binding)
    core.need(type(gate_workers) is list and len(gate_workers) == 2,
              "two validated gate workers")
    workers = []
    for rank, compact in enumerate(gate_workers):
        core.need(type(compact) is dict and set(compact) == {
            "rank", "pid", "start_ticks", "boot_id", "identity_sha256"}
            and compact["rank"] == rank, "ordered compact gate worker")
        full = core._json(core._file(material["native_dir"], f"worker-rank-{rank}.json", 1 << 20))
        core.need(type(full) is dict and full.get("schema") == 1
                  and all(full.get(k) == v for k, v in compact.items())
                  and all(full.get(k) == binding[k] for k in (
                      "mode", "arm", "session_sha256", "spec_sha256", "source_manifest_sha256"))
                  and type(full.get("sources")) is dict and full["sources"] == binding["sources"]
                  and core.sha(core.canonical(full["sources"])[:-1]) == binding["source_manifest_sha256"]
                  and core.sha(core.canonical({k: v for k, v in full.items()
                                               if k != "identity_sha256"})) == compact["identity_sha256"],
                  "full worker / validated gate identity")
        workers.append(full)
    identities = [identity(binding, worker) for worker in workers]
    core.need([i["rank"] for i in identities] == [0, 1], "ordered authenticated workers")
    raw = core._file(material["client_dir"] / "paired", "RUN-SUMMARY.json", 8 << 20)
    block = summary["blocks"][0]
    core.need(block["name"] == "paired" and len(raw) == block["summary_bytes"], "paired summary size")
    http_pin = owner.publish_http(root, identities[0], CONFIG["menu"], raw, block["summary_sha256"])
    close_pins = owner.wait_worker_close(root, identities, CONFIG["menu"], deadline)
    devices = []
    for ident in identities:
        core.need(core.profiled(ident), "matched starts are profiled")
        name = f"SHARED-DEVICE-rank-{ident['rank']}.json"
        raw_device = core._file(root, name, owner.RECEIPT_BYTES)
        devices.append(dict(file=name, sha256=core.sha(raw_device), bytes=len(raw_device)))
    return dict(schema=1, identities=identities, close_pins=close_pins, http_pin=http_pin,
                device_pins=devices, summary_sha256=block["summary_sha256"],
                trust_root="parent-barrier-before-teardown", diagnostic_success=False)


def replay(directory_root, binding, workers, result, client_root):
    """Called by the existing HTTP reader after its identity and wire checks."""
    owner = modules(); core = owner.core
    root = Path(directory_root) / "shared-diagnostic"
    if result["status"] != "HTTP_ARM_COMPLETE":
        return owner.failed_start(root)
    receipt = result["shared_diagnostic"]
    core.need(type(receipt) is dict and set(receipt) == {
        "schema", "identities", "close_pins", "http_pin", "device_pins", "summary_sha256",
        "trust_root", "diagnostic_success"} and receipt["schema"] == 1
        and receipt["trust_root"] == "parent-barrier-before-teardown"
        and receipt["diagnostic_success"] is False, "parent barrier receipt")
    identities = [identity(binding, worker) for worker in workers]
    core.need(receipt["identities"] == identities, "barrier/live-worker join")
    raw = core._file(Path(client_root) / "paired", "RUN-SUMMARY.json", 8 << 20)
    core.need(receipt["summary_sha256"] == result["client_summary"]["blocks"][0]["summary_sha256"],
              "barrier/result paired summary")
    mounts = [dict(path=f"/mnt/shared-trace-raw-rank-{ident['rank']}", fstype="tmpfs",
                   capacity_bytes=32 << 20, verified=True) for ident in identities]
    checked = owner.read_start(root, identities, CONFIG["menu"], receipt["close_pins"], receipt["http_pin"],
                               raw, receipt["summary_sha256"], raw_mounts=mounts, device_pins=receipt["device_pins"])
    compact = dict(status=checked["status"], diagnostic_success=False, gpu_authority=False,
        capture_statuses=[c["status"] for c in checked["captures"]],
        inventory_statuses=[i["status"] for i in checked["inventories"]],
        core_summary_sha256=[r["summary_sha256"] for r in checked["records"]],
        http_receipt_sha256=receipt["http_pin"]["sha256"],
        trace_windows_joined=len(checked["rank_comparisons"]),
        retained_artifact_bytes=checked["transport_index"]["bytes"],
        detailed_readout_sha256=core.sha(core.canonical(checked)),
        cross_start_comparison="OFFLINE_AFTER_RELEASE", graph_completion="OFFLINE_INVENTORY_JOIN_ONLY")
    core.need(len(core.canonical(compact)) <= 4096, "shared replay receipt bound")
    return compact
