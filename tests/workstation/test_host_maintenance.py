"""Host maintenance reports: offline fixtures only (no pacman, disks, rasdaemon or cluster)."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "infrastructure/ansible/roles/host_maintenance"
spec = importlib.util.spec_from_file_location("workstation_maintenance", ROLE / "files/workstation_maintenance.py")
wm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wm)


def simple_vercmp(left, right):
    def key(version):
        return [int(part) if part.isdigit() else part for part in version.replace("-", ".").split(".")]
    return (key(left) > key(right)) - (key(left) < key(right))


class Completed:
    def __init__(self, stdout="", returncode=0):
        self.stdout, self.returncode = stdout, returncode


class TextfileTests(unittest.TestCase):
    def test_validation_accepts_only_the_collectors_own_bounded_series(self):
        m = wm.Metrics()
        m.add("workstation_nvme_temperature_celsius", 47, {"device": "nvme0", "serial": 'A"B\\C'})
        text = wm.validate_textfile("health", m.text())
        self.assertIn('serial="A_B_C"', text)
        for bad in ('node_memory_MemTotal_bytes 1\n', 'workstation_nvme_x{a="b"} 1\nrm -rf /\n',
                    'workstation_retention_x 1\n'):
            with self.assertRaises(wm.Failure):
                wm.validate_textfile("health", bad)
        with self.assertRaises(wm.Failure):
            wm.validate_textfile("unknown", m.text())

    def test_publish_refuses_symlinks_and_writes_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            out = tmp / "textfile"
            out.mkdir()
            source = tmp / "health.prom"
            source.write_text("workstation_nvme_media_errors 0\n")
            wm.publish("health", source, out)
            self.assertEqual((out / "maintenance-health.prom").read_text(), "workstation_nvme_media_errors 0\n")
            link = tmp / "link.prom"
            link.symlink_to(source)
            with self.assertRaises(OSError):
                wm.publish("health", link, out)


class AdvisoryTests(unittest.TestCase):
    groups = [{"name": f"AVG-{i}", "packages": [f"filler{i}"], "status": "Fixed", "severity": "Low",
               "type": "x", "affected": "1-1", "fixed": "2-1", "issues": []} for i in range(600)] + [
        {"name": "AVG-1", "packages": ["pam"], "status": "Vulnerable", "severity": "High", "type": "fs",
         "affected": "1.7.0-2", "fixed": None, "issues": ["CVE-2025-6020"]},
        {"name": "AVG-2", "packages": ["openssl"], "status": "Fixed", "severity": "Medium", "type": "rce",
         "affected": "3.5.0-1", "fixed": "3.5.1-1", "issues": ["CVE-2025-4575"]},
        {"name": "AVG-3", "packages": ["curl"], "status": "Fixed", "severity": "Critical", "type": "rce",
         "affected": "8.0-1", "fixed": "8.9-1", "issues": ["CVE-0"]},
        {"name": "AVG-4", "packages": ["zlib"], "status": "Not affected", "severity": "High", "type": "x",
         "affected": "1-1", "fixed": None, "issues": []},
    ]
    installed = dict({f"pkg{i}": "1-1" for i in range(60)}, pam="1.7.2-2", openssl="3.5.0-1", curl="8.0-1",
                     zlib="1.3-1", **{"linux-git": "7.3.3-3"})

    def scan(self, tmp, audit, tracker_ok=True, configured=None):
        state = Path(tmp) / "state"
        tracker = Path(tmp) / "tracker.json"
        tracker.write_text(json.dumps(self.groups))
        sync = Path(tmp) / "sync"
        sync.mkdir(exist_ok=True)
        (sync / "core.db").write_text("")
        (sync / "extra.db").write_text("")

        def fake_run(argv, timeout=120, check=True, env=None):
            if argv[:2] == ["pacman", "-Qmq"]:
                return Completed("linux-git\n")
            if argv[0] == "arch-audit":
                if not tracker_ok:
                    raise wm.Failure("arch-audit exited 1")
                self.assertIn("--show-testing", argv)
                return Completed(json.dumps([{"packages": [p]} for p in audit]))
            raise AssertionError(argv)

        args = mock.Mock(state=str(state), tracker=str(tracker), sync_dir=str(sync))
        with mock.patch.object(wm, "run", fake_run), mock.patch.object(wm, "vercmp", simple_vercmp), \
                mock.patch.object(wm, "installed_packages", return_value=self.installed), \
                mock.patch.object(wm, "sync_versions", side_effect=[configured or {"openssl": "3.5.1-1", "curl": "8.0-1",
                                                                                   "pam": "1.7.2-2"},
                                                                    {"pam": "1.7.3-1", "curl": "8.9-1"}]), \
                mock.patch.object(wm, "refresh_current_db", return_value=("db", "conf")):
            with mock.patch.object(wm, "run", side_effect=lambda argv, **kw: (
                    Completed("a 1 -> 2\nb 1 -> 2\n") if "-Qu" in argv else fake_run(argv, **kw))):
                code = wm.advisories(args)
        return code, json.loads((state / "advisories.json").read_text()), (state / "advisories.prom").read_text()

    def test_classifies_fix_status_and_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, report, prom = self.scan(tmp, ["pam", "openssl", "curl"])
        self.assertEqual(code, 0)
        classes = {f["package"]: f["class"] for f in report["findings"]}
        self.assertEqual(classes, {"pam": "no_fix_recorded", "openssl": "fix_in_configured_repo",
                                   "curl": "fix_needs_snapshot_move"})
        pam = next(f for f in report["findings"] if f["package"] == "pam")
        self.assertTrue(pam["newer_in_current_arch"])
        self.assertEqual(report["uncovered"], ["linux-git"])
        self.assertIn('workstation_advisory_findings{class="no_fix_recorded",severity="high"} 1', prom)
        self.assertIn('workstation_advisory_findings{class="fix_needs_snapshot_move",severity="critical"} 1', prom)
        self.assertIn('workstation_advisory_uncovered_package_info{package="linux-git"} 1', prom)
        self.assertIn("workstation_update_pending_packages 2", prom)
        self.assertIn("workstation_advisory_last_run_success 1", prom)

    def test_disagreement_or_scanner_failure_is_never_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.scan(tmp, ["pam", "openssl", "curl"])
            code, report, prom = self.scan(tmp, [])  # empty scanner output is not "clean"
            self.assertEqual(code, 1)
            self.assertFalse(report["success"])
            self.assertIn("workstation_advisory_last_run_success 0", prom)
            # Last good findings remain, with their own (older) success time.
            self.assertEqual(len(report["findings"]), 3)
            code, report, prom = self.scan(tmp, [], tracker_ok=False)
            self.assertEqual(code, 1)
            self.assertIn("arch-audit exited 1", report["reason"])


class HealthTests(unittest.TestCase):
    smart = {"serial_number": "S7TEST", "model_name": "Samsung SSD 9100 PRO 1TB", "firmware_version": "X",
             "smart_status": {"passed": True},
             "nvme_composite_temperature_threshold": {"warning": 84, "critical": 88},
             "nvme_smart_health_information_log": {"critical_warning": 0, "temperature": 47, "available_spare": 100,
                                                   "available_spare_threshold": 10, "percentage_used": 0,
                                                   "media_errors": 0, "num_err_log_entries": 0,
                                                   "unsafe_shutdowns": 33, "warning_temp_time": 0,
                                                   "critical_comp_time": 0, "power_on_hours": 50,
                                                   "data_units_written": 5}}

    def test_smart_ras_and_backup_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "ras.db"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE mc_event (id INTEGER, err_type TEXT)")
                connection.executemany("INSERT INTO mc_event VALUES (?, ?)",
                                       [(1, "Corrected"), (2, "Uncorrected"), (3, "Corrected")])
                connection.execute("CREATE TABLE aer_event (id INTEGER, err_type TEXT)")
                connection.execute("CREATE TABLE mce_record (id INTEGER, status INTEGER)")
                # MCi_STATUS with VAL (63, so negative as SQLite INTEGER) and UC (61) set, and a corrected one.
                connection.executemany("INSERT INTO mce_record VALUES (?, ?)",
                                       [(1, (1 << 63 | 1 << 61) - (1 << 64)), (2, (1 << 63) - (1 << 64))])
                connection.execute("CREATE TABLE non_standard_event (id INTEGER, severity TEXT)")
                connection.execute("CREATE TABLE memory_failure_event (id INTEGER, pfn TEXT)")

            def fake_run(argv, timeout=120, check=True, env=None):
                if argv[0] == "smartctl":
                    if argv[-1].endswith("bad"):
                        return Completed("not json", 2)
                    return Completed(json.dumps(self.smart))
                if argv[:2] == ["systemctl", "is-active"]:
                    return Completed("active\n" if argv[2] == "smartd.service" else "inactive\n", 0)
                if argv[:2] == ["systemctl", "is-enabled"]:
                    return Completed("enabled\n" if argv[2] == "smartd.service" else "disabled\n", 0)
                raise AssertionError(argv)

            args = mock.Mock(state=tmp, device=["/dev/disk/by-id/nvme-good"], ras_db=str(database),
                             unit=["smartd.service", "rasdaemon.service"])
            with mock.patch.object(wm, "run", fake_run):
                self.assertEqual(wm.health(args), 0)
                text = (Path(tmp) / "health.prom").read_text()
                args.device.append("/dev/disk/by-id/nvme-bad")
                self.assertEqual(wm.health(args), 1)
        wm.validate_textfile("health", text)
        self.assertIn('workstation_nvme_temperature_celsius{device="unknown",serial="S7TEST"} 47', text)
        self.assertIn('workstation_ras_events_recorded{table="mc_event",severity="uncorrected"} 1', text)
        self.assertIn('workstation_ras_events_recorded{table="mc_event",severity="corrected"} 2', text)
        self.assertIn('workstation_ras_events_recorded{table="aer_event",severity="all"} 0', text)
        self.assertIn('workstation_ras_events_recorded{table="aer_event",severity="fatal"} 0', text)
        self.assertIn('workstation_ras_events_recorded{table="mce_record",severity="uncorrected"} 1', text)
        self.assertIn('workstation_ras_events_recorded{table="mce_record",severity="corrected"} 1', text)
        self.assertIn('workstation_ras_events_recorded{table="non_standard_event",severity="fatal"} 0', text)
        self.assertIn('workstation_ras_events_recorded{table="memory_failure_event",severity="all"} 0', text)
        self.assertNotIn('table="memory_failure_event",severity="fatal"', text)
        self.assertIn("workstation_ras_database_readable 1", text)
        self.assertIn('workstation_maintenance_service_active{unit="rasdaemon.service"} 0', text)
        self.assertIn('workstation_maintenance_backup_configured{kind="service"} 0', text)


class RetentionTests(unittest.TestCase):
    def tree(self, tmp):
        root = Path(tmp) / "ai-stack"
        builds = root / "builds"
        lock = {"THEROCK_COMMIT": "r" * 40, "PYTORCH_COMMIT": "p" * 40, "SGLANG_COMMIT": "s" * 40,
                "SGLANG_PATCHSET_SHA256": "a" * 64, "COMFYUI_COMMIT": "c" * 40, "SWARMUI_COMMIT": "w" * 40,
                "RUNTIME_BASE_IMAGE": "docker.io/library/python@sha256:" + "b" * 64}
        (root / "pipeline/current").mkdir(parents=True)
        (root / "pipeline/current/stack.lock").write_text("\n".join(f"{k}={v}" for k, v in lock.items()) + "\n")

        def run_dir(name, receipt=None, digest=None, age=0):
            path = builds / name
            (path / "out").mkdir(parents=True)
            if receipt is not None:
                (path / "out/RECEIPT.json").write_text(json.dumps(receipt))
            if digest:
                (path / "out/image.digest").write_text(digest + "\n")
            stamp = 1_800_000_000 - age
            os.utime(path, (stamp, stamp))
            return path

        torch = run_dir("torch-t1", {"therock_commit": "r" * 40, "pytorch_commit": "p" * 40})
        torch_sha = wm.sha256_file(torch / "out/RECEIPT.json")
        run_dir("rocm-r1", {"therock_commit": "r" * 40})
        run_dir("sglang-current", {"sglang_commit": "s" * 40, "sglang_patchset_sha256": "a" * 64,
                                   "torch_receipt_sha256": torch_sha}, age=500)
        run_dir("sglang-newest", {"sglang_commit": "x" * 40}, age=10)
        run_dir("sglang-old", {"sglang_commit": "y" * 40}, age=900_000)
        run_dir("sglang-failed", None, age=900_000)
        deployed, rollback, published, unused = ("sha256:" + c * 64 for c in "1234")
        for key, digest in (("img1", deployed), ("img2", rollback), ("img3", published), ("img4", unused)):
            run_dir(f"image-{key}", digest=digest)
        storage = Path(tmp) / "storage"
        (storage / "overlay-images").mkdir(parents=True)
        (storage / "overlay-layers").mkdir(parents=True)
        layers = [{"id": "base", "diff-size": 100}] + [
            {"id": f"l{k}", "parent": "base", "diff-size": 1000 * k} for k in range(1, 6)]
        images = [{"id": f"{k}" * 64, "names": [f"registry.lan.thespry.family/local/sglang-gfx1201:img{k}"],
                   "layer": f"l{k}", "created": f"2026-11-0{k}T00:00:00Z"} for k in range(1, 5)]
        images.append({"id": "9" * 64, "names": ["registry.lan.thespry.family/local/unpublished:x"], "layer": "l5",
                       "created": "2026-11-01T00:00:00Z"})
        (storage / "overlay-layers/layers.json").write_text(json.dumps(layers))
        (storage / "overlay-images/images.json").write_text(json.dumps(images))
        return root, storage, deployed, rollback

    def kubectl(self, deployed, rollback):
        items = [
            {"kind": "Deployment", "metadata": {"namespace": "ai", "name": "sglang"},
             "spec": {"template": {"spec": {"containers": [{"image": "r/sglang@" + deployed}]}}}},
            {"kind": "ReplicaSet", "metadata": {"namespace": "ai", "name": "a", "ownerReferences": [
                {"kind": "Deployment", "name": "sglang"}], "annotations": {"deployment.kubernetes.io/revision": "2"}},
             "spec": {"replicas": 1, "template": {"spec": {"containers": [{"image": "r/sglang@" + deployed}]}}}},
            {"kind": "ReplicaSet", "metadata": {"namespace": "ai", "name": "b", "ownerReferences": [
                {"kind": "Deployment", "name": "sglang"}], "annotations": {"deployment.kubernetes.io/revision": "1"}},
             "spec": {"replicas": 0, "template": {"spec": {"containers": [{"image": "r/sglang@" + rollback}]}}}},
        ]
        return Completed(json.dumps({"items": items}))

    def report(self, tmp, kube_ok=True):
        root, storage, deployed, rollback = self.tree(tmp)
        real_run = wm.run

        def fake_run(argv, timeout=120, check=True, env=None):
            if argv[0] == "kubectl":
                if not kube_ok:
                    raise wm.Failure("kubectl exited 1")
                return self.kubectl(deployed, rollback)
            if argv[0] == "du":
                return Completed("".join(f"{len(p) * 1024}\t{p}\n" for p in argv[5:]))
            return real_run(argv, timeout=timeout, check=check)

        args = mock.Mock(state=str(Path(tmp) / "state"), ai_stack_root=str(root), pipeline=str(root / "pipeline/current"),
                         storage=str(storage), ai_stack_metrics=str(Path(tmp) / "none"), kubectl="kubectl", keep_sglang=2)
        with mock.patch.object(wm, "run", fake_run), mock.patch.object(wm.time, "time", return_value=1_800_000_000):
            code = wm.retention(args)
        report = json.loads((Path(tmp) / "state/retention.json").read_text())
        return code, report, root

    def test_candidates_exclude_deployed_rollback_current_and_unpublished(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, report, root = self.report(tmp)
            self.assertTrue((root / "builds/sglang-old").is_dir())  # report only: nothing removed
        self.assertEqual(code, 0)
        self.assertFalse(report["deletion_enabled"])
        runs = {Path(r["path"]).name: r["decision"] for r in report["runs"]}
        self.assertEqual(runs["sglang-current"], "keep")
        self.assertEqual(runs["sglang-newest"], "keep")
        self.assertEqual(runs["sglang-old"], "candidate")
        self.assertEqual(runs["sglang-failed"], "candidate")
        self.assertEqual(runs["rocm-r1"], "keep")
        images = {row["names"][0].rsplit(":", 1)[1]: row["decision"] for row in report["images"]}
        self.assertEqual(images, {"img1": "keep", "img2": "keep", "img3": "candidate", "img4": "keep", "x": "review"})
        self.assertEqual(report["image_totals"]["candidate_bytes"], 3000)

    def test_incomplete_inventory_proposes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, report, _ = self.report(tmp, kube_ok=False)
        self.assertEqual(code, 1)
        self.assertFalse(report["inputs"]["kubernetes"])
        self.assertFalse(any(r["decision"] == "candidate" for r in report["runs"] + report["images"]))


class JobResultTests(unittest.TestCase):
    def test_records_failure_and_keeps_last_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            textfile = tmp / "textfile"
            textfile.mkdir()
            events = []
            with mock.patch.object(wm, "TEXTFILE_DIR", textfile), \
                    mock.patch.object(wm, "JOB_STATE", tmp / "state/jobs.json"), \
                    mock.patch.object(wm, "log_event", events.append), \
                    mock.patch.object(wm, "unit_usage", return_value={"CPUUsageNSec": 2_500_000_000, "MemoryPeak": 10}):
                with mock.patch.dict(os.environ, {"SERVICE_RESULT": "success"}):
                    wm.job_result(mock.Mock(job="paccache", unit="paccache.service", publish=[]))
                with mock.patch.dict(os.environ, {"SERVICE_RESULT": "exit-code"}):
                    wm.job_result(mock.Mock(job="paccache", unit="paccache.service", publish=["health=/missing"]))
            text = (textfile / "maintenance-jobs.prom").read_text()
            state = json.loads((tmp / "state/jobs.json").read_text())
        wm.validate_textfile("jobs", text)
        self.assertIn('workstation_maintenance_job_last_result{task="paccache"} 0', text)
        self.assertIn('workstation_maintenance_job_cpu_seconds{task="paccache"} 2.5', text)
        self.assertGreater(state["paccache"]["last_success"], 0)
        self.assertEqual([e["result"] for e in events], ["success", "failure"])
        self.assertTrue(all(set(e) == {"schema", "job", "result", "service_result"} for e in events))


if __name__ == "__main__":
    unittest.main()
