#!/usr/bin/env python3
"""Portable structural checks, not proof that reviewed source is trustworthy."""

import hashlib
import importlib.util
import io
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("bridge_source", ROOT / "infrastructure/iso/docker/bridge-source.py")
SOURCE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SOURCE)
ARCHIVE = "spry-bridge-0.0.0-src.tar.gz"
PREFIX = "spry-ai-workstation-bridge-0.0.0/"


def entries():
    files = {name: b"fixture\n" for name in SOURCE.FILES}
    files[".go-version"] = b"1.27.1\n"
    files.update({name + "/fixture": b"fixture\n" for name in SOURCE.ROOTS})
    files["cmd/bridge-arch-package/main.go"] = b"package main\n"
    files["packaging/arch/PKGBUILD.in"] = b"fixture /source.sha256\n"
    return [(PREFIX + name, content, 0o644, tarfile.REGTYPE, "") for name, content in sorted(files.items())]


def seal(bundle, content=None):
    bundle.mkdir(exist_ok=True)
    with tarfile.open(bundle / ARCHIVE, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        for name, data, mode, kind, link in entries() if content is None else content:
            header = tarfile.TarInfo(name)
            header.size, header.mode, header.type, header.linkname = len(data), mode, kind, link
            archive.addfile(header, io.BytesIO(data))
    (bundle / "PKGBUILD").write_text("# reviewed synthetic recipe\n")
    return reseal(bundle)


def reseal(bundle):
    digest = hashlib.sha256((bundle / ARCHIVE).read_bytes()).hexdigest()
    recipe = hashlib.sha256((bundle / "PKGBUILD").read_bytes()).hexdigest()
    (bundle / "SHA256SUMS").write_text(f"{digest}  {ARCHIVE}\n{recipe}  PKGBUILD\n")
    return digest


class BridgeSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = self.root / "bundle"
        self.digest = seal(self.bundle)

    def validate(self, digest=None, destination=None):
        SOURCE.validate(self.bundle, digest or self.digest, "v0.0.0", destination)

    def test_valid_exact_bundle_extracts_once_and_preserves_modes(self):
        content = entries() + [(PREFIX + "scripts/executable.sh", b"exit 0\n", 0o755, tarfile.REGTYPE, "")]
        self.digest = seal(self.bundle, content)
        self.validate()
        destination = self.root / "checkout"
        self.validate(destination=destination)
        self.assertEqual((destination / ".go-version").read_bytes(), b"1.27.1\n")
        self.assertEqual((destination / "scripts/executable.sh").stat().st_mode & 0o7777, 0o755)
        with self.assertRaises(FileExistsError):
            self.validate(destination=destination)

    def test_exact_manifest_digest_and_bundle_membership(self):
        for change in ("digest", "recipe", "manifest", "extra", "symlink", "hardlink", "fifo"):
            with self.subTest(change=change):
                candidate = self.root / change
                digest = seal(candidate)
                if change == "digest":
                    digest = "0" * 64
                elif change == "recipe":
                    (candidate / "PKGBUILD").write_text("changed")
                elif change == "manifest":
                    (candidate / "SHA256SUMS").write_text("\n" + (candidate / "SHA256SUMS").read_text())
                elif change == "extra":
                    (candidate / "unexpected").write_text("data")
                elif change == "symlink":
                    (candidate / "PKGBUILD").unlink()
                    (candidate / "PKGBUILD").symlink_to(self.bundle / "PKGBUILD")
                elif change == "hardlink":
                    (self.root / "linked-recipe").hardlink_to(candidate / "PKGBUILD")
                else:
                    (candidate / "PKGBUILD").unlink()
                    os.mkfifo(candidate / "PKGBUILD")
                with self.assertRaises((ValueError, OSError)):
                    SOURCE.validate(candidate, digest, "v0.0.0", self.root / (change + "-output"))
                self.assertFalse((self.root / (change + "-output")).exists())

    def test_unsafe_archive_is_rejected_before_any_extraction(self):
        bad = [
            ("../escape", b"bad", 0o644, tarfile.REGTYPE, ""),
            (PREFIX + "internal/../escape", b"bad", 0o644, tarfile.REGTYPE, ""),
            (PREFIX + "internal//double", b"bad", 0o644, tarfile.REGTYPE, ""),
            (PREFIX + "internal/./dot", b"bad", 0o644, tarfile.REGTYPE, ""),
            ("/" + PREFIX + "internal/absolute", b"bad", 0o644, tarfile.REGTYPE, ""),
            (PREFIX + "internal/.git/config", b"bad", 0o644, tarfile.REGTYPE, ""),
            (PREFIX + "unknown/file", b"bad", 0o644, tarfile.REGTYPE, ""),
            (PREFIX + "internal/new name", b"bad", 0o644, tarfile.REGTYPE, ""),
            (PREFIX + "internal/é", b"bad", 0o644, tarfile.REGTYPE, ""),
            (PREFIX + "internal/link", b"", 0o644, tarfile.SYMTYPE, "../../outside"),
            (PREFIX + "internal/link", b"", 0o644, tarfile.LNKTYPE, PREFIX + ".go-version"),
            (PREFIX + "internal/fifo", b"", 0o644, tarfile.FIFOTYPE, ""),
            (PREFIX + "internal/device", b"", 0o644, tarfile.CHRTYPE, ""),
            (PREFIX + "internal/sparse", b"", 0o644, tarfile.GNUTYPE_SPARSE, ""),
            (PREFIX + "internal/directory", b"", 0o755, tarfile.DIRTYPE, ""),
            (PREFIX + "internal/suid", b"bad", 0o4755, tarfile.REGTYPE, ""),
            (PREFIX + "internal/sgid", b"bad", 0o2755, tarfile.REGTYPE, ""),
            (PREFIX + "internal/writable", b"bad", 0o666, tarfile.REGTYPE, ""),
            entries()[0],
            (PREFIX + "internal/fixture/child", b"bad", 0o644, tarfile.REGTYPE, ""),
        ]
        for item in bad:
            with self.subTest(item=item):
                self.digest = seal(self.bundle, entries() + [item])
                destination = self.root / "rejected"
                with self.assertRaises(ValueError):
                    self.validate(destination=destination)
                self.assertFalse(destination.exists())

    def test_missing_required_inputs_refused(self):
        for missing in sorted(SOURCE.REQUIRED | {".github/fixture"}):
            with self.subTest(missing=missing):
                self.digest = seal(self.bundle, [item for item in entries() if item[0] != PREFIX + missing])
                with self.assertRaises(ValueError):
                    self.validate()

    def test_bounded_compression_members_file_size_and_total(self):
        for name, maximum in (("MAX_COMPRESSED", 1), ("MAX_UNPACKED", 10), ("MAX_TAR", 1024), ("MAX_FILE", 1), ("MAX_ENTRIES", 1)):
            with self.subTest(limit=name), patch.object(SOURCE, name, maximum), self.assertRaises(ValueError):
                self.validate()

    def test_corrupt_archive_and_invalid_version_refused(self):
        (self.bundle / ARCHIVE).write_bytes(b"not gzip")
        self.digest = reseal(self.bundle)
        with self.assertRaises(OSError):
            self.validate()
        with self.assertRaises(ValueError):
            SOURCE.validate(self.bundle, self.digest, "v01.0.0")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--fixture":
        seal(Path(sys.argv[2]))
    else:
        unittest.main()
