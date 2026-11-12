"""service_mode.py prepare: a render becomes the List a mode switch applies."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib/workstation"))
import service_mode  # noqa: E402


def deployment(name, replicas=0):
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": name, "namespace": "ai-home-lab"}, "spec": {"replicas": replicas}}


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.workloads = self.root / "workloads.yaml"
        self.live = self.root / "live.json"
        self.output = self.root / "apply.json"
        docs = [deployment(n) for n in ("sglang", "swarmui", "comfyui-gpu0", "litellm", "open-webui", "new-service")]
        docs.append({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "gateway-policy", "namespace": "ai-home-lab"}})
        self.workloads.write_text(yaml.safe_dump_all(docs))
        self.live.write_text(json.dumps({"items": [deployment("sglang", 1), deployment("swarmui", 1),
                                                   deployment("litellm", 1), deployment("open-webui", 1)]}))

    def test_managed_stay_at_zero_and_others_keep_live_replicas(self):
        summary = service_mode.prepare(self.workloads, self.live, self.output, {"sglang", "swarmui", "comfyui-gpu0"})
        applied = json.loads(self.output.read_text())
        self.assertEqual(("List", "v1"), (applied["kind"], applied["apiVersion"]))
        replicas = {d["metadata"]["name"]: d["spec"]["replicas"] for d in applied["items"] if d["kind"] == "Deployment"}
        # Live GPU workloads are not restarted by the apply: the session starts them in order.
        self.assertEqual({"sglang": 0, "swarmui": 0, "comfyui-gpu0": 0, "litellm": 1, "open-webui": 1,
                          "new-service": 0}, replicas)
        self.assertEqual(["new-service"], summary["not_live"])
        self.assertEqual(["comfyui-gpu0", "sglang", "swarmui"], summary["managed"])
        self.assertTrue(any(d["kind"] == "ConfigMap" for d in applied["items"]))
        self.assertEqual(0o600, self.output.stat().st_mode & 0o777)

    def test_existing_output_and_duplicates_are_refused(self):
        self.output.write_text("{}")
        with self.assertRaises(ValueError):
            service_mode.prepare(self.workloads, self.live, self.output, {"sglang"})
        self.output.unlink()
        docs = list(yaml.safe_load_all(self.workloads.read_text())) + [deployment("sglang")]
        self.workloads.write_text(yaml.safe_dump_all(docs))
        with self.assertRaises(ValueError):
            service_mode.prepare(self.workloads, self.live, self.output, {"sglang"})
        self.assertFalse(self.output.exists())

    def test_cli_requires_managed_names(self):
        with self.assertRaises(SystemExit):
            service_mode.main(["prepare", str(self.workloads), str(self.live), str(self.output), "--managed", ""])


if __name__ == "__main__":
    unittest.main()
