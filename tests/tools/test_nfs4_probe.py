"""tools/sandbox/nfs4_probe.py: the COMPOUND request it sends and how it reads replies (no network)."""
import struct
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools/sandbox/nfs4_probe.py"
sys.path.insert(0, str(TOOL.parent))
import nfs4_probe


def reply(xid, status, results, accept=0, reply_stat=0, tag=b"probe"):
    body = struct.pack(">III", xid, 1, reply_stat) + struct.pack(">II", 0, 0) + struct.pack(">I", accept)
    return body + struct.pack(">I", status) + nfs4_probe.opaque(tag) + struct.pack(">I", results)


class RequestTests(unittest.TestCase):
    def test_compound_lookups_one_per_component(self):
        xid, request, depth = nfs4_probe.call("/srv/microserver/sandbox/kid")
        self.assertEqual(4, depth)
        (mark,) = struct.unpack_from(">I", request)
        self.assertEqual(0x80000000 | (len(request) - 4), mark)
        self.assertEqual((xid, 0, 2, 100003, 4, 1), struct.unpack_from(">IIIIII", request, 4))
        # The tail is fixed: tag, minor version 0, six operations. (Counting LOOKUP opcodes over the
        # whole request is wrong: the random xid and the hostname's length can hold the same bytes.)
        ops = struct.pack(">I", nfs4_probe.OP_PUTROOTFH)
        for part in ("srv", "microserver", "sandbox", "kid"):
            ops += struct.pack(">I", nfs4_probe.OP_LOOKUP) + nfs4_probe.opaque(part.encode())
        ops += struct.pack(">I", nfs4_probe.OP_GETFH)
        self.assertTrue(request.endswith(nfs4_probe.opaque(b"probe") + struct.pack(">II", 0, 6) + ops))

    def test_opaque_pads_to_four_bytes(self):
        self.assertEqual(b"\0\0\0\x03abc\0", nfs4_probe.opaque(b"abc"))


class ReplyTests(unittest.TestCase):
    PATH = "/srv/microserver/sandbox/kid"

    def test_a_resolved_path_is_exported(self):
        self.assertEqual((True, "exported to this host"), nfs4_probe.parse(reply(7, 0, 6), 7, self.PATH, 4))

    def test_a_failed_lookup_names_how_far_it_resolved(self):
        ok, detail = nfs4_probe.parse(reply(7, 2, 5), 7, self.PATH, 4)
        self.assertFalse(ok)
        self.assertEqual("NOENT (not exported to this host); resolved /srv/microserver/sandbox", detail)

    def test_rpc_level_refusals(self):
        self.assertEqual((False, "RPC call denied"), nfs4_probe.parse(reply(8, 0, 6), 7, self.PATH, 4))
        self.assertEqual((False, "RPC call denied"), nfs4_probe.parse(reply(7, 0, 6, reply_stat=1), 7, self.PATH, 4))
        self.assertEqual((False, "RPC not accepted (2)"), nfs4_probe.parse(reply(7, 0, 6, accept=2), 7, self.PATH, 4))
        self.assertIn("NFS4 error 99", nfs4_probe.parse(reply(7, 99, 2), 7, self.PATH, 4)[1])


class CommandTests(unittest.TestCase):
    def test_usage_before_the_root_check(self):
        for args, code in ((["--help"], 0), ([], 2)):
            result = subprocess.run([sys.executable, "-I", str(TOOL), *args], check=False, capture_output=True, text=True)
            self.assertEqual(code, result.returncode)
            self.assertIn("nfs4_probe.py SERVER PATH", result.stdout)


if __name__ == "__main__":
    unittest.main()
