"""tools/sandbox/sandbox_config_helper.py: account review, home labels, address picks (fixtures only)."""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/sandbox"))
import sandbox_config_helper as helper

PARENTS, FAMILY = "g-parents", "g-family"
DUMP = {"groups": [{"id": PARENTS, "name": "Parents"}, {"id": FAMILY, "name": "Family"}],
        "users": [{"id": "u1", "name": "Admin Person", "role": "admin", "group_ids": []},
                  {"id": "u2", "name": "Parent One", "role": "user", "group_ids": [PARENTS, FAMILY]},
                  {"id": "u3", "name": "Kid Two", "role": "user", "group_ids": [FAMILY]},
                  {"id": "u4", "name": "Guest", "role": "user", "group_ids": []}]}


def write(directory, name, value):
    path = Path(directory) / name
    path.write_text(value if isinstance(value, str) else json.dumps(value))
    return path


class LabelTests(unittest.TestCase):
    def test_labels_are_unique_dns_labels(self):
        taken = set()
        self.assertEqual("kid-two", helper.label("Kid Two", taken))
        self.assertEqual("kid-two-2", helper.label("kid two", taken))
        self.assertEqual("kid-two-3", helper.label("KID  TWO!", taken))
        self.assertEqual("user-9-lives", helper.label("9 Lives", taken))
        self.assertEqual("eunice", helper.label("Ëünïcé", taken))
        self.assertEqual("user", helper.label("!!!", taken))
        long = helper.label("a" * 40, taken)
        self.assertEqual(32, len(long))
        self.assertEqual("a" * 30 + "-2", helper.label("a" * 40, taken))


class ReviewTests(unittest.TestCase):
    def dump(self, directory):
        return helper.load_dump(write(directory, "dump.json", DUMP))

    def test_groups_resolve_most_permissive(self):
        with tempfile.TemporaryDirectory() as directory:
            dump, groups = self.dump(directory)
        self.assertEqual({"Parents": PARENTS, "Family": FAMILY}, groups)
        self.assertEqual(["Admin", "Parents", "Family", None], [helper.resolved(u) for u in dump["users"]])

    def test_a_missing_or_repeated_group_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            for groups in ([{"id": PARENTS, "name": "Parents"}], DUMP["groups"] + [{"id": "x", "name": "Family"}]):
                with self.subTest(groups=groups), self.assertRaises(SystemExit):
                    helper.load_dump(write(directory, "dump.json", {**DUMP, "groups": groups}))

    def test_propose_writes_a_review_without_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            review = Path(directory) / "review.tsv"
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                helper.propose(write(directory, "dump.json", DUMP), review)
            text = review.read_text() + out.getvalue()
            dump, _ = self.dump(directory)
            entries = helper.read_review(dump, review)
        for secret in ("u1", "u2", PARENTS, FAMILY):
            self.assertNotIn(secret, text)
        self.assertIn("4 accounts (1 admin, 3 user)", out.getvalue())
        self.assertEqual([("admin-person", "Admin"), ("parent-one", "Parents"), ("kid-two", "Family")],
                         [(e["name"], e["group"]) for e in entries])
        self.assertEqual(["u1", "u2", "u3"], [e["webui_user_id"] for e in entries])

    def test_bad_reviews_are_refused(self):
        rows = ["1 yes admin-person Admin 20G", "2 yes parent-one Parents 20G", "3 yes kid-two Family 20G",
                "4 no guest - 20G"]
        bad = {"a Family-only account as Parents": {2: "3 yes kid-two Parents 20G"},
               "Admin for a user account": {1: "2 yes parent-one Admin 20G"},
               "an unknown group": {2: "3 yes kid-two Guests 20G"},
               "include neither yes nor no": {2: "3 maybe kid-two Family 20G"},
               "a repeated row": {3: "3 no kid-two Family 20G"},
               "a missing column": {3: "4 no guest 20G"}}
        with tempfile.TemporaryDirectory() as directory:
            dump, _ = self.dump(directory)
            self.assertEqual(3, len(helper.read_review(dump, write(directory, "ok.tsv", "\n".join(rows)))))
            self.assertRaises(ValueError, helper.read_review, dump, write(directory, "short.tsv", "\n".join(rows[:3])))
            for name, change in bad.items():
                lines = [change.get(i, line) for i, line in enumerate(rows)]
                with self.subTest(name), self.assertRaises(ValueError):
                    helper.read_review(dump, write(directory, "bad.tsv", "\n".join(lines)))


class AddressTests(unittest.TestCase):
    def pick(self, taken=(), cidrs=("10.43.0.0/16",)):
        with tempfile.TemporaryDirectory() as directory:
            args = [write(directory, "cidrs.json", {"items": [{"spec": {"cidrs": list(cidrs)}}]}),
                    write(directory, "services.json", {"items": [{"spec": {"clusterIP": ip}} for ip in taken]}),
                    write(directory, "addresses.json", {"items": [{"metadata": {"name": "10.43.0.1"}}]})]
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                helper.pick_ips(*args)
        return json.loads(out.getvalue())

    def test_the_preferred_static_band_addresses(self):
        self.assertEqual({"cidr": "10.43.0.0/16", "family": "10.43.0.201", "parents": "10.43.0.202"}, self.pick())
        self.assertEqual("10.43.0.255", self.pick(taken=["10.43.0.201"])["parents"])

    def test_the_service_range_must_be_one_ipv4_network(self):
        with self.assertRaises(SystemExit):
            self.pick(cidrs=("10.43.0.0/16", "10.44.0.0/16"))

    def test_api_endpoint(self):
        def endpoint(addresses, ports=({"name": "https", "port": 6443},)):
            with tempfile.TemporaryDirectory() as directory:
                path = write(directory, "slices.json", {"items": [
                    {"addressType": "IPv4", "endpoints": [{"addresses": addresses}], "ports": list(ports)}]})
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    helper.api_endpoint(path)
                return out.getvalue().strip()

        self.assertEqual("10.0.30.11:6443", endpoint(["10.0.30.11"]))
        for addresses in (["10.0.30.11", "10.0.30.12"], ["8.8.8.8"], []):
            with self.subTest(addresses=addresses), self.assertRaises(SystemExit):
                endpoint(addresses)


class ExtractTests(unittest.TestCase):
    def test_extract_reads_the_database_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "webui.db"
            subprocess.run([sys.executable, "-I", str(ROOT / "tests/tools/make-fake-db.py"), str(db)], check=True)
            before = db.read_bytes()
            result = subprocess.run([sys.executable, "-I", str(ROOT / "tools/sandbox/sandbox_config_helper.py"),
                                     "extract", str(db)], capture_output=True, text=True, check=True)
            self.assertEqual(before, db.read_bytes())
        dump = json.loads(result.stdout)
        self.assertEqual({"Parents", "Family"}, {g["name"] for g in dump["groups"]})
        self.assertEqual(6, len(dump["users"]))
        self.assertNotIn("@example", result.stdout)


if __name__ == "__main__":
    unittest.main()
