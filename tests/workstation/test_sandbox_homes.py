"""Sandbox home mapping validation, static volume render and NAS variables."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib/workstation"))
import sandbox
import sandbox_homes

USERS = {"users": [
    {"webui_user_id": "00000000-0000-4000-8000-000000000001", "name": "example-parent", "group": "Parents"},
    {"webui_user_id": "00000000-0000-4000-8000-000000000002", "name": "no", "quota": "50G", "group": "Family"}]}


class MappingTests(unittest.TestCase):
    def read(self, value):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sandbox-users.json"
            path.write_text(value if isinstance(value, str) else json.dumps(value))
            return sandbox_homes.read_users(path)

    def test_valid_mapping_fills_the_default_quota(self):
        users = self.read(USERS)
        self.assertEqual(["20G", "50G"], [user["quota"] for user in users])
        self.assertEqual(USERS["users"][1], users[1])
        self.assertEqual([], self.read({"users": []}))

    def test_malformed_ambiguous_or_unsafe_mappings_are_refused(self):
        first, second = USERS["users"]
        for users in ([{**first, "name": "Example"}], [{**first, "name": "a/../b"}], [{**first, "name": "alice\n"}],
                      [{**first, "name": "-a"}], [{**first, "name": "a" * 33}], [{**first, "name": ""}],
                      [{**first, "quota": "20"}], [{**first, "quota": "0G"}], [{**first, "quota": "501G"}],
                      [{**first, "quota": "1T"}], [{**first, "quota": 20}], [{**first, "quota": "20G\n"}],
                      [{**first, "group": "Guests"}], [{**first, "webui_user_id": ""}],
                      [{**first, "webui_user_id": "a b"}], [{**first, "home": "/srv"}],
                      [{k: v for k, v in first.items() if k != "group"}], ["example-parent"],
                      [first, {**second, "name": first["name"]}],
                      [first, {**second, "webui_user_id": first["webui_user_id"]}],
                      [{**first, "name": f"user{i}", "webui_user_id": f"id{i}"} for i in range(33)]):
            with self.subTest(users=users), self.assertRaises(ValueError):
                self.read({"users": users})
        for value in ({"users": {}}, {"users": [], "extra": 1}, [], "not json"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.read(value)

    def test_errors_do_not_echo_user_ids(self):
        with self.assertRaises(ValueError) as caught:
            self.read({"users": [{**USERS["users"][0], "group": "Guests"}]})
        self.assertNotIn("00000000", str(caught.exception))


class RenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as directory:
            cls.path = Path(directory) / "sandbox-users.json"
            cls.path.write_text(json.dumps(USERS))
            cls.users = sandbox_homes.read_users(cls.path)
            cls.rendered = subprocess.run([sys.executable, str(ROOT / "lib/workstation/sandbox_homes.py"), "render",
                                           str(cls.path)], capture_output=True, text=True, check=True).stdout
            cls.nas = subprocess.run([sys.executable, str(ROOT / "lib/workstation/sandbox_homes.py"), "nas-vars",
                                      str(cls.path)], capture_output=True, text=True, check=True).stdout
            cls.refused = subprocess.run([sys.executable, str(ROOT / "lib/workstation/sandbox_homes.py"), "render",
                                          str(cls.path.with_name("missing.json"))], capture_output=True, text=True)
        cls.objects = list(yaml.safe_load_all(cls.rendered))

    def test_each_user_gets_one_retained_volume_bound_to_one_claim(self):
        self.assertEqual(sandbox_homes.render(self.users), self.objects)
        volumes = [o for o in self.objects if o["kind"] == "PersistentVolume"]
        claims = [o for o in self.objects if o["kind"] == "PersistentVolumeClaim"]
        self.assertEqual(["sandbox-home-example-parent", "sandbox-home-no"], [c["metadata"]["name"] for c in claims])
        for volume, claim, user in zip(volumes, claims, self.users, strict=True):
            name = sandbox_homes.claim_name(user["name"])
            self.assertEqual(name, volume["metadata"]["name"])
            self.assertNotIn("namespace", volume["metadata"])
            self.assertEqual(sandbox.NAMESPACE, claim["metadata"]["namespace"])
            spec = volume["spec"]
            self.assertEqual({"namespace": sandbox.NAMESPACE, "name": name}, spec["claimRef"])
            self.assertEqual(name, claim["spec"]["volumeName"])
            self.assertEqual("Retain", spec["persistentVolumeReclaimPolicy"])
            # An empty class on both sides keeps the default provisioner away from the claim.
            self.assertEqual(("", ""), (spec["storageClassName"], claim["spec"]["storageClassName"]))
            self.assertEqual((["ReadWriteOnce"],) * 2, (spec["accessModes"], claim["spec"]["accessModes"]))
            self.assertEqual(user["quota"] + "i", spec["capacity"]["storage"])
            self.assertEqual(sandbox_homes.CLAIM_REQUEST, claim["spec"]["resources"]["requests"]["storage"])
            share = f'/srv/microserver/sandbox/{user["name"]}'
            self.assertEqual({"server": "192.168.30.10", "share": share}, spec["csi"]["volumeAttributes"])
        handles = [v["spec"]["csi"]["volumeHandle"] for v in volumes]
        self.assertEqual(len(set(handles)), len(handles))

    def test_mount_matches_the_nas_nfs_class_and_the_workspace_owner(self):
        storage_class = yaml.safe_load((ROOT / "infrastructure/nfs-csi/storageclass.yaml").read_text())
        self.assertEqual(storage_class["parameters"]["server"], sandbox_homes.NAS_SERVER)
        self.assertEqual(storage_class["mountOptions"], sandbox_homes.MOUNT_OPTIONS)
        self.assertEqual(storage_class["provisioner"], self.objects[0]["spec"]["csi"]["driver"])
        # Homes are separate exports, outside the share the provisioner writes to.
        self.assertFalse(sandbox_homes.EXPORT_ROOT.startswith(storage_class["parameters"]["share"]))
        security = sandbox.policy({"image": "fixture", "egress_service_ips": {"family": "10.43.0.201"}},
                                  "family")["container_security_context"]
        self.assertEqual((sandbox_homes.OWNER,) * 2, (security["runAsUser"], security["runAsGroup"]))

    def test_outputs_carry_names_and_quotas_but_no_user_ids(self):
        self.assertEqual({"microserver_sandbox_homes_enabled": True, "microserver_sandbox_homes": [
            {"name": "example-parent", "quota": "20G"}, {"name": "no", "quota": "50G"}]}, json.loads(self.nas))
        for output in (self.rendered, self.nas):
            self.assertNotIn("00000000", output)
        self.assertEqual((1, ""), (self.refused.returncode, self.refused.stdout))
        self.assertTrue(self.refused.stderr.startswith("ERROR: "))


if __name__ == "__main__":
    unittest.main()
