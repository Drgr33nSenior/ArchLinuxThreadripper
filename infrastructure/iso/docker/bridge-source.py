#!/usr/bin/env python3
"""Validate a reviewed Bridge source bundle before extracting executable source.

This validator belongs to the builder, not to the supplied archive. The digest
is explicit owner input; structural validity alone does not establish trust.
"""

import gzip
import hashlib
import io
import os
from pathlib import Path
import re
import stat
import sys
import tarfile
import tempfile
import zlib

MAX_COMPRESSED = 64 * 1024 * 1024
MAX_UNPACKED = 256 * 1024 * 1024
MAX_TAR = 320 * 1024 * 1024
MAX_FILE = 16 * 1024 * 1024
MAX_ENTRIES = 10000
ROOTS = {"cmd", "internal", "api", "deployment", "docs", "scripts", "packaging", ".github"}
FILES = {".go-version", "go.mod", "go.sum", "Makefile", "README.md"}
REQUIRED = FILES | {"cmd/bridge-arch-package/main.go", "packaging/arch/PKGBUILD.in"}


def read_regular(path, maximum):
    # Do not block opening a FIFO before its type can be refused.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
            raise ValueError("bundle input is not a bounded, independent regular file")
        value = stream.read(maximum + 1)
        if len(value) > maximum:
            raise ValueError("bundle input exceeds its size limit")
        return value


def validate(bundle, digest, version, destination=None):
    if not re.fullmatch(r"[a-f0-9]{64}", digest) or not re.fullmatch(
        r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version
    ):
        raise ValueError("explicit source SHA-256 and stable version are required")
    if bundle.is_symlink() or not bundle.is_dir():
        raise ValueError("bundle must be a real directory")
    archive_name = "spry-bridge-" + version[1:] + "-src.tar.gz"
    if {item.name for item in bundle.iterdir()} != {archive_name, "PKGBUILD", "SHA256SUMS"}:
        raise ValueError("bundle must contain exactly archive, recipe and checksum manifest")
    archive = read_regular(bundle / archive_name, MAX_COMPRESSED)
    recipe = read_regular(bundle / "PKGBUILD", 128 * 1024)
    manifest = read_regular(bundle / "SHA256SUMS", 1024)
    archive_hash = hashlib.sha256(archive).hexdigest()
    expected = f"{archive_hash}  {archive_name}\n{hashlib.sha256(recipe).hexdigest()}  PKGBUILD\n".encode()
    if archive_hash != digest or manifest != expected:
        raise ValueError("reviewed source digest or exact two-file checksum manifest differs")
    prefix = "spry-ai-workstation-bridge-" + version[1:] + "/"
    # Bound decompression before tarfile parses extended headers, whose declared
    # lengths otherwise bypass ordinary member-size checks. No source is executed.
    with tempfile.TemporaryFile() as expanded:
        size = 0
        with gzip.GzipFile(fileobj=io.BytesIO(archive)) as zipped:
            while True:
                chunk = zipped.read(min(1024 * 1024, MAX_TAR - size + 1))
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_TAR:
                    raise ValueError("expanded archive exceeds its size limit")
                expanded.write(chunk)
        expanded.seek(0)
        with tarfile.open(fileobj=expanded, mode="r:") as source:
            members = []
            names = set()
            total = 0
            seen_roots = set()
            for member in source:
                if len(members) >= MAX_ENTRIES:
                    raise ValueError("archive has too many entries")
                if not member.name.startswith(prefix):
                    raise ValueError("archive entry has an unexpected package prefix")
                relative = member.name[len(prefix):]
                parts = relative.split("/")
                if (
                    not re.fullmatch(r"[A-Za-z0-9_.@/-]+", relative)
                    or any(part in {"", ".", "..", ".git"} for part in parts)
                    or relative in names
                    or not (relative in FILES or len(parts) > 1 and parts[0] in ROOTS)
                ):
                    raise ValueError("archive entry escapes or differs from the source allowlist")
                if (member.type not in {tarfile.REGTYPE, tarfile.AREGTYPE}
                        or member.issparse() or member.linkname or member.mode not in {0o644, 0o755}):
                    raise ValueError("archive contains a link, special entry or unsupported file mode")
                if member.size < 0 or member.size > MAX_FILE:
                    raise ValueError("archive member exceeds its size limit")
                total += member.size
                if total > MAX_UNPACKED:
                    raise ValueError("archive files exceed the unpacked size limit")
                names.add(relative)
                if len(parts) > 1:
                    seen_roots.add(parts[0])
                members.append((relative, member))
            if not REQUIRED <= names or seen_roots != ROOTS:
                raise ValueError("archive lacks required source roots, toolchain, generator or template")
            # A file cannot also be an ancestor of another file.
            if any("/".join(name.split("/")[:index]) in names
                   for name in names for index in range(1, len(name.split("/")))):
                raise ValueError("archive contains conflicting file and directory paths")
            if destination is None:
                return
            # Exclusive new extraction, after full validation; retain failed work
            # rather than delete it. Never let tarfile choose filesystem targets.
            destination.mkdir(mode=0o700)
            for relative, member in members:
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                with source.extractfile(member) as incoming:
                    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(fd, "wb") as outgoing:
                        remaining = member.size
                        while remaining:
                            chunk = incoming.read(min(1024 * 1024, remaining))
                            if not chunk:
                                raise ValueError("archive member is truncated")
                            outgoing.write(chunk)
                            remaining -= len(chunk)
                        outgoing.flush()
                        os.fchmod(outgoing.fileno(), member.mode)


def main():
    if len(sys.argv) not in {4, 5}:
        raise ValueError("usage: bridge-source.py BUNDLE SHA256 VERSION [NEW_CHECKOUT]")
    validate(Path(sys.argv[1]), sys.argv[2], sys.argv[3], Path(sys.argv[4]) if len(sys.argv) == 5 else None)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, tarfile.TarError, EOFError, zlib.error):
        print("Local Bridge source validation failed; retain the reviewed bundle and failed job for inspection.", file=sys.stderr)
        sys.exit(1)
