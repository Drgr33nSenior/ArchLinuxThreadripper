"""Owner-facing publication, collection and replay for the shared diagnostic.

Call after the existing owner authenticates source, worker, HTTP and release
evidence. These functions confer no release, quality or execution authority.
No hash is substituted for a missing producer digest.
"""
from contextlib import contextmanager
from pathlib import Path
import re
import json
import time
import os
import inspect

import shared_diagnostic as core
import shared_readout as readout
from shared_capture import CaptureObserver
from shared_runtime_binding import authenticate, authenticate_composed_target, authenticate_target
from shared_trace_io import WINDOWS, PREFILL_LIMIT, DECODE_LIMIT, RANK_TRACE_LIMIT

RECEIPT_BYTES = 4096
OTHER_BYTES = 256 << 10
START_BYTES = 15 << 20
MAX_MEMBERS = 63
# Schema-2 capture-time graph inventory, per rank (graph_inventory.INVENTORY_BYTES).
INVENTORY_BYTES = 192 << 10


def _root(directory):
    path = Path(directory)
    core.need(path.is_absolute() and path.resolve() == path and path.is_dir(),
              "canonical shared output root")
    return path


def _publish(directory, name, value):
    raw = core.canonical(value)
    core.need(len(raw) <= RECEIPT_BYTES, "owner receipt byte bound")
    core.exclusive(_root(directory) / name, raw)
    return {"file": name, "sha256": core.sha(raw), "bytes": len(raw)}


