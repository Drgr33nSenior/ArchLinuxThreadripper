"""Explicit, disruptive SQLite/PVC capture; restore only to a fresh staging tree.

No live restore operation exists here. Application writers are stopped before
capture. Optional local-first capture ends at the independent encrypted SSD;
explicit off-site replication never holds the workstation session lock.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import sqlite3
import stat
import subprocess
import sys
import tarfile
import time


ROOT = Path(__file__).resolve().parents[2]
DATA = Path("/var/lib/rancher/k3s")
NAMESPACE = "ai-home-lab"
WRITERS = ("open-webui", "service-tools", "litellm")
CLAIMS = {"webui-rag-qwen3-data", "creative-models", "service-tools-state", "gateway-postgres"}
GAMING_HOMES = {"gaming-home": "steam-headless", "parent-gaming-home": "parent-steam-headless",
                "kids-gaming-home": "kids-steam-headless"}
CAPTURES = Path("/var/lib/workstation-service-backup")
TELEMETRY = Path("/var/lib/workstation-telemetry")
HOST_INCLUDE = Path("/etc/restic/workstation.include")
HOST_EXCLUDE = Path("/etc/restic/workstation.exclude")
REPOSITORY_ENTRY_LIMIT = 1000000
MODEL_LOCKS = {"Qwen3.8-27B-FP8": "SGLANG_DUAL", "Qwen3.5-9B": "SGLANG_SINGLE"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1048576), b""):
            result.update(chunk)
    return result.hexdigest()


def model_identity(model):
    lock = dict(line.split("=", 1) for line in (ROOT / "versions.lock").read_text().splitlines()
                if "=" in line and not line.startswith("#"))
    prefix = MODEL_LOCKS[model]
    return {"model": model, "repository": lock[prefix + "_MODEL_REPOSITORY"],
            "revision": lock[prefix + "_MODEL_REVISION"]}


def inventory_model(model, source, destination):
    """Hash an owner-staged model without including its downloadable weights."""
    source, destination = Path(source).resolve(), Path(destination).resolve()
    require(source.is_dir() and not destination.exists() and not destination.is_relative_to(source)
            and not destination.is_relative_to(ROOT), "use a staged model and fresh protected output outside the checkout")
    files = {}
    for path in sorted(source.rglob("*")):
        require(not path.is_symlink(), "inventory a materialized model tree, not cache symlinks")
        if path.is_file():
            files[str(path.relative_to(source))] = {"sha256": digest(path), "bytes": path.stat().st_size}
            require(len(files) <= 10000, "model inventory exceeds file bound")
    require(files and any(name.endswith(".safetensors") for name in files), "model weights missing")
    with destination.open("x", encoding="utf-8") as stream:
        json.dump({"schema_version": 1, **model_identity(model), "collected_at": int(time.time()),
                   "files": files}, stream, indent=2)
        stream.write("\n")
    destination.chmod(0o600)


def model_inventory(path):
    record = json.loads(private_file(path).read_text())
    model = record.get("model")
    require(model in MODEL_LOCKS and all(record.get(k) == v for k, v in model_identity(model).items()),
            "model inventory must match the current pinned identity")
    require(record.get("schema_version") == 1 and record.get("files"), "incomplete model inventory")
    for name, entry in record["files"].items():
        require(not Path(name).is_absolute() and ".." not in Path(name).parts and
                re.fullmatch(r"[a-f0-9]{64}", entry["sha256"]) and type(entry["bytes"]) is int
                and entry["bytes"] >= 0, "invalid model file inventory")
    return record


def private_file(path):
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), "protected file missing")
    meta = path.stat()
    require(meta.st_uid == os.geteuid() and stat.S_IMODE(meta.st_mode) == 0o600,
            "protected file requires caller ownership and mode 0600")
    return path


def run(argv, output=None, env=None, pass_fds=(), timeout=300):
    # Never propagate raw process output: pg_dump/Restic/Kubernetes errors can
    # contain private names, SQL or repository credentials.
    result = subprocess.run(argv, stdout=output or subprocess.PIPE, stderr=subprocess.PIPE,
                            env=env, pass_fds=pass_fds, timeout=timeout, check=False)
    require(result.returncode == 0, "operation failed; retain the protected journal and inspect locally")
    return result.stdout


def sqlite_check(path):
    with sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True) as db:
        require(db.execute("PRAGMA quick_check").fetchone() == ("ok",), "SQLite integrity check failed")


def archive(source, destination, excluded=()):
    # Tar preserves symlinks without following them. Extraction is deliberately
    # absent; isolated restore instructions must review these protected archives.
    def keep(member):
        if any(member.name == x or member.name.startswith(x + "/") for x in excluded):
            return None
        return member
    with tarfile.open(destination, "w", dereference=False) as bundle:
        bundle.add(source, arcname=".", filter=keep)


def materialized(path):
    """Refuse a symlink in any component before resolving a protected path."""
    path = Path(path)
    require(path.is_absolute() and ".." not in path.parts and
            not any(p.is_symlink() for p in (path, *path.parents)), "symlink or unsafe protected path")
    return path


def local_policy(policy):
    local = policy.get("local_repository")
    if local is None:
        return None
    require(isinstance(local, dict) and set(local) == {"mountpoint", "filesystem_uuid", "repository"},
            "invalid local repository policy")
    mount, repository = materialized(local["mountpoint"]), materialized(local["repository"])
    require(mount != Path("/") and repository != mount and repository.is_relative_to(mount) and
            isinstance(local["filesystem_uuid"], str) and
            re.fullmatch(r"[A-Za-z0-9-]{4,100}", local["filesystem_uuid"]), "invalid backup SSD identity")
    return local


def filesystem(path):
    documents = json.loads(run(["findmnt", "--json", "--target", str(path), "--output",
                                "TARGET,SOURCE,FSTYPE,UUID"]))["filesystems"]
    require(len(documents) == 1, "filesystem identity is ambiguous")
    return documents[0]


def physical_disks(source, backup=False):
    require(isinstance(source, str) and source.startswith("/dev/"), "block-device identity required")
    devices = json.loads(run(["lsblk", "--json", "--paths", "--inverse", "--output",
                              "NAME,TYPE,ROTA,SERIAL,WWN", source]))["blockdevices"]
    disks = set()
    def visit(device):
        kind = device.get("type", "")
        if backup:
            require(kind in {"disk", "part", "crypt"}, "backup SSD must not be RAID, LVM or aggregate storage")
        if kind == "disk":
            # Device names establish overlap during this boot; hardware identity
            # is also required so missing/virtual ancestry cannot imply safety.
            require(device.get("serial") or device.get("wwn"), "physical disk identity unavailable")
            if backup:
                require(device.get("rota") in (False, 0, "0"), "backup device must be an SSD")
            disks.add((device["name"], device.get("serial") or "", device.get("wwn") or ""))
        for child in device.get("children", []):
            visit(child)
    for device in devices:
        visit(device)
    require(disks and (not backup or len(disks) == 1), "physical storage ancestry unavailable or ambiguous")
    return disks


def validate_repository_tree(repository):
    mounts = json.loads(run(["findmnt", "--json", "--list", "--output", "TARGET"]))["filesystems"]
    require(len(mounts) <= 10000 and all(not Path(mount["target"]).is_relative_to(repository) for mount in mounts),
            "nested repository mount or oversized mount table; independent storage cannot be verified")
    deadline, count = time.monotonic() + 30, 0
    def failed_walk(error):
        raise ValueError("repository tree could not be inspected") from error
    for directory, directories, files in os.walk(repository, followlinks=False, onerror=failed_walk):
        count += len(directories) + len(files)
        require(count <= REPOSITORY_ENTRY_LIMIT and time.monotonic() < deadline, "repository inspection exceeds safety bound")
        require(not any((Path(directory) / name).is_symlink() for name in (*directories, *files)),
                "symlink inside initialized repository; independent storage cannot be verified")


def validate_local_repository(policy, *, sources=(), with_identity=False):
    """Read-only storage gate, always before maintenance or filesystem capture."""
    local = local_policy(policy)
    require(local is not None, "local-first policy required")
    mount, repository = materialized(local["mountpoint"]), materialized(local["repository"])
    for path in (mount, repository):
        require(path.is_dir() and path.stat().st_uid == 0 and not path.stat().st_mode & 0o022,
                "backup mount/repository must be protected root-owned directories")
    mounted = filesystem(repository)
    require(mounted.get("target") == str(mount) and mounted.get("uuid") == local["filesystem_uuid"] and
            mounted.get("fstype") in {"ext4", "xfs", "btrfs"}, "backup SSD mountpoint or filesystem UUID mismatch")
    backup = physical_disks(mounted["source"], backup=True)
    paths = (DATA, Path(policy["staging"]).parent, Path("/"),
             *(Path(name) for name in host_backup_policy(policy)), *sources)
    require(len(paths) <= 120, "backup source identity exceeds inspection bound")
    for path in dict.fromkeys(paths):
        active = physical_disks(filesystem(path)["source"])
        require(not any(a[0] == b[0] or (a[1] and a[1] == b[1]) or (a[2] and a[2] == b[2])
                        for a in active for b in backup), "backup SSD shares a physical disk with active storage")
    require((repository / "config").is_file() and not (repository / "config").is_symlink(),
            "owner must initialize the encrypted local repository first")
    validate_repository_tree(repository)
    identity = (mounted["source"], mounted["uuid"], frozenset(backup))
    return (repository, identity) if with_identity else repository


def credential_files(local=False, remote=False):
    directory = materialized(os.environ["CREDENTIALS_DIRECTORY"])
    names = (["restic_local_password"] if local else []) + (
        ["restic_repository", "restic_password", "aws_credentials"] if remote else [])
    for name in names:
        path = materialized(directory / name)
        require(path.is_file() and path.stat().st_uid == 0 and not path.stat().st_mode & 0o077,
                "missing or unprotected systemd backup credential")
    if remote:
        repository = (directory / "restic_repository").read_text().strip()
        require(repository.startswith(("s3:https://", "sftp:", "rest:https://")),
                "off-array encrypted transport required")
    return directory


def restic_arguments(credentials, repository=None):
    if repository is not None:
        return ["restic", "--repo", str(repository), "--password-file", str(credentials / "restic_local_password")]
    return ["restic", "--repository-file", str(credentials / "restic_repository"), "--password-file",
            str(credentials / "restic_password")]


def offsite_environment(credentials):
    return dict(os.environ, AWS_SHARED_CREDENTIALS_FILE=str(credentials / "aws_credentials"),
                AWS_EC2_METADATA_DISABLED="true")


def host_backup_policy(policy):
    for path in (HOST_INCLUDE, HOST_EXCLUDE):
        materialized(path)
        require(path.is_file() and path.stat().st_uid == 0 and not path.stat().st_mode & 0o022,
                "install and review protected host backup include/exclude policies first")
    paths = [line.strip() for line in HOST_INCLUDE.read_text().splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    require(len(paths) <= 100 and {"/etc", "/home", "/var/lib/sbctl"} <= set(paths),
            "host backup policy omits required configuration or homes or exceeds inspection bound")
    forbidden = (CAPTURES, DATA, Path(policy["local_repository"]["mountpoint"]))
    for name in paths:
        path = materialized(name)
        require(not any(c in name for c in "*?[]\\") and not any(
            path == p or path.is_relative_to(p) or p.is_relative_to(path) for p in forbidden),
            "host include overlaps live K3s, backup storage or capture staging")
    return paths


def commit_local(policy, directory, credentials):
    directory = materialized(directory)
    record = validate_capture(directory)
    captured_at = recovery_point(record)
    sources = capture_source_paths(directory, record)
    require(not (directory / "local-backup.json").exists(), "local snapshot already committed; replicate its exact ID")
    repository, storage_identity = validate_local_repository(policy, sources=sources, with_identity=True)
    host_paths = host_backup_policy(policy)
    require(all(digest(path) == digest(directory / name) for path, name in
                ((HOST_INCLUDE, "host-include.txt"), (HOST_EXCLUDE, "host-exclude.txt"))),
            "host backup policy changed during capture")
    response = run(restic_arguments(credentials, repository) + ["backup", "--json", "--tag",
                   "workstation-service", "--files-from", str(HOST_INCLUDE), "--exclude-file", str(HOST_EXCLUDE),
                   "--one-file-system", str(directory)], timeout=7200)
    summaries = [json.loads(line) for line in response.splitlines() if line.strip()]
    summaries = [s for s in summaries if s.get("message_type") == "summary"]
    require(len(summaries) == 1 and re.fullmatch(r"[a-f0-9]{64}", summaries[0].get("snapshot_id", "")),
            "local snapshot completion ID unavailable; retain capture and inspect repository")
    require(validate_local_repository(policy, sources=sources, with_identity=True) == (repository, storage_identity),
            "backup SSD identity changed during local capture; success cannot be verified")
    write_record(directory / "local-backup.json", {"schema_version": 1, "snapshot_id": summaries[0]["snapshot_id"],
                 "manifest_sha256": digest(directory / "manifest.json"), "local_repository": policy["local_repository"],
                 "captured_path": str(directory), "snapshot_paths": sorted([str(directory), *host_paths]),
                 "completed_at": int(time.time())})
    metrics(True, "local", recovery_time=captured_at)


def write_record(path, record):
    temporary = materialized(path.with_name("." + path.name + ".tmp"))
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(record, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    temporary.replace(path)


def capture_source_paths(directory, record):
    # Capture has always retained this journal under the manifest checksum.
    # Refuse replication without the evidence needed to check actual PVC mounts.
    require("journal.json" in record["files"], "capture journal required to verify PVC storage ancestry")
    volumes = json.loads((directory / "journal.json").read_text())["volumes"]
    require(set(volumes) == set(record["claims"]), "capture journal volume scope differs from manifest")
    sources = tuple(materialized(volume["source"]) for volume in volumes.values())
    require(all(path != DATA / "storage" and path.is_relative_to(DATA / "storage") for path in sources),
            "unexpected journal PV location")
    return sources


def replicate(policy_path, directory):
    """Copy only a recorded successful snapshot; no capture or session mutation."""
    policy = json.loads(private_file(policy_path).read_text())
    directory = materialized(directory)
    require(directory.is_relative_to(CAPTURES), "replication requires a protected capture directory")
    try:
        record = validate_capture(directory)
        captured_at = recovery_point(record)
        local = json.loads(private_file(directory / "local-backup.json").read_text())
        require(local.get("schema_version") == 1 and re.fullmatch(r"[a-f0-9]{64}", local.get("snapshot_id", "")) and
                local.get("manifest_sha256") == digest(directory / "manifest.json") and
                local.get("local_repository") == local_policy(policy) and local.get("captured_path") == str(directory),
                "local snapshot identity does not match this capture and repository")
        repository = validate_local_repository(policy, sources=capture_source_paths(directory, record))
        credentials = credential_files(local=True, remote=True)
        source = restic_arguments(credentials, repository)
        destination = restic_arguments(credentials)
        env = offsite_environment(credentials)
        snapshot = json.loads(run(source + ["snapshots", "--json", local["snapshot_id"]]))
        require(len(snapshot) == 1 and snapshot[0].get("id") == local["snapshot_id"] and
                sorted(snapshot[0].get("paths", [])) == local.get("snapshot_paths") and
                str(directory) in local.get("snapshot_paths", []) and
                {"/etc", "/home", "/var/lib/sbctl"} <= set(local.get("snapshot_paths", [])) and
                "workstation-service" in snapshot[0].get("tags", []), "local snapshot is missing or has different scope")
        # Read the manifest from the committed snapshot as well as staging. This
        # binds the exact successful ID to the verified SQLite/token/PVC set.
        committed_manifest = run(source + ["dump", local["snapshot_id"], str(directory / "manifest.json")])
        require(hashlib.sha256(committed_manifest).hexdigest() == local["manifest_sha256"],
                "local snapshot contains a different capture manifest")
        source_config = json.loads(run(source + ["cat", "config"]))
        destination_config = json.loads(run(destination + ["cat", "config"], env=env))
        require(source_config.get("chunker_polynomial") and source_config["chunker_polynomial"] ==
                destination_config.get("chunker_polynomial"), "owner must initialize matched repository chunker parameters")
        # Restic acquires repository locks. A repeat copies this ID or skips its
        # existing copy; interrupted uploads retain the valid local snapshot.
        run(destination + ["copy", "--from-repo", str(repository), "--from-password-file",
            str(credentials / "restic_local_password"), local["snapshot_id"]], env=env, timeout=7200)
        # Copy may assign a different ID (and records the source as original).
        # Resolve that destination identity even when copy skipped an existing
        # snapshot, then verify its scope and committed manifest before success.
        copied = json.loads(run(destination + ["snapshots", "--json", "--tag", "workstation-service"], env=env))
        copied = [snapshot for snapshot in copied if snapshot.get("original") == local["snapshot_id"] or
                  snapshot.get("id") == local["snapshot_id"]]
        require(len(copied) == 1 and re.fullmatch(r"[a-f0-9]{64}", copied[0].get("id", "")) and
                sorted(copied[0].get("paths", [])) == local["snapshot_paths"] and
                "workstation-service" in copied[0].get("tags", []), "destination snapshot missing, ambiguous or has different scope")
        destination_id = copied[0]["id"]
        copied_manifest = run(destination + ["dump", destination_id, str(directory / "manifest.json")], env=env)
        require(hashlib.sha256(copied_manifest).hexdigest() == local["manifest_sha256"],
                "destination snapshot contains a different capture manifest")
        write_record(directory / "offsite-backup.json", {"schema_version": 1, "snapshot_id": destination_id,
                     "source_snapshot_id": local["snapshot_id"],
                     "manifest_sha256": local["manifest_sha256"], "completed_at": int(time.time())})
        metrics(True, "offsite", local_enabled=True, recovery_time=captured_at)
    except BaseException:
        metrics(False, "offsite", local_enabled=True)
        raise
    print("Exact local snapshot copied off-site; local capture retained.")


def recovery_point(record):
    value = record.get("created_at")
    require(type(value) is int and 0 < value <= int(time.time()) + 300, "invalid capture recovery timestamp")
    return value


def validate_capture(directory):
    directory = Path(directory).resolve()
    record = json.loads((directory / "manifest.json").read_text())
    require(record.get("schema_version") == 1 and record.get("status") == "consistent-capture",
            "capture was not completed")
    if "created_at" in record:
        recovery_point(record)
    baseline = record.get("scope") == "baseline"
    claims = set(record.get("claims", []))
    gaming = claims.intersection(GAMING_HOMES)
    applications = claims - gaming
    require(record.get("database") == "k3s-sqlite" and
            (claims and applications <= CLAIMS - {"gateway-postgres", "service-tools-state"}
             if baseline else applications == CLAIMS), "wrong datastore or incomplete application capture")
    require(not gaming or set(record.get("gaming_claims", [])) == gaming, "gaming home inventory missing")
    require(set(record["files"]) >= {"state.db", "server-token", "server.tar", "config.tar",
                                    "models.json", "rag-SHA256SUMS"} |
            (set() if baseline else {"litellm.dump", "roles.sql"}) |
            {name + ".tar" for name in claims - {"gateway-postgres"}}, "required recovery files missing")
    for name, expected in record["files"].items():
        require(re.fullmatch(r"[A-Za-z0-9_.-]+", name) and not (directory / name).is_symlink(),
                "unsafe capture path")
        require(digest(directory / name) == expected, "capture checksum/token pairing mismatch")
    sqlite_check(directory / "state.db")
    for name in gaming:
        # Inspect every archive header without extracting or following links.
        # Absolute Steam symlink targets are preserved as links for owner review;
        # restore-stage only copies the archive, never materializes those links.
        with tarfile.open(directory / (name + ".tar"), "r:") as bundle:
            members = bundle.getmembers()
            require(members and all(not Path(m.name).is_absolute() and ".." not in Path(m.name).parts and
                    (m.isfile() or m.isdir() or m.issym() or m.islnk()) and
                    (not m.islnk() or (not Path(m.linkname).is_absolute() and ".." not in Path(m.linkname).parts))
                    for m in members), "unsafe or empty gaming home archive")
    # Integrity pairing is not proof that K3s can decrypt bootstrap data. That
    # requires the isolated boot acceptance in the runbook.
    return record


def restore_stage(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    forbidden = tuple(p.resolve() for p in (DATA, Path("/etc"), Path("/var/lib/kubelet"), Path("/run")))
    require(destination.is_absolute() and not any(destination == p or destination.is_relative_to(p)
            or p.is_relative_to(destination) for p in forbidden), "restore destination overlaps active system state")
    require(not destination.exists() and not destination.is_relative_to(source), "restore needs a fresh isolated directory")
    record = validate_capture(source)
    destination.mkdir(mode=0o700)
    for name in (*record["files"], "manifest.json"):
        shutil.copyfile(source / name, destination / name)
        (destination / name).chmod(0o600)
    validate_capture(destination)
    return destination


def metrics(success, stage="offsite", local_enabled=None, recovery_time=None):
    if recovery_time is not None:
        recovery_point({"created_at": recovery_time})
    require(TELEMETRY.is_dir() and not TELEMETRY.is_symlink(), "prepare telemetry textfile directory first")
    fd = os.open(TELEMETRY / ".service-backup.metrics.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        _metrics(success, stage, local_enabled, recovery_time)
    finally:
        os.close(fd)


def _metrics(success, stage, local_enabled, recovery_time):
    require(stage in {"local", "offsite"}, "invalid backup metric stage")
    directory = TELEMETRY
    require(directory.is_dir() and not directory.is_symlink(), "prepare telemetry textfile directory first")
    path = directory / "service-backup.prom"
    require(not path.is_symlink(), "unsafe metric path")
    previous = {}
    if path.exists():
        previous = dict(re.findall(r"^(workstation_service_backup_[a-z_]+) ([0-9]+)$", path.read_text(), re.M))
    temporary = directory / ".service-backup.prom.tmp"
    require(not temporary.is_symlink() and not path.is_symlink(), "unsafe metric path")
    values = {}
    for name in ("local", "offsite"):
        prefix = "workstation_service_backup_" + name
        legacy = "workstation_service_backup" if name == "offsite" else prefix
        values[prefix + "_last_success_seconds"] = previous.get(prefix + "_last_success_seconds",
                                                                previous.get(legacy + "_last_success_seconds", "0"))
        values[prefix + "_last_attempt_failed"] = previous.get(prefix + "_last_attempt_failed",
                                                              previous.get(legacy + "_last_attempt_failed", "0"))
    prefix = "workstation_service_backup_" + stage
    if success and recovery_time is not None:
        values[prefix + "_last_success_seconds"] = str(max(int(values[prefix + "_last_success_seconds"]), recovery_time))
    values[prefix + "_last_attempt_failed"] = "0" if success else "1"
    for suffix in ("_last_success_seconds", "_last_attempt_failed"):
        values["workstation_service_backup" + suffix] = values["workstation_service_backup_offsite" + suffix]
    values["workstation_service_backup_local_enabled"] = str(int(local_enabled)) if local_enabled is not None else (
        "1" if stage == "local" else previous.get("workstation_service_backup_local_enabled", "0"))
    temporary.write_text("".join(f"{name} {value}\n" for name, value in values.items()))
    temporary.chmod(0o644)
    temporary.replace(path)


class Capture:
    def __init__(self, policy, baseline=False, local_first=False):
        self.policy_path = str(Path(policy).resolve())
        self.policy = json.loads(private_file(policy).read_text())
        require(set(self.policy) - {"local_repository"} == {"context", "node", "kubeconfig", "session_config", "hardware",
                                    "state_directory", "staging", "environment", "model_inventories", "scope"}, "invalid backup policy keys")
        local_policy(self.policy)
        require(not local_first or self.policy.get("local_repository") is not None, "local-first policy required")
        require(self.policy["scope"] in {"baseline", "service"}, "explicit backup scope required")
        self.baseline = baseline or self.policy["scope"] == "baseline"
        require(isinstance(self.policy["model_inventories"], list) and
                1 <= len(self.policy["model_inventories"]) <= 2, "provide inventories for staged inference models")
        self.models = [model_inventory(path) for path in self.policy["model_inventories"]]
        require(len({m["model"] for m in self.models}) == len(self.models), "duplicate model inventory")
        require(self.policy["environment"] in {"dev", "tst", "int"}, "explicit non-production target required")
        for name in ("context", "node"):
            require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,200}", self.policy[name]), "invalid target")
        private_file(self.policy["kubeconfig"])
        private_file(self.policy["session_config"])
        require(Path("/etc/workstation/session-policy.conf").is_file(), "installed canonical session policy required")
        self.kube = ["kubectl", "--kubeconfig", self.policy["kubeconfig"], "--context", self.policy["context"],
                     "--request-timeout=20s"]

    def get(self, *args):
        return json.loads(run(self.kube + list(args) + ["-o", "json"]))

    def volumes(self, node):
        pvcs = self.get("-n", NAMESPACE, "get", "pvc")["items"]
        names = {p["metadata"]["name"] for p in pvcs}
        require(not any(re.search(r"gaming.*home|home.*gaming", name, re.I) and name not in GAMING_HOMES
                        for name in names), "unknown gaming-home claim requires an explicit backup policy change")
        claims = {p["metadata"]["name"]: p for p in pvcs if p["metadata"]["name"] in CLAIMS | GAMING_HOMES.keys()}
        if self.baseline:
            require(not set(claims).intersection({"gateway-postgres", "service-tools-state"}),
                    "new service claims require a full capture")
            # Preserve the pre-service treatment of unbound application claims,
            # but a known gaming home must never be silently omitted.
            claims = {name: claim for name, claim in claims.items()
                      if name in GAMING_HOMES or claim["status"]["phase"] == "Bound"}
            require(claims, "no existing bound application state to capture")
        else:
            require(CLAIMS <= set(claims), "initialize required service claims before full capture")
        volumes = {}
        for name, claim in sorted(claims.items()):
            require(claim["status"]["phase"] == "Bound", "bind required application and gaming-home claims before capture")
            pv = self.get("get", "pv", claim["spec"]["volumeName"])
            reference = pv["spec"]["claimRef"]
            require(pv["spec"]["persistentVolumeReclaimPolicy"] == "Retain" and
                    reference["uid"] == claim["metadata"]["uid"] and
                    reference.get("name", name) == name and reference.get("namespace", NAMESPACE) == NAMESPACE,
                    "migrate bound PV retention and verify claim identity first")
            terms = pv["spec"]["nodeAffinity"]["required"]["nodeSelectorTerms"]
            require(len(terms) == 1 and any(e.get("key") == "kubernetes.io/hostname" and
                    e.get("operator") == "In" and e.get("values") == [node["metadata"]["labels"]["kubernetes.io/hostname"]]
                    for e in terms[0].get("matchExpressions", [])), "PV is not confined to this node")
            source = materialized(pv["spec"].get("hostPath", pv["spec"].get("local", {}))["path"])
            require(source.is_dir() and source.is_relative_to(DATA / "storage") and source != DATA / "storage",
                    "unexpected PV location")
            volumes[name] = {"pvc_uid": claim["metadata"]["uid"], "pv_uid": pv["metadata"]["uid"],
                             "pv": pv["metadata"]["name"], "source": str(source)}
        return volumes

    def gaming_writers_plan(self, volumes):
        gaming = set(volumes).intersection(GAMING_HOMES)
        if not gaming:
            return ()
        deployments = self.get("-n", NAMESPACE, "get", "deployments")["items"]
        managed = {}
        def used(spec):
            claims = set()
            for volume in spec.get("volumes", []):
                claim = volume.get("persistentVolumeClaim", {}).get("claimName")
                if claim in gaming:
                    claims.add(claim)
                # Direct hostPath mounts can bypass PVC ownership. Refuse them
                # rather than pretending scaling a known Deployment covers them.
                if "hostPath" in volume:
                    path = materialized(volume["hostPath"]["path"])
                    require(not any(path == Path(volumes[name]["source"]) or
                            path.is_relative_to(Path(volumes[name]["source"])) or
                            Path(volumes[name]["source"]).is_relative_to(path) for name in gaming),
                            "unmanaged hostPath writer overlaps a gaming home")
            return claims
        for deployment in deployments:
            claims = used(deployment["spec"]["template"]["spec"])
            if claims:
                name = deployment["metadata"]["name"]
                require(len(claims) == 1 and GAMING_HOMES[next(iter(claims))] == name and
                        deployment["spec"]["replicas"] in (0, 1), "unmanaged gaming-home deployment writer")
                managed[deployment["metadata"]["uid"]] = name
        self.gaming_journal = {d["metadata"]["name"]: {"uid": d["metadata"]["uid"],
                              "replicas": d["spec"]["replicas"]} for d in deployments
                              if d["metadata"]["uid"] in managed}
        pods = self.get("-n", NAMESPACE, "get", "pods")["items"]
        replicasets = self.get("-n", NAMESPACE, "get", "replicasets")["items"]
        owners = {}
        for replica in replicasets:
            controller = [r for r in replica["metadata"].get("ownerReferences", []) if r.get("controller")]
            if len(controller) == 1 and controller[0].get("kind") == "Deployment" and controller[0]["uid"] in managed:
                owners[replica["metadata"]["uid"]] = controller[0]["uid"]
        for pod in pods:
            if used(pod["spec"]):
                controller = [r for r in pod["metadata"].get("ownerReferences", []) if r.get("controller")]
                require(len(controller) == 1 and controller[0].get("kind") == "ReplicaSet" and
                        controller[0]["uid"] in owners, "unmanaged gaming-home Pod writer")
        return tuple(sorted(managed.values()))

    def stopped(self):
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if not self.get("-n", NAMESPACE, "get", "pods")["items"]:
                break
            time.sleep(2)
        else:
            raise ValueError("application Pods did not exit; capture refused")
        containers = json.loads(run(["k3s", "crictl", "ps", "-o", "json"]))
        require(not any(c.get("labels", {}).get("io.kubernetes.pod.namespace") == NAMESPACE
                        for c in containers["containers"]), "CRI still has application containers")

    def scale_zero(self, name, journal, output):
        document = self.get("-n", NAMESPACE, "get", "deployment", name)
        require(document["spec"]["replicas"] in (0, 1), "unexpected replica count")
        if name in journal:
            require(journal[name]["uid"] == document["metadata"]["uid"], "planned gaming deployment was replaced")
        else:
            journal[name] = {"uid": document["metadata"]["uid"], "replicas": document["spec"]["replicas"]}
        # Persist the original identity BEFORE a mutation. A failed Pod wait must
        # not lose the information needed to resume this deployment safely.
        (output / "journal.json").write_text(json.dumps({"phase": "quiescing", "deployments": journal}))
        run(self.kube + ["-n", NAMESPACE, "scale", "deployment/" + name,
            "--resource-version=" + document["metadata"]["resourceVersion"],
            "--current-replicas=" + str(document["spec"]["replicas"]), "--replicas=0"])
        # Scale is not quiescence: wait for every Pod selected by this Deployment.
        selector = ",".join(f"{k}={v}" for k, v in document["spec"]["selector"]["matchLabels"].items())
        deadline = time.monotonic() + 180
        while self.get("-n", NAMESPACE, "get", "pods", "-l", selector)["items"]:
            require(time.monotonic() < deadline, "writer did not exit; capture refused")
            time.sleep(2)

    def quiesce_apps(self, output, journal):
        writers = WRITERS
        if self.baseline:
            deployments = self.get("-n", NAMESPACE, "get", "deployments")["items"]
            names = {d["metadata"]["name"] for d in deployments}
            require(not names.intersection({"service-tools", "litellm", "gateway-postgres"}),
                    "baseline capture is only for the pre-service cluster")
            writers = ("open-webui",) if "open-webui" in names else ()
        for name in (*getattr(self, "gaming_writers", ()), *writers):
            self.scale_zero(name, journal, output)
        if self.baseline:
            self.stopped()
            return
        pg = self.get("-n", NAMESPACE, "get", "pods", "-l", "app.kubernetes.io/name=gateway-postgres")["items"]
        require(len(pg) == 1 and pg[0]["status"]["phase"] == "Running", "one running database required")
        for name, command in (("litellm.dump", ["pg_dump", "-U", "postgres", "-d", "litellm", "-Fc"]),
                              ("roles.sql", ["pg_dumpall", "-U", "postgres", "--globals-only"])):
            with (output / name).open("xb") as stream:
                run(self.kube + ["-n", NAMESPACE, "exec", pg[0]["metadata"]["name"], "--"] + command, output=stream)
        self.scale_zero("gateway-postgres", journal, output)
        self.stopped()

    def capture(self):
        stage = "local" if self.policy.get("local_repository") is not None else "offsite"
        try:
            self._capture()
        except BaseException:
            metrics(False, stage, local_enabled=stage == "local")
            raise

    def _capture(self):
        require(os.geteuid() == 0 and Path("/etc/arch-release").is_file(), "capture requires the explicit Arch host")
        require(Path("/proc/sys/kernel/random/boot_id").read_text().strip() ==
                (Path(self.policy["hardware"]).parent / "boot-id.txt").read_text().strip(), "recollect current-boot hardware")
        output = materialized(self.policy["staging"])
        require(output.is_relative_to(CAPTURES) and not output.exists(),
                "use a new protected capture directory under /var/lib/workstation-service-backup")
        require(output.parent.is_dir() and output.parent.stat().st_uid == 0 and
                stat.S_IMODE(output.parent.stat().st_mode) == 0o700, "capture parent must be root-owned 0700")
        local = self.policy.get("local_repository") is not None
        if local:
            validate_local_repository(self.policy)
            host_backup_policy(self.policy)
        credentials = credential_files(local=local, remote=not local)
        if local:
            config = json.loads(run(restic_arguments(credentials, Path(self.policy["local_repository"]["repository"])) +
                                    ["cat", "config"]))
            require(config.get("chunker_polynomial"), "owner must initialize and unlock the encrypted local repository")
        state = materialized(self.policy["state_directory"])
        require(state.is_dir() and not state.is_symlink() and stat.S_IMODE(state.stat().st_mode) == 0o700
                and state.stat().st_uid == 0, "canonical session directory must be root-owned 0700")
        fd = os.open(state / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            require(fd == 3, "canonical inherited descriptor 3 unavailable; close extra descriptors")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            installed = {}
            for line in private_file("/etc/workstation/session-policy.conf").read_text().splitlines():
                fields = shlex.split(line, comments=True)
                if fields and "=" in fields[0]:
                    key, value = fields[0].split("=", 1)
                    installed[key] = value
            require(installed.get("SESSION_STATE_DIRECTORY") == str(state) and
                    installed.get("SESSION_KUBECONFIG") == self.policy["kubeconfig"], "backup and session identities differ")
            planned = json.loads(run([str(ROOT / "bin/workstationctl"), "--config", self.policy["session_config"],
                                      "session", "plan", "maintenance"]))
            require(planned["context"] == self.policy["context"] and planned["node"] == self.policy["node"] and
                    planned["namespace"] == NAMESPACE, "backup and session targets differ")
            node = self.get("get", "node", self.policy["node"])
            require(node["status"]["nodeInfo"]["bootID"] == Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                    "backup kubeconfig does not identify this host boot")
            # Resolve retention/binding before stopping any writer. The capture
            # still rechecks the same claim/PV identities after quiescing.
            planned_volumes = self.volumes(node)
            self.gaming_writers = self.gaming_writers_plan(planned_volumes)
            if local:
                validate_local_repository(self.policy, sources=tuple(Path(v["source"]) for v in planned_volumes.values()))
            output.mkdir(mode=0o700, parents=False)
            journal = dict(getattr(self, "gaming_journal", {}))
            (output / "journal.json").write_text(json.dumps({"phase": "quiescing", "resume": "explicit-owner-only",
                                                            "deployments": journal}))
            env = dict(os.environ, WORKSTATION_SESSION_LOCK_FD="3")
            run([str(ROOT / "bin/workstationctl"), "--config", self.policy["session_config"], "session", "switch",
                 "maintenance", self.policy["hardware"], str(state), "--execute"], env=env, pass_fds=(fd,), timeout=600)
            self.quiesce_apps(output, journal)
            volumes = self.volumes(node)
            require(volumes == planned_volumes, "claim/PV/node/path changed during capture")
            for name, volume in volumes.items():
                if name != "gateway-postgres":
                    archive(Path(volume["source"]), output / (name + ".tar"),
                            ("./Models",) if name == "creative-models" else ())
            # K3s stop does not stop containers. The Pod and CRI checks above
            # happen first, then the offline SQLite/server/token capture.
            run(["systemctl", "stop", "k3s.service"])
            require(subprocess.run(["systemctl", "is-active", "--quiet", "k3s.service"], check=False).returncode == 3,
                    "K3s did not reach inactive state")
            require(not (DATA / "server/db/etcd").exists(), "this capture handles bare-metal SQLite only")
            sqlite_check(DATA / "server/db/state.db")
            archive(DATA / "server", output / "server.tar")
            # SQLite backup API folds any WAL into a self-contained database.
            with sqlite3.connect((DATA / "server/db/state.db").as_uri() + "?mode=ro", uri=True) as src:
                with sqlite3.connect(output / "state.db") as dst:
                    src.backup(dst)
            shutil.copyfile(DATA / "server/token", output / "server-token")
            with tarfile.open(output / "config.tar", "w", dereference=False) as bundle:
                for path in (Path("/etc/rancher/k3s"), Path("/etc/systemd/system/k3s.service"),
                             Path("/etc/systemd/system/k3s.service.env"), Path("/etc/workstation")):
                    if path.exists():
                        bundle.add(path, arcname=str(path).lstrip("/"))
            shutil.copyfile(ROOT / "versions.lock", output / "versions.lock")
            shutil.copyfile(ROOT / "apps/overlays/rag/SHA256SUMS", output / "rag-SHA256SUMS")
            (output / "models.json").write_text(json.dumps(self.models, indent=2) + "\n")
            shutil.copyfile(ROOT / "infrastructure/creative/runtime.json", output / "creative-runtime.json")
            shutil.copyfile(ROOT / "apps/base/shared-api/litellm.yaml", output / "gateway-config.yaml")
            if local:
                shutil.copyfile(HOST_INCLUDE, output / "host-include.txt")
                shutil.copyfile(HOST_EXCLUDE, output / "host-exclude.txt")
            next_command = [str(ROOT / "bin/workstationctl"), "service-backup", "replicate", self.policy_path,
                            str(output), "--execute"] if local else []
            (output / "journal.json").write_text(json.dumps({"phase": "captured-stopped", "deployments": journal,
                    "volumes": volumes, "resume": "explicit-owner-only",
                    "next": "verify deployment UIDs, explicitly resume and verify services, then replicate" if local else
                            "verify deployment UIDs and explicitly resume services",
                    "replicate_command": next_command}, indent=2))
            record = {"schema_version": 1, "status": "consistent-capture", "database": "k3s-sqlite",
                      "scope": "baseline" if self.baseline else "service",
                      "claims": sorted(volumes), "gaming_claims": sorted(set(volumes).intersection(GAMING_HOMES)),
                      "created_at": int(time.time()),
                      "files": {p.name: digest(p) for p in output.iterdir() if p.is_file()}}
            (output / "manifest.json").write_text(json.dumps(record, indent=2) + "\n")
            for path in output.iterdir():
                path.chmod(0o600)
            validate_capture(output)
            if local:
                commit_local(self.policy, output, credentials)
                print("Consistent capture committed to encrypted backup SSD; cluster remains stopped. "
                      "Explicitly resume and verify services using the journal, then run: " + shlex.join(next_command))
            else:
                run(restic_arguments(credentials) + ["backup", "--quiet", "--tag", "workstation-service", str(output)],
                    env=offsite_environment(credentials), timeout=7200)
                metrics(True, "offsite", local_enabled=False, recovery_time=recovery_point(record))
                print("Consistent capture encrypted and uploaded; cluster remains stopped. Follow the explicit resume runbook.")
        finally:
            os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("capture")
    capture.add_argument("policy")
    capture.add_argument("--execute", action="store_true", required=True)
    capture.add_argument("--baseline", action="store_true", help="pre-service cluster only; no new database/tools claims")
    capture.add_argument("--local-first", action="store_true", help="require the independent local backup SSD policy")
    verify = commands.add_parser("verify")
    verify.add_argument("directory")
    restore = commands.add_parser("restore-stage")
    restore.add_argument("source")
    restore.add_argument("destination")
    replicate_command = commands.add_parser("replicate")
    replicate_command.add_argument("policy")
    replicate_command.add_argument("directory")
    replicate_command.add_argument("--execute", action="store_true", required=True)
    result = commands.add_parser("record-result")
    result.add_argument("--stage", choices=("local", "offsite"), default="offsite")
    inventory = commands.add_parser("inventory-model")
    inventory.add_argument("model", choices=MODEL_LOCKS)
    inventory.add_argument("source")
    inventory.add_argument("destination")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.command == "capture":
            Capture(args.policy, args.baseline, args.local_first).capture()
        elif args.command == "replicate":
            replicate(args.policy, args.directory)
        elif args.command == "verify":
            validate_capture(args.directory)
            print("Capture integrity passed; isolated application restore still required.")
        elif args.command == "record-result":
            if os.environ.get("SERVICE_RESULT") != "success":
                metrics(False, args.stage)
        elif args.command == "inventory-model":
            inventory_model(args.model, args.source, args.destination)
            print("Model identities and file hashes recorded; quality and runtime qualification unchanged.")
        else:
            restore_stage(args.source, args.destination)
            print("Protected restore staged; active cluster and PVCs untouched.")
    except (ValueError, OSError, KeyError, TypeError, tarfile.TarError, sqlite3.Error, subprocess.SubprocessError):
        print("Service backup refused or failed; retain protected staging and follow the recovery runbook.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
