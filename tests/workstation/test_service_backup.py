"""Offline backup gates and interrupted replication; no cluster or real credentials."""
from contextlib import contextmanager
import copy
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib/workstation"))
import service_backup as backup


@contextmanager
def root_metadata():
    original = Path.stat
    def root_stat(path, *args, **kwargs):
        values = list(original(path, *args, **kwargs))
        values[4] = 0
        return os.stat_result(values)
    with patch.object(Path, "stat", root_stat):
        yield


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.mount = self.root / "ssd"
        self.repository = self.mount / "restic"
        self.repository.mkdir(parents=True, mode=0o700)
        self.mount.chmod(0o700)
        (self.repository / "config").write_text("encrypted repository fixture")
        self.staging = self.root / "captures"
        self.staging.mkdir(mode=0o700)
        self.capture = self.staging / "one"
        self.credentials = self.root / "credentials"
        self.credentials.mkdir(mode=0o700)
        self.policy = {"staging": str(self.capture), "local_repository": {
            "mountpoint": str(self.mount), "filesystem_uuid": "aabb-1122", "repository": str(self.repository)}}
        self.policy_path = self.root / "policy.json"
        self.policy_path.write_text(json.dumps(self.policy))
        self.policy_path.chmod(0o600)
        self.telemetry = self.root / "metrics"
        self.telemetry.mkdir()
        self.include = self.root / "include"
        self.exclude = self.root / "exclude"
        self.include.write_text("# reviewed host policy\n/etc\n/home\n/var/lib/sbctl\n")
        self.exclude.write_text("/etc/credstore\n/etc/credstore.encrypted\n/home/*/.cache\n")
        self.calls = []
        self.snapshot_id = "a" * 64
        self.destination_id = "b" * 64
        self.destination_available = True
        self.destination_manifest_valid = True
        self.fail_copy = False
        self.polynomial = "abcdef"
        self.nested_mounts = []

    def storage_run(self, argv, **kwargs):
        if argv[0] == "findmnt":
            if "--list" in argv:
                return json.dumps({"filesystems": [{"target": str(self.mount)}, {"target": "/"},
                                                    *({"target": str(path)} for path in self.nested_mounts)]}).encode()
            path = argv[argv.index("--target") + 1]
            local = path == str(self.repository)
            return json.dumps({"filesystems": [{"target": str(self.mount) if local else "/",
                "source": "/dev/backup1" if local else "/dev/active1", "fstype": "ext4",
                "uuid": "aabb-1122" if local else "active-0000"}]}).encode()
        if argv[0] == "lsblk":
            local = argv[-1] == "/dev/backup1"
            return json.dumps({"blockdevices": [{"name": argv[-1], "type": "part", "children": [
                {"name": "/dev/backup" if local else "/dev/active", "type": "disk", "rota": False,
                 "serial": "backup-hardware" if local else "active-hardware", "wwn": ""}]}]}).encode()
        self.fail("unexpected storage command")

    def make_capture(self, gaming=()):
        self.capture.mkdir(mode=0o700)
        with sqlite3.connect(self.capture / "state.db") as database:
            database.execute("CREATE TABLE fixture (value TEXT)")
        for name in ("server-token", "server.tar", "config.tar", "models.json", "rag-SHA256SUMS",
                     "litellm.dump", "roles.sql", "webui-rag-qwen3-data.tar", "creative-models.tar",
                     "service-tools-state.tar"):
            (self.capture / name).write_text("synthetic offline fixture")
        (self.capture / "host-include.txt").write_bytes(self.include.read_bytes())
        (self.capture / "host-exclude.txt").write_bytes(self.exclude.read_bytes())
        for name in gaming:
            home = self.root / name
            (home / ".local/share/Steam/steamapps/compatdata/42/pfx/drive_c/users/steamuser/Saved Games").mkdir(parents=True)
            (home / ".local/share/Steam/steamapps/compatdata/42/pfx/drive_c/users/steamuser/Saved Games/save.dat").write_text("save")
            (home / ".config/sunshine").mkdir(parents=True)
            (home / ".config/sunshine/state.json").write_text("pairing fixture")
            backup.archive(home, self.capture / (name + ".tar"))
        (self.capture / "journal.json").write_text(json.dumps({"volumes": {
            claim: {"source": str(backup.DATA / "storage" / claim)} for claim in backup.CLAIMS | set(gaming)}}))
        record = {"schema_version": 1, "status": "consistent-capture", "database": "k3s-sqlite",
                  "scope": "service", "claims": sorted(backup.CLAIMS | set(gaming)), "gaming_claims": list(gaming),
                  "created_at": 50,
                  "files": {p.name: backup.digest(p) for p in self.capture.iterdir()}}
        (self.capture / "manifest.json").write_text(json.dumps(record))
        for path in self.capture.iterdir():
            path.chmod(0o600)

    @contextmanager
    def local_context(self):
        original_symlink = Path.is_symlink
        def linux_symlink(path):
            return False if str(path) in {"/etc", "/var", "/home"} else original_symlink(path)
        with root_metadata(), patch.object(backup.os, "geteuid", return_value=0), patch.object(backup, "CAPTURES", self.staging), \
                patch.object(backup, "TELEMETRY", self.telemetry), patch.object(backup, "HOST_INCLUDE", self.include), \
                patch.object(backup, "HOST_EXCLUDE", self.exclude), patch.object(Path, "is_symlink", linux_symlink):
            yield

    def restic_run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if argv[0] != "restic":
            return self.storage_run(argv, **kwargs)
        if "backup" in argv:
            self.assertIn("--files-from", argv)
            self.assertEqual(str(self.include), argv[argv.index("--files-from") + 1])
            self.assertEqual(str(self.exclude), argv[argv.index("--exclude-file") + 1])
            self.assertIn("--one-file-system", argv)
            self.assertEqual(str(self.capture), argv[-1])
            return json.dumps({"message_type": "summary", "snapshot_id": self.snapshot_id}).encode()
        if "snapshots" in argv:
            if "--repo" not in argv:
                return json.dumps([{"id": self.destination_id, "original": self.snapshot_id,
                    "paths": sorted([str(self.capture), "/etc", "/home", "/var/lib/sbctl"]),
                    "tags": ["workstation-service"]}] if self.destination_available else []).encode()
            return json.dumps([{"id": self.snapshot_id, "paths": sorted([str(self.capture), "/etc", "/home", "/var/lib/sbctl"]),
                                "tags": ["workstation-service"]}]).encode()
        if "dump" in argv:
            if "--repo" not in argv:
                self.assertEqual(self.destination_id, argv[argv.index("dump") + 1])
                if not self.destination_manifest_valid:
                    return b"different destination manifest"
            return (self.capture / "manifest.json").read_bytes()
        if "cat" in argv:
            return json.dumps({"chunker_polynomial": "abcdef" if "--repo" in argv else self.polynomial}).encode()
        if "copy" in argv:
            self.assertEqual(self.snapshot_id, argv[-1])
            self.assertEqual(str(self.repository), argv[argv.index("--from-repo") + 1])
            self.assertNotIn("WORKSTATION_SESSION_LOCK_FD", kwargs.get("env", {}))
            self.assertEqual(str(self.credentials / "aws_credentials"), kwargs["env"]["AWS_SHARED_CREDENTIALS_FILE"])
            if self.fail_copy:
                raise ValueError("synthetic offsite outage")
            return b""
        self.fail("unexpected restic operation")

    def metric_values(self):
        return dict(line.split() for line in (self.telemetry / "service-backup.prom").read_text().splitlines())

    def test_correct_mount_and_physical_independence(self):
        with self.local_context(), patch.object(backup, "run", self.storage_run):
            self.assertEqual(self.repository, backup.validate_local_repository(self.policy))

    def test_missing_wrong_mount_and_uuid_are_refused(self):
        for target, uuid in (("/", "aabb-1122"), (str(self.mount), "wrong-uuid")):
            def wrong(argv, **kwargs):
                if argv[0] == "findmnt":
                    return json.dumps({"filesystems": [{"target": target, "uuid": uuid, "fstype": "ext4", "source": "/dev/x"}]}).encode()
                self.fail("identity rejection must precede block inspection")
            with self.subTest(target=target, uuid=uuid), self.local_context(), patch.object(backup, "run", wrong):
                with self.assertRaisesRegex(ValueError, "mountpoint|UUID"):
                    backup.validate_local_repository(self.policy)
        self.repository.rename(self.mount / "missing")
        with self.local_context(), self.assertRaisesRegex(ValueError, "protected"):
            backup.validate_local_repository(self.policy)

    def test_shared_disk_and_raid_are_refused(self):
        for mode in ("shared", "raid"):
            def unsafe(argv, **kwargs):
                result = json.loads(self.storage_run(argv, **kwargs))
                if argv[0] == "lsblk" and argv[-1] == "/dev/backup1":
                    if mode == "shared":
                        result["blockdevices"][0]["children"][0]["serial"] = "active-hardware"
                    else:
                        result["blockdevices"][0]["type"] = "raid0"
                return json.dumps(result).encode()
            with self.subTest(mode=mode), self.local_context(), patch.object(backup, "run", unsafe):
                with self.assertRaisesRegex(ValueError, "physical disk|RAID"):
                    backup.validate_local_repository(self.policy)

    def test_host_root_or_actual_pvc_on_backup_disk_is_refused(self):
        pvc_source = Path("/var/lib/rancher/k3s/storage/pvc-home")
        for shared in ("/home", str(pvc_source)):
            def shared_source(argv, **kwargs):
                result = json.loads(self.storage_run(argv, **kwargs))
                if argv[0] == "findmnt" and "--target" in argv and argv[argv.index("--target") + 1] == shared:
                    result["filesystems"][0]["source"] = "/dev/backup2"
                elif argv[0] == "lsblk" and argv[-1] == "/dev/backup2":
                    result["blockdevices"][0]["children"][0].update(name="/dev/backup", serial="backup-hardware")
                return json.dumps(result).encode()
            with self.subTest(shared=shared), self.local_context(), patch.object(backup, "run", shared_source), \
                    self.assertRaisesRegex(ValueError, "shares a physical disk"):
                backup.validate_local_repository(self.policy, sources=(pvc_source,))

    def test_capture_journal_sources_are_manifest_bound_and_scope_checked(self):
        self.make_capture()
        record = backup.validate_capture(self.capture)
        volumes = {claim: {"source": str(backup.DATA / "storage" / claim)} for claim in record["claims"]}
        (self.capture / "journal.json").write_text(json.dumps({"volumes": volumes}))
        record["files"]["journal.json"] = backup.digest(self.capture / "journal.json")
        (self.capture / "manifest.json").write_text(json.dumps(record))
        with self.local_context():
            verified = backup.validate_capture(self.capture)
            self.assertEqual(set(Path(v["source"]) for v in volumes.values()), set(backup.capture_source_paths(self.capture, verified)))
        volumes.pop(next(iter(volumes)))
        (self.capture / "journal.json").write_text(json.dumps({"volumes": volumes}))
        with self.assertRaisesRegex(ValueError, "pairing"):
            backup.validate_capture(self.capture)
        with self.assertRaisesRegex(ValueError, "scope"):
            backup.capture_source_paths(self.capture, record)

    def test_rotational_or_missing_disk_identity_is_refused(self):
        for field, value in (("rota", True), ("serial", "")):
            def unsafe(argv, **kwargs):
                result = json.loads(self.storage_run(argv, **kwargs))
                if argv[0] == "lsblk":
                    result["blockdevices"][0]["children"][0][field] = value
                return json.dumps(result).encode()
            with self.subTest(field=field), self.local_context(), patch.object(backup, "run", unsafe), self.assertRaises(ValueError):
                backup.validate_local_repository(self.policy)

    def test_symlinked_mount_or_ancestor_is_refused(self):
        alias = self.root / "alias"
        alias.symlink_to(self.mount, target_is_directory=True)
        policy = copy.deepcopy(self.policy)
        policy["local_repository"]["mountpoint"] = str(alias)
        policy["local_repository"]["repository"] = str(alias / "restic")
        with self.local_context(), self.assertRaisesRegex(ValueError, "symlink"):
            backup.validate_local_repository(policy)

    def test_repository_data_or_deeper_symlink_to_active_storage_is_refused(self):
        for name in ("data", "index/deep/redirect"):
            link = self.repository / name
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(self.staging, target_is_directory=True)
            with self.subTest(name=name), self.local_context(), patch.object(backup, "run", self.storage_run), \
                    self.assertRaisesRegex(ValueError, "symlink inside initialized repository"):
                backup.validate_local_repository(self.policy)
            link.unlink()

    def test_nested_repository_bind_mount_is_refused_before_tree_walk(self):
        self.nested_mounts = [self.repository / "data/ab"]
        with self.local_context(), patch.object(backup, "run", self.storage_run), patch.object(backup.os, "walk") as walk:
            with self.assertRaisesRegex(ValueError, "nested repository mount"):
                backup.validate_local_repository(self.policy)
            walk.assert_not_called()

    def test_repository_inspection_is_bounded_and_read_errors_fail_closed(self):
        (self.repository / "data").mkdir()
        with self.local_context(), patch.object(backup, "run", self.storage_run), \
                patch.object(backup, "REPOSITORY_ENTRY_LIMIT", 1), self.assertRaisesRegex(ValueError, "safety bound"):
            backup.validate_local_repository(self.policy)
        def failed_walk(*args, **kwargs):
            kwargs["onerror"](OSError("synthetic directory access failure"))
            return iter(())
        with self.local_context(), patch.object(backup, "run", self.storage_run), patch.object(backup.os, "walk", failed_walk), \
                self.assertRaisesRegex(ValueError, "could not be inspected"):
            backup.validate_local_repository(self.policy)

    def test_mount_or_physical_disk_replaced_during_backup_cannot_report_success(self):
        self.make_capture()
        for replacement in ("unmounted", "different-physical-disk"):
            completed = False
            def changed(argv, **kwargs):
                nonlocal completed
                result = self.restic_run(argv, **kwargs)
                if "backup" in argv:
                    completed = True
                elif completed and argv[0] == "findmnt" and "--target" in argv and \
                        argv[argv.index("--target") + 1] == str(self.repository) and replacement == "unmounted":
                    result = json.dumps({"filesystems": [{"target": "/", "source": "/dev/active1", "uuid": "active-0000", "fstype": "ext4"}]}).encode()
                elif completed and argv[0] == "lsblk" and argv[-1] == "/dev/backup1" and replacement == "different-physical-disk":
                    document = json.loads(result)
                    document["blockdevices"][0]["children"][0]["serial"] = "replacement-backup-hardware"
                    result = json.dumps(document).encode()
                return result
            with self.subTest(replacement=replacement), self.local_context(), patch.object(backup, "run", changed):
                backup.metrics(True, "local", recovery_time=40)
                with self.assertRaisesRegex(ValueError, "mountpoint|identity changed"):
                    backup.commit_local(self.policy, self.capture, self.credentials)
                self.assertFalse((self.capture / "local-backup.json").exists())
                self.assertEqual("40", self.metric_values()["workstation_service_backup_local_last_success_seconds"])

    def test_host_policies_required_and_recursive_storage_refused(self):
        with self.local_context():
            self.assertEqual(["/etc", "/home", "/var/lib/sbctl"], backup.host_backup_policy(self.policy))
            for extra in ("/", str(self.mount), str(self.staging), str(backup.DATA)):
                self.include.write_text("/etc\n/home\n/var/lib/sbctl\n" + extra + "\n")
                with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, "overlap"):
                    backup.host_backup_policy(self.policy)
            self.include.unlink()
            with self.assertRaisesRegex(ValueError, "include/exclude"):
                backup.host_backup_policy(self.policy)

    def test_mount_and_host_policy_fail_before_lock_or_quiescence(self):
        worker = object.__new__(backup.Capture)
        worker.policy = {**self.policy, "hardware": str(self.root / "hardware.json")}
        (self.root / "boot-id.txt").write_text("boot-fixture")
        original_read, original_file = Path.read_text, Path.is_file
        def host_read(path, *args, **kwargs):
            return "boot-fixture" if str(path) == "/proc/sys/kernel/random/boot_id" else original_read(path, *args, **kwargs)
        def host_file(path):
            return True if str(path) == "/etc/arch-release" else original_file(path)
        for failing_gate in ("validate_local_repository", "host_backup_policy"):
            with self.subTest(gate=failing_gate), self.local_context(), patch.object(Path, "read_text", host_read), \
                    patch.object(Path, "is_file", host_file), patch.object(backup, "validate_local_repository"), \
                    patch.object(backup, "host_backup_policy"), patch.object(backup, "credential_files") as credentials, \
                    patch.object(backup.os, "open") as lock, patch.object(backup, "run") as mutation:
                with patch.object(backup, failing_gate, side_effect=ValueError("preflight refused")), self.assertRaisesRegex(ValueError, "preflight"):
                    worker._capture()
                lock.assert_not_called()
                mutation.assert_not_called()
                credentials.assert_not_called()

    def test_local_capture_credentials_do_not_require_remote_files(self):
        password = self.credentials / "restic_local_password"
        password.write_text("synthetic password fixture")
        password.chmod(0o400)
        with self.local_context(), patch.dict(backup.os.environ, {"CREDENTIALS_DIRECTORY": str(self.credentials)}):
            self.assertEqual(self.credentials, backup.credential_files(local=True))
            with self.assertRaisesRegex(ValueError, "credential"):
                backup.credential_files(remote=True)
            password.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "credential"):
                backup.credential_files(local=True)

    def test_local_success_remote_failure_and_exact_retry_preserve_capture(self):
        self.make_capture()
        with self.local_context(), patch.object(backup, "run", self.restic_run), \
                patch.object(backup, "credential_files", return_value=self.credentials):
            backup.commit_local(self.policy, self.capture, self.credentials)
            recorded = (self.capture / "local-backup.json").read_bytes()
            local_success = self.metric_values()["workstation_service_backup_local_last_success_seconds"]
            self.fail_copy = True
            with self.assertRaisesRegex(ValueError, "outage"):
                backup.replicate(self.policy_path, self.capture)
            values = self.metric_values()
            self.assertEqual("1", values["workstation_service_backup_offsite_last_attempt_failed"])
            self.assertEqual("0", values["workstation_service_backup_last_success_seconds"])
            self.assertEqual(local_success, values["workstation_service_backup_local_last_success_seconds"])
            self.assertEqual("0", values["workstation_service_backup_local_last_attempt_failed"])
            self.assertEqual("1", values["workstation_service_backup_local_enabled"])
            self.assertEqual(recorded, (self.capture / "local-backup.json").read_bytes())
            backup.validate_capture(self.capture)
            self.fail_copy = False
            with patch.object(backup.fcntl, "flock", wraps=backup.fcntl.flock) as locks:
                backup.replicate(self.policy_path, self.capture)
                # The only Python lock is the metrics file, never a session lock.
                self.assertEqual(1, locks.call_count)
            copies = [argv for argv, _ in self.calls if "copy" in argv]
            self.assertEqual(2, len(copies))
            self.assertEqual(copies[0], copies[1])
            self.assertEqual(1, sum("backup" in argv for argv, _ in self.calls))
            self.assertTrue((self.capture / "offsite-backup.json").is_file())
            offsite = json.loads((self.capture / "offsite-backup.json").read_text())
            self.assertEqual(self.destination_id, offsite["snapshot_id"])
            self.assertEqual(self.snapshot_id, offsite["source_snapshot_id"])
            self.assertEqual("0", self.metric_values()["workstation_service_backup_last_attempt_failed"])

    def test_idempotent_replication_retry_resolves_existing_destination_id(self):
        self.make_capture()
        with self.local_context(), patch.object(backup, "run", self.restic_run), \
                patch.object(backup, "credential_files", return_value=self.credentials):
            backup.commit_local(self.policy, self.capture, self.credentials)
            backup.replicate(self.policy_path, self.capture)
            backup.replicate(self.policy_path, self.capture)
            offsite = json.loads((self.capture / "offsite-backup.json").read_text())
            self.assertEqual(self.destination_id, offsite["snapshot_id"])
            self.assertEqual(self.snapshot_id, offsite["source_snapshot_id"])
            self.assertEqual(2, sum("copy" in argv for argv, _ in self.calls))
            self.assertEqual(1, sum("backup" in argv for argv, _ in self.calls))

    def test_missing_destination_or_manifest_mismatch_preserves_local_success(self):
        self.make_capture()
        with self.local_context(), patch.object(backup, "run", self.restic_run), \
                patch.object(backup, "credential_files", return_value=self.credentials):
            backup.commit_local(self.policy, self.capture, self.credentials)
            local_record = (self.capture / "local-backup.json").read_bytes()
            for available, valid, error in ((False, True, "destination snapshot missing"),
                                             (True, False, "destination snapshot contains")):
                self.destination_available, self.destination_manifest_valid = available, valid
                with self.subTest(available=available, valid=valid), self.assertRaisesRegex(ValueError, error):
                    backup.replicate(self.policy_path, self.capture)
                self.assertFalse((self.capture / "offsite-backup.json").exists())
                self.assertEqual(local_record, (self.capture / "local-backup.json").read_bytes())
                values = self.metric_values()
                self.assertEqual("0", values["workstation_service_backup_offsite_last_success_seconds"])
                self.assertEqual("1", values["workstation_service_backup_offsite_last_attempt_failed"])
                self.assertEqual("0", values["workstation_service_backup_local_last_attempt_failed"])
                backup.validate_capture(self.capture)

    def test_chunker_mismatch_never_uploads(self):
        self.make_capture()
        self.polynomial = "different"
        with self.local_context(), patch.object(backup, "run", self.restic_run), \
                patch.object(backup, "credential_files", return_value=self.credentials):
            backup.commit_local(self.policy, self.capture, self.credentials)
            with self.assertRaisesRegex(ValueError, "chunker"):
                backup.replicate(self.policy_path, self.capture)
        self.assertFalse(any("copy" in argv for argv, _ in self.calls))

    def test_tampered_or_missing_snapshot_never_uploads(self):
        self.make_capture()
        with self.local_context(), patch.object(backup, "run", self.restic_run):
            backup.commit_local(self.policy, self.capture, self.credentials)
        for response in ([], [{"id": "b" * 64}], [{"id": self.snapshot_id, "paths": [], "tags": []}]):
            def unsafe(argv, **kwargs):
                return json.dumps(response).encode() if "snapshots" in argv else self.restic_run(argv, **kwargs)
            with self.subTest(response=response), self.local_context(), patch.object(backup, "run", unsafe), \
                    patch.object(backup, "credential_files", return_value=self.credentials), self.assertRaisesRegex(ValueError, "snapshot"):
                backup.replicate(self.policy_path, self.capture)
        self.assertFalse(any("copy" in argv for argv, _ in self.calls))

    def test_snapshot_manifest_join_rejects_wrong_committed_capture(self):
        self.make_capture()
        with self.local_context(), patch.object(backup, "run", self.restic_run):
            backup.commit_local(self.policy, self.capture, self.credentials)
        def unsafe(argv, **kwargs):
            return b"different manifest" if "dump" in argv else self.restic_run(argv, **kwargs)
        with self.local_context(), patch.object(backup, "run", unsafe), \
                patch.object(backup, "credential_files", return_value=self.credentials), self.assertRaisesRegex(ValueError, "manifest"):
            backup.replicate(self.policy_path, self.capture)
        self.assertFalse(any("copy" in argv for argv, _ in self.calls))

    def test_missing_successful_snapshot_id_cannot_be_replicated(self):
        self.make_capture()
        with self.local_context(), patch.object(backup, "run", self.restic_run), self.assertRaises(ValueError):
            backup.replicate(self.policy_path, self.capture)
        self.assertFalse(self.calls)

    def test_metrics_keep_other_stage_and_legacy_offsite_semantics(self):
        with self.local_context(), patch.object(backup.time, "time", return_value=100):
            backup.metrics(True, recovery_time=100)
            backup.metrics(True, "local", recovery_time=100)
            backup.metrics(False)
            backup.metrics(False, "local")
        values = self.metric_values()
        self.assertEqual("100", values["workstation_service_backup_last_success_seconds"])
        self.assertEqual("100", values["workstation_service_backup_local_last_success_seconds"])
        self.assertEqual("1", values["workstation_service_backup_local_last_attempt_failed"])
        self.assertEqual("1", values["workstation_service_backup_offsite_last_attempt_failed"])

    def test_local_metric_migration_preserves_legacy_remote_failure(self):
        (self.telemetry / "service-backup.prom").write_text(
            "workstation_service_backup_last_success_seconds 90\nworkstation_service_backup_last_attempt_failed 1\n")
        with self.local_context(), patch.object(backup.time, "time", return_value=100):
            backup.metrics(True, "local", recovery_time=100)
        values = self.metric_values()
        self.assertEqual("90", values["workstation_service_backup_offsite_last_success_seconds"])
        self.assertEqual("1", values["workstation_service_backup_offsite_last_attempt_failed"])
        self.assertEqual("1", values["workstation_service_backup_last_attempt_failed"])

    def test_old_capture_retries_do_not_refresh_recovery_freshness(self):
        self.make_capture()
        with self.local_context(), patch.object(backup, "run", self.restic_run), \
                patch.object(backup, "credential_files", return_value=self.credentials), \
                patch.object(backup.time, "time", return_value=604850):
            backup.commit_local(self.policy, self.capture, self.credentials)
            backup.replicate(self.policy_path, self.capture)
            backup.replicate(self.policy_path, self.capture)
            values = self.metric_values()
            self.assertEqual("50", values["workstation_service_backup_local_last_success_seconds"])
            self.assertEqual("50", values["workstation_service_backup_offsite_last_success_seconds"])
            offsite = json.loads((self.capture / "offsite-backup.json").read_text())
            self.assertEqual(604850, offsite["completed_at"])
            backup.metrics(True, "offsite", recovery_time=100)
            backup.replicate(self.policy_path, self.capture)
            self.assertEqual("100", self.metric_values()["workstation_service_backup_offsite_last_success_seconds"])

    def test_successful_unit_result_does_not_freshen_or_clear_failure(self):
        with self.local_context(), patch.object(backup.time, "time", return_value=100):
            backup.metrics(True, "local", recovery_time=50)
            backup.metrics(False, "local")
            before = (self.telemetry / "service-backup.prom").read_bytes()
            with patch.dict(backup.os.environ, {"SERVICE_RESULT": "success"}), \
                    patch.object(sys, "argv", ["service_backup.py", "record-result", "--stage", "local"]):
                self.assertEqual(0, backup.main())
            self.assertEqual(before, (self.telemetry / "service-backup.prom").read_bytes())

    def test_failed_unit_result_and_capture_read_preserve_recovery_point(self):
        self.make_capture()
        with self.local_context(), patch.object(backup.time, "time", return_value=100):
            backup.metrics(True, "offsite", recovery_time=50)
            with patch.dict(backup.os.environ, {"SERVICE_RESULT": "exit-code"}), \
                    patch.object(sys, "argv", ["service_backup.py", "record-result"]):
                self.assertEqual(0, backup.main())
            self.assertEqual("50", self.metric_values()["workstation_service_backup_offsite_last_success_seconds"])
            self.assertEqual("1", self.metric_values()["workstation_service_backup_offsite_last_attempt_failed"])
            (self.capture / "manifest.json").write_text("invalid capture JSON")
            with self.assertRaises(ValueError):
                backup.replicate(self.policy_path, self.capture)
            self.assertEqual("50", self.metric_values()["workstation_service_backup_offsite_last_success_seconds"])
            self.assertEqual("1", self.metric_values()["workstation_service_backup_offsite_last_attempt_failed"])

    def test_recovery_timestamp_types_and_future_bounds_are_checked(self):
        self.make_capture()
        record = json.loads((self.capture / "manifest.json").read_text())
        with patch.object(backup.time, "time", return_value=100):
            for value in (True, "50", 0, -1, 401):
                record["created_at"] = value
                (self.capture / "manifest.json").write_text(json.dumps(record))
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, "timestamp"):
                    backup.validate_capture(self.capture)
            record.pop("created_at")
            (self.capture / "manifest.json").write_text(json.dumps(record))
            backup.validate_capture(self.capture)  # Existing isolated restore fixtures remain accepted.
            with self.assertRaisesRegex(ValueError, "timestamp"):
                backup.commit_local(self.policy, self.capture, self.credentials)

    def test_gaming_archives_preserve_proton_saves_and_pairing_on_isolated_stage(self):
        self.make_capture(backup.GAMING_HOMES)
        destination = self.root / "restore"
        backup.restore_stage(self.capture, destination)
        record = backup.validate_capture(destination)
        self.assertEqual(set(backup.GAMING_HOMES), set(record["gaming_claims"]))
        for claim in backup.GAMING_HOMES:
            with tarfile.open(destination / (claim + ".tar")) as archive:
                names = archive.getnames()
                self.assertTrue(any("compatdata/42/pfx/" in name and name.endswith("save.dat") for name in names))
                self.assertIn("./.config/sunshine/state.json", names)

    def test_gaming_archive_traversal_or_damage_refused(self):
        self.make_capture(["gaming-home"])
        for name in ("../escape", "/absolute"):
            with tarfile.open(self.capture / "gaming-home.tar", "w") as archive:
                member = tarfile.TarInfo(name)
                member.size = 1
                archive.addfile(member, io.BytesIO(b"x"))
            record = json.loads((self.capture / "manifest.json").read_text())
            record["files"]["gaming-home.tar"] = backup.digest(self.capture / "gaming-home.tar")
            (self.capture / "manifest.json").write_text(json.dumps(record))
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "gaming home"):
                backup.validate_capture(self.capture)