def _inventory_receipt(value, rank):
    """A schema-2 close receipt carries the producer pin or its bounded export failure type."""
    pin = type(value) is dict and set(value) == {"file", "sha256", "bytes"}
    core.need((pin and value["file"] == f"GRAPH-INVENTORY-rank-{rank}.json" and type(value["bytes"]) is int
               and 0 < value["bytes"] <= INVENTORY_BYTES and core.hex64(value["sha256"]) == value["sha256"])
              or (type(value) is dict and set(value) == {"status", "error"} and value["status"] == "EXPORT_FAILED"
                  and type(value["error"]) is str
                  and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", value["error"]) is not None),
              "graph inventory close receipt")
    return value


def _with_inventory(receipt, inventory, observer):
    """Export once at worker close; an export failure never hides the capture receipt."""
    if receipt is None or observer.identity["schema"] != 2:
        return receipt
    if inventory is None:
        value = {"status": "EXPORT_FAILED", "error": "NoInventoryProducer"}
    else:
        try: value = inventory.export(str(observer.journal.directory))
        except BaseException as error: value = {"status": "EXPORT_FAILED", "error": type(error).__name__[:80]}
    return dict(receipt, graph_inventory=value)


def _close_fields(identity):
    fields = {"schema", "identity", "core_summary_sha256", "capture_sha256", "capture_status", "capture_write_error"}
    return fields | {"graph_inventory"} if identity["schema"] == 2 else fields


def publish_worker_close(directory, identity, receipt):
    """Publish the receipt returned by restore, including incomplete captures."""
    core.identity_check(identity)
    core.need(type(receipt) is dict and set(receipt) == _close_fields(identity), "returned worker receipt fields")
    if identity["schema"] == 2: _inventory_receipt(receipt["graph_inventory"], identity["rank"])
    core.need(type(receipt["schema"]) is int and receipt["schema"] == 1
              and receipt["identity"] == identity, "returned worker identity")
    core.hex64(receipt["core_summary_sha256"])
    if receipt["capture_sha256"] is not None:
        core.hex64(receipt["capture_sha256"])
    core.need(receipt["capture_status"] in {"NOT_PROFILED", "CAPTURE_EXPORT_COMPLETE", "INCOMPLETE"},
              "capture disposition")
    core.need(receipt["capture_write_error"] is None or
              (type(receipt["capture_write_error"]) is str and
               re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", receipt["capture_write_error"])),
              "bounded capture failure type")
    if receipt["capture_write_error"] is not None or receipt["capture_sha256"] is None:
        core.need(receipt["capture_status"] == "INCOMPLETE", "missing capture cannot succeed")
    return _publish(directory, f"SHARED-CLOSE-rank-{identity['rank']}.json", receipt)


@contextmanager
def worker_scope(observer, scheduler_type, expected_originals, *, instance_ready=None, inventory=None):
    """Publish at menu completion or callback failure, before owner termination."""
    core.need("run" not in observer.__dict__ and "process" not in observer.__dict__,
              "observer callbacks already overridden")
    try:
        restore = core.install(observer, scheduler_type, expected_originals)
    except BaseException:
        if observer.close_receipt is not None:
            try: publish_worker_close(observer.journal.directory, observer.identity,
                                       _with_inventory(observer.close_receipt, inventory, observer))
            except BaseException: pass
        raise
    attempted = False; failure = None; pin = None
    instance = None
    installed = {name: scheduler_type.__dict__[name] for name in expected_originals}
    def check_instance(value):
        nonlocal instance
        core.need(type(value) is scheduler_type, "actual Scheduler instance type")
        if instance is None:
            core.need(all(inspect.getattr_static(value, name) is method for name, method in installed.items()),
                      "actual Scheduler instance hooks")
            instance = value
            if instance_ready is not None: instance_ready(value)
        else:
            core.need(value is instance, "multiple Scheduler instances")
    observed_run = observer.run; observed_process = observer.process
    def finish():
        nonlocal attempted, failure, pin
        if attempted:
            if failure is not None: raise failure
            return pin
        attempted = True
        try:
            try:
                receipt = restore()
            except BaseException:
                # A foreign hook can make restore raise after close completed.
                receipt = observer.close_receipt
                if receipt is not None:
                    try: publish_worker_close(observer.journal.directory, observer.identity, _with_inventory(receipt, inventory, observer))
                    except BaseException: pass
                raise
            pin = publish_worker_close(observer.journal.directory, observer.identity, _with_inventory(receipt, inventory, observer))
            observer.owner_close_pin = pin
            return pin
        except BaseException as error:
            failure = error
            raise
        finally:
            observer.__dict__.pop("run", None); observer.__dict__.pop("process", None)
    def run(*args, **kwargs):
        try:
            check_instance(args[1])
            result = observed_run(*args, **kwargs)
            if observer.disabled: finish()
            return result
        except BaseException:
            try: finish()
            except BaseException: pass
            raise
    def process(*args, **kwargs):
        try:
            check_instance(args[1])
            result = observed_process(*args, **kwargs)
            if observer.disabled or len(observer.finished) == len(observer.menu): finish()
            return result
        except BaseException:
            try: finish()
            except BaseException: pass
            raise
    observer.run = run; observer.process = process
    try:
        yield observer
    except BaseException:
        try: finish()
        except BaseException: pass
        raise
    else:
        finish()


def publish_http(directory, identity, menu, summary_raw, validated_summary_sha256):
    """Bind bytes returned by the existing HTTP reader to this actual start.

    The supplied digest must come from that reader's authenticated result. This
    helper does not independently grade or validate the HTTP response tree.
    """
    core.identity_check(identity); core.menu_check(menu)
    core.need(type(summary_raw) is bytes and len(summary_raw) <= 16 << 20
              and core.sha(summary_raw) == core.hex64(validated_summary_sha256),
              "validated HTTP summary identity")
    return _publish(directory, "SHARED-HTTP-START.json", {
        "schema": 1, "start_binding": readout.start_binding(identity),
        "menu_sha256": core.sha(core.canonical(menu)), "summary_sha256": validated_summary_sha256})


def wait_worker_close(directory, identities, menu, deadline, *, clock=time.monotonic, sleep=time.sleep):
    """Parent callback barrier after HTTP completion and before server teardown.

    At most five seconds, charged inside the existing owner deadline. This only
    waits for publication; it starts no work and never retries a model request.
    Full semantic replay still follows collection and independent release.
    """
    root = _root(directory); core.menu_check(menu)
    core.need(len(identities) == 2, "both worker identities")
    for rank, identity in enumerate(identities):
        core.identity_check(identity); core.need(identity["rank"] == rank, "ordered worker identities")
    core.need(readout.start_binding(identities[0]) == readout.start_binding(identities[1]), "same start workers")
    now = clock()
    core.need(type(deadline) in (int, float) and now < deadline < float("inf"), "finite close publication deadline")
    end = min(deadline, now + 5.0); pins = {}
    while True:
        for rank, identity in enumerate(identities):
            if rank in pins: continue
            name = f"SHARED-CLOSE-rank-{rank}.json"
            if not (root / name).exists(): continue
            raw = core._file(root, name, RECEIPT_BYTES)
            try: receipt = core._json(raw)
            except json.JSONDecodeError: continue  # Writer may be inside its exclusive write.
            core.need(type(receipt) is dict and receipt.get("identity") == identity,
                      "published worker identity")
            expected_status = "CAPTURE_EXPORT_COMPLETE" if core.profiled(identity) else "NOT_PROFILED"
            core.need(receipt.get("capture_status") == expected_status and receipt.get("capture_write_error") is None,
                      "worker observation failed before teardown")
            summary_raw = core._file(root, f"OBS-rank-{rank}.json", core.REPORT_BYTES)
            core.need(core.sha(summary_raw) == core.hex64(receipt["core_summary_sha256"]), "published core pin")
            summary = core._json(summary_raw)
            core.need(summary["identity"] == identity and summary["menu_sha256"] == core.sha(core.canonical(menu))
                      and summary["status"] == "COMPLETE" and summary["restored"] is True,
                      "complete observation before teardown")
            capture_raw = core._file(root, f"CAPTURE-rank-{rank}.json", core.REPORT_BYTES)
            core.need(core.sha(capture_raw) == core.hex64(receipt["capture_sha256"]), "published capture pin")
            pins[rank] = {"file": name, "sha256": core.sha(raw), "bytes": len(raw)}
        if len(pins) == 2:
            core.need(clock() < end, "close publication deadline")
            return [pins[rank] for rank in (0, 1)]
        remaining = end - clock()
        core.need(remaining > 0, "close publication deadline")
        sleep(min(0.02, remaining))


@contextmanager
def authenticated_worker_scope(directory, identity, menu, scheduler_type, sources, backend=None):
    """Entry seam for the source-pinned worker, installed before Scheduler init.

    The worker supplies its already authenticated identity and menu. A profiled
    start also supplies KinetoBackend, which checks the dedicated raw mount.
    No production entrypoint is redirected by importing this module.
    """
    originals = authenticate(scheduler_type, sources)
    observer = CaptureObserver(directory, identity, menu, backend)
    with worker_scope(observer, scheduler_type, originals):
        yield observer


@contextmanager
def target_worker_scope(directory, identity, menu, target, sources, backend=None, *, instance_ready=None,
                        bundle_hooks=None, inventory=None):
    """Production boundary: wrap the worker's actual target before construction.

    With bundle_hooks, the candidate bundle scope must already be active (outer)
    and this scope nests inside it, so restoration returns the bundle hook first.
    """
    if bundle_hooks is None:
        scheduler_type, originals = authenticate_target(target, sources)
    else:
        scheduler_type, originals, _ = authenticate_composed_target(target, sources, bundle_hooks)
    observer = CaptureObserver(directory, identity, menu, backend)
    with worker_scope(observer, scheduler_type, originals, instance_ready=instance_ready, inventory=inventory):
        yield observer


def publish_device(directory, identity, torch, authenticated_devices, gpu_id):
    """Bind the current worker device to the owner's existing physical-device check.

    Call at the first native callback, after Scheduler sets its CUDA device and
    before any diagnostic window. Identity queries read no model tensors.
    """
    core.identity_check(identity)
    core.need(core.profiled(identity) and type(gpu_id) is int and gpu_id == identity["rank"],
              "profile worker GPU/rank mapping")
    core.need(type(authenticated_devices) is list and len(authenticated_devices) == 2
              and [row["index"] for row in authenticated_devices] == [0, 1]
              and all(type(row["index"]) is int for row in authenticated_devices)
              and len({row["uuid"] for row in authenticated_devices}) == 2,
              "authenticated physical devices")
    core.need(torch.cuda.current_device() == gpu_id and not torch.cuda.is_current_stream_capturing(),
              "worker device boundary")
    uuid = str(torch.cuda.get_device_properties(gpu_id).uuid)
    core.need(0 < len(uuid) <= 128 and uuid == authenticated_devices[gpu_id]["uuid"],
              "worker physical UUID")
    return _publish(directory, f"SHARED-DEVICE-rank-{identity['rank']}.json", {
        "rank": identity["rank"], "pid": identity["pid"],
        "worker_identity_sha256": identity["worker_identity_sha256"],
        "device_uuid": uuid, "trace_device_index": gpu_id})


def _classify(name):
    """Closed file vocabulary; unrecognised diagnostic output is a refusal."""
    if name == "SHARED-HTTP-START.json":
        return None, "other", RECEIPT_BYTES
    for rank in (0, 1):
        if name == f"OBS-rank-{rank}.json": return rank, "journal", core.REPORT_BYTES
        if re.fullmatch(rf"OBS-rank-{rank}-chunk-00[0-3]\.jsonl", name):
            return rank, "journal", core.CHUNK_BYTES
        if name == f"CAPTURE-rank-{rank}.json": return rank, "other", core.REPORT_BYTES
        if name == f"GRAPH-INVENTORY-rank-{rank}.json": return rank, "inventory", INVENTORY_BYTES
        if name in {f"SHARED-CLOSE-rank-{rank}.json", f"SHARED-DEVICE-rank-{rank}.json"}:
            return rank, "other", RECEIPT_BYTES
        for window in WINDOWS:
            stem = f"TRACE-rank-{rank}-{window}"
            if name in {stem + ".attempt.json", stem + ".export.json"}:
                return rank, "other", RECEIPT_BYTES
            if name == stem + ".json.gz":
                return rank, "trace", PREFILL_LIMIT if window.endswith("prefill") else DECODE_LIMIT
    core.need(False, "unexpected diagnostic artifact: " + name[:100])


def collect(directory):
    """Bound the files passed to the existing collector, even after a failed run.

    This index authenticates transport only; read_start requires independently
    pinned producer receipts and replays the semantic readers afterwards.
    """
    root = _root(directory); rows = {}; used = {(r, k): 0 for r in (0, 1) for k in ("journal", "other", "trace", "inventory")}
    names = []
    with os.scandir(root) as entries:
        for entry in entries:
            core.need(len(names) < MAX_MEMBERS, "shared artifact member bound")
            _classify(entry.name)
            names.append(entry.name)
    for name in sorted(names):
        rank, kind, limit = _classify(name)
        raw = core._file(root, name, limit)
        if rank is None:
            # Charge the common HTTP receipt to both reserves conservatively.
            for r in (0, 1): used[r, "other"] += len(raw)
        else: used[rank, kind] += len(raw)
        rows[name] = {"bytes": len(raw), "sha256": core.sha(raw)}
    for rank in (0, 1):
        core.need(used[rank, "journal"] <= core.RANK_BYTES and used[rank, "other"] <= OTHER_BYTES
                  and used[rank, "trace"] <= RANK_TRACE_LIMIT and used[rank, "inventory"] <= INVENTORY_BYTES,
                  "shared partition bound")
    total = sum(r["bytes"] for r in rows.values())
    core.need(total <= START_BYTES, "shared start byte bound")
    return {"schema": 1, "files": rows, "bytes": total,
            "partitions": {str(r): {k: used[r, k] for k in ("journal", "other", "trace", "inventory")} for r in (0, 1)},
            "semantic_validation": False, "independent_release": False, "gpu_authority": False}


def failed_start(directory):
    """Retain 0/1/2-rank artifacts without calling any success reader.

    Existing protected closeout must still independently establish release.
    Malformed files are retained as bounded bytes; unknown or unsafe files refuse.
    """
    root = Path(directory)
    if not root.exists():
        core.need(not root.is_symlink() and root.is_absolute() and root.resolve() == root,
                  "canonical missing shared output root")
        index = None; ranks = []
    else:
        index = collect(root)
        ranks = [r for r in (0, 1) if f"SHARED-CLOSE-rank-{r}.json" in index["files"]]
    return {"status": "INCOMPLETE_RETAINED", "transport_index": index,
            "present_close_ranks": ranks, "semantic_validation": False,
            "independent_release": False, "grading_performed": False, "automatic_retry": False}


def _pinned(root, name, pin, limit):
    core.need(set(pin) == {"file", "sha256", "bytes"} and pin["file"] == name, "owner artifact pin")
    raw = core._file(root, name, limit)
    core.need(type(pin["bytes"]) is int and len(raw) == pin["bytes"]
              and core.sha(raw) == core.hex64(pin["sha256"]), "owner artifact digest")
    return raw


def _read_inventory(root, ident, value, expected_files):
    """Join the pinned inventory to its worker; semantic graph joins stay offline."""
    _inventory_receipt(value, ident["rank"])
    if "status" in value:
        return {"status": "EXPORT_FAILED", "error": value["error"], "completion_established": False}
    raw = _pinned(root, value["file"], value, INVENTORY_BYTES); expected_files.add(value["file"])
    doc = core._json(raw)
    core.need(type(doc) is dict and doc.get("schema") == 1 and doc.get("status") in ("COMPLETE", "INCOMPLETE")
              and all(doc.get(k) == ident[k] for k in ("rank", "pid", "nonce", "start", "role"))
              and doc.get("host_metadata_only") is True and doc.get("gpu_tensor_reads") == 0
              and doc.get("keep_graph_changed") is False and doc.get("completion_claimed") is False,
              "graph inventory worker join")
    return {"status": doc["status"], "file": value["file"], "sha256": value["sha256"], "bytes": value["bytes"],
            "completion_established": False}


def read_start(directory, identities, menu, close_pins, http_pin, summary_raw, summary_sha256,
               *, raw_mounts, device_pins):
    """Replay one collected start; upstream owner validators remain mandatory."""
    root = _root(directory); index = collect(root)
    core.need(len(identities) == len(close_pins) == len(raw_mounts) == len(device_pins) == 2,
              "both rank inputs")
    for rank, ident in enumerate(identities):
        core.identity_check(ident); core.need(ident["rank"] == rank, "ordered worker ranks")
    core.need(readout.start_binding(identities[0]) == readout.start_binding(identities[1]), "same actual start")
    http_raw = _pinned(root, "SHARED-HTTP-START.json", http_pin, RECEIPT_BYTES)
    expected_files = {"SHARED-HTTP-START.json"}
    records = []; captures = []; http = []; inventories = []
    for rank, ident in enumerate(identities):
        close = core._json(_pinned(root, f"SHARED-CLOSE-rank-{rank}.json", close_pins[rank], RECEIPT_BYTES))
        core.need(set(close) == _close_fields(ident)
                  and type(close["schema"]) is int and close["schema"] == 1
                  and close["identity"] == ident and close["capture_write_error"] is None
                  and close["capture_status"] == ("CAPTURE_EXPORT_COMPLETE" if core.profiled(ident) else "NOT_PROFILED"),
                  "complete producer close receipt")
        record = core.read_recording(root, ident, menu, core.hex64(close["core_summary_sha256"]))
        core.need(record["status"] == "COMPLETE", "complete producer record")
        summary = core._json(core._file(root, f"OBS-rank-{rank}.json", core.REPORT_BYTES))
        core.need(core.sha(core.canonical(summary)) == close["core_summary_sha256"], "canonical core summary")
        expected_files.update(part["file"] for part in summary["parts"])
        expected_files.update((f"OBS-rank-{rank}.json", f"CAPTURE-rank-{rank}.json", f"SHARED-CLOSE-rank-{rank}.json"))
        device_raw = None; device_sha = None
        if core.profiled(ident):
            expected_files.add(f"SHARED-DEVICE-rank-{rank}.json")
            for window in WINDOWS:
                expected_files.update(f"TRACE-rank-{rank}-{window}" + suffix
                                      for suffix in (".attempt.json", ".export.json", ".json.gz"))
            device_raw = _pinned(root, f"SHARED-DEVICE-rank-{rank}.json", device_pins[rank], RECEIPT_BYTES)
            device_sha = device_pins[rank]["sha256"]
        else:
            core.need(device_pins[rank] is None and raw_mounts[rank] is None, "unprofiled start resources")
        captures.append(readout.read_capture(root, record, menu, core.hex64(close["capture_sha256"]),
                                             raw_mounts[rank], device_raw, device_sha))
        http.append(readout.join_http(record, menu, summary_raw, summary_sha256, http_raw, http_pin["sha256"]))
        records.append(record)
        if ident["schema"] == 2:
            inventories.append(_read_inventory(root, ident, close["graph_inventory"], expected_files))
    core.need(core.join_ranks(*records)["status"] == "COMPLETE", "rank inventory agreement")
    core.need(set(index["files"]) == expected_files, "exact shared start file set")
    comparisons = []
    if core.profiled(identities[0]):
        comparisons = [readout.compare_rank_traces(*captures, window) for window in WINDOWS]
    return {"status": "OWNER_OBSERVATIONS_REPLAYED", "records": records, "captures": captures,
            "http": http, "rank_comparisons": comparisons, "transport_index": index, "inventories": inventories,
            "upstream_source_wire_quality_release_required": True, "diagnostic_success": False,
            "gpu_authority": False}
