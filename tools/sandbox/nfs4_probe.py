"""Read-only NFSv4.0 export probe: is each path visible to this host in the server's pseudo-filesystem?

    sudo python3 -I nfs4_probe.py SERVER PATH...

One COMPOUND per path (PUTROOTFH, LOOKUP for each component, GETFH), AUTH_SYS as root,
from a reserved source port because the exports use the default `secure` option. It opens
nothing, creates no client state and mounts nothing. The NFSv4 pseudo-filesystem a server
shows a client holds only the exports that client may mount and the directories leading to
them, so a path whose every component resolves is exported to this host (or is a directory
above such an export). Prints one line per path; exits 0 only when every path resolves.
"""
import os
import random
import socket
import struct
import sys

NFS_PROGRAM, NFS_V4, COMPOUND = 100003, 4, 1
OP_GETFH, OP_LOOKUP, OP_PUTROOTFH = 10, 15, 24
ERRORS = {2: "NOENT (not exported to this host)", 13: "ACCESS", 20: "NOTDIR", 10016: "WRONGSEC",
          10021: "MINOR_VERS_MISMATCH (server refuses NFSv4.0)", 10006: "SERVERFAULT"}


def opaque(data):
    return struct.pack(">I", len(data)) + data + b"\0" * (-len(data) % 4)


def call(path):
    components = [part for part in path.split("/") if part]
    cred = struct.pack(">I", 0) + opaque(socket.gethostname().encode()[:255]) + struct.pack(">III", 0, 0, 0)
    ops = struct.pack(">I", OP_PUTROOTFH)
    for part in components:
        ops += struct.pack(">I", OP_LOOKUP) + opaque(part.encode())
    ops += struct.pack(">I", OP_GETFH)
    xid = random.getrandbits(32)
    body = (struct.pack(">IIIIII", xid, 0, 2, NFS_PROGRAM, NFS_V4, COMPOUND)
            + struct.pack(">I", 1) + opaque(cred) + struct.pack(">II", 0, 0)
            + opaque(b"probe") + struct.pack(">II", 0, len(components) + 2) + ops)
    return xid, struct.pack(">I", 0x80000000 | len(body)) + body, len(components)


def connect(server):
    for port in random.sample(range(600, 1024), 40):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("", port))
        except OSError:
            sock.close()
            continue
        sock.settimeout(8)
        sock.connect((server, 2049))
        return sock
    raise OSError("no free reserved source port (run as root)")


def receive(sock):
    data = b""
    while True:
        header = b""
        while len(header) < 4:
            chunk = sock.recv(4 - len(header))
            if not chunk:
                raise OSError("connection closed")
            header += chunk
        (mark,) = struct.unpack(">I", header)
        size, last = mark & 0x7FFFFFFF, mark & 0x80000000
        if size > 1 << 20:
            raise OSError("reply too large")
        while size:
            chunk = sock.recv(size)
            if not chunk:
                raise OSError("connection closed")
            data += chunk
            size -= len(chunk)
        if last:
            return data


def probe(server, path):
    xid, request, depth = call(path)
    with connect(server) as sock:
        sock.sendall(request)
        reply = receive(sock)
    return parse(reply, xid, path, depth)


def parse(reply, xid, path, depth):
    """The COMPOUND reply -> (resolved, detail)."""
    got, kind, state = struct.unpack_from(">III", reply, 0)
    if got != xid or kind != 1 or state != 0:
        return False, "RPC call denied"
    offset = 12
    _, length = struct.unpack_from(">II", reply, offset)
    offset += 8 + length + (-length % 4)
    (accept,) = struct.unpack_from(">I", reply, offset)
    if accept != 0:
        return False, f"RPC not accepted ({accept})"
    offset += 4
    status, length = struct.unpack_from(">II", reply, offset)
    offset += 8 + length + (-length % 4)
    (count,) = struct.unpack_from(">I", reply, offset)
    if status == 0:
        return True, "exported to this host"
    # Results run up to the failed operation: PUTROOTFH, the LOOKUPs that resolved, the failure.
    reached = min(max(count - 2, 0), depth)
    resolved = "/" + "/".join([p for p in path.split("/") if p][:reached])
    return False, f"{ERRORS.get(status, f'NFS4 error {status}')}; resolved {resolved}"


def main():
    if len(sys.argv) < 3:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    server, paths = sys.argv[1], sys.argv[2:]
    failed = 0
    for path in paths:
        if not path.startswith("/") or any(part in {".", ".."} for part in path.split("/")):
            print(f"FAIL {path}: not an absolute normalized path")
            failed += 1
            continue
        try:
            ok, detail = probe(server, path)
        except (OSError, struct.error) as error:
            ok, detail = False, f"probe failed: {error}"
        failed += not ok
        print(f'{"ok  " if ok else "FAIL"} {server}:{path}: {detail}')
    return 1 if failed else 0


if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] in ("-h", "--help"):
        print(__doc__.strip())
        raise SystemExit(0 if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help") else 2)
    if os.geteuid() != 0:
        print("run as root: the exports require a reserved source port", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main())