class GamingPlanTests(unittest.TestCase):
    def setUp(self):
        BackupTests.setUp(self)
        self.worker = object.__new__(backup.Capture)
        self.worker.baseline = True
        self.worker.kube = ["fixture-kubectl"]
        self.data = self.root / "k3s"
        self.home = self.data / "storage/home"
        self.home.mkdir(parents=True)
        self.claim = {"metadata": {"name": "parent-gaming-home", "uid": "pvc-1"},
                      "status": {"phase": "Bound"}, "spec": {"volumeName": "pv-1"}}
        self.pv = {"metadata": {"name": "pv-1", "uid": "pv-uid-1"}, "spec": {
            "persistentVolumeReclaimPolicy": "Retain", "claimRef": {"uid": "pvc-1", "namespace": backup.NAMESPACE,
            "name": "parent-gaming-home"}, "local": {"path": str(self.home)}, "nodeAffinity": {"required": {
            "nodeSelectorTerms": [{"matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "In", "values": ["node-1"]}]}]}}}}
        self.node = {"metadata": {"labels": {"kubernetes.io/hostname": "node-1"}}}
        self.pod_spec = {"volumes": [{"persistentVolumeClaim": {"claimName": "parent-gaming-home"}}]}
        self.deployment = {"metadata": {"name": "parent-steam-headless", "uid": "deployment-1"},
                           "spec": {"replicas": 1, "template": {"spec": self.pod_spec}}}
        self.replica = {"metadata": {"uid": "rs-1", "ownerReferences": [{"controller": True, "kind": "Deployment", "uid": "deployment-1"}]}}
        self.pod = {"metadata": {"ownerReferences": [{"controller": True, "kind": "ReplicaSet", "uid": "rs-1"}]}, "spec": self.pod_spec}
        self.deployments, self.pods = [self.deployment], [self.pod]
        def get(*args):
            if "pvc" in args:
                return {"items": [self.claim]}
            if "pv" in args:
                return self.pv
            if "deployments" in args:
                return {"items": self.deployments}
            if "replicasets" in args:
                return {"items": [self.replica]}
            if "pods" in args:
                return {"items": self.pods}
            self.fail("unexpected kubernetes query")
        self.worker.get = get

    def test_known_claim_checks_uid_node_retention_path_and_unknown_names(self):
        with patch.object(backup, "DATA", self.data):
            volumes = self.worker.volumes(self.node)
            self.assertEqual("pv-uid-1", volumes["parent-gaming-home"]["pv_uid"])
            original = copy.deepcopy(self.pv)
            for change in (lambda: self.pv["spec"].update(persistentVolumeReclaimPolicy="Delete"),
                           lambda: self.pv["spec"]["claimRef"].update(uid="replacement"),
                           lambda: self.pv["spec"]["local"].update(path=str(self.root)),
                           lambda: self.pv["spec"]["nodeAffinity"]["required"]["nodeSelectorTerms"][0]["matchExpressions"][0].update(values=["other-node"])):
                self.pv = copy.deepcopy(original)
                change()
                with self.assertRaises(ValueError):
                    self.worker.volumes(self.node)
            self.claim["metadata"]["name"] = "visitor-gaming-home"
            with self.assertRaisesRegex(ValueError, "unknown gaming-home"):
                self.worker.volumes(self.node)

    def test_known_gaming_writers_record_original_identity_before_maintenance(self):
        volumes = {"parent-gaming-home": {"source": str(self.home)}}
        self.assertEqual(("parent-steam-headless",), self.worker.gaming_writers_plan(volumes))
        self.assertEqual({"parent-steam-headless": {"uid": "deployment-1", "replicas": 1}}, self.worker.gaming_journal)

    def test_unmanaged_deployment_and_pod_writers_refused(self):
        volumes = {"parent-gaming-home": {"source": str(self.home)}}
        self.deployment["metadata"]["name"] = "unmanaged"
        with self.assertRaisesRegex(ValueError, "unmanaged gaming-home deployment"):
            self.worker.gaming_writers_plan(volumes)
        self.deployment["metadata"]["name"] = "parent-steam-headless"
        self.pod["metadata"]["ownerReferences"] = []
        with self.assertRaisesRegex(ValueError, "unmanaged gaming-home Pod"):
            self.worker.gaming_writers_plan(volumes)

    def test_hostpath_bypass_refused(self):
        self.pods.append({"metadata": {}, "spec": {"volumes": [{"hostPath": {"path": str(self.data)}}]}})
        with self.assertRaisesRegex(ValueError, "hostPath"):
            self.worker.gaming_writers_plan({"parent-gaming-home": {"source": str(self.home)}})

    def test_quiesce_preserves_premaintenance_replicas_and_uid(self):
        document = {"metadata": {"uid": "deployment-1", "resourceVersion": "9"}, "spec": {
            "replicas": 0, "selector": {"matchLabels": {"app": "gaming"}}}}
        self.worker.get = lambda *args: {"items": []} if "pods" in args else document
        journal = {"parent-steam-headless": {"uid": "deployment-1", "replicas": 1}}
        with patch.object(backup, "run", return_value=b""):
            self.worker.scale_zero("parent-steam-headless", journal, self.root)
        self.assertEqual(1, journal["parent-steam-headless"]["replicas"])
        document["metadata"]["uid"] = "replacement"
        with patch.object(backup, "run") as mutation, self.assertRaisesRegex(ValueError, "replaced"):
            self.worker.scale_zero("parent-steam-headless", journal, self.root)
        mutation.assert_not_called()

    def test_gaming_quiesces_before_application_database(self):
        self.worker.baseline = False
        self.worker.gaming_writers = ("parent-steam-headless", "kids-steam-headless")
        events = []
        self.worker.scale_zero = lambda name, *args: events.append(name)
        self.worker.stopped = lambda: events.append("quiescent")
        self.worker.get = lambda *args: {"items": [{"metadata": {"name": "pg"}, "status": {"phase": "Running"}}]}
        def dump(argv, output, **kwargs):
            events.append(argv[argv.index("--") + 1])
            output.write(b"dump")
        with patch.object(backup, "run", dump):
            self.worker.quiesce_apps(self.root, {})
        self.assertEqual(["parent-steam-headless", "kids-steam-headless", "open-webui", "service-tools", "litellm",
                          "pg_dump", "pg_dumpall", "gateway-postgres", "quiescent"], events)


if __name__ == "__main__":
    unittest.main()
