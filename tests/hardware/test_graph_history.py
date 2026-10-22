"""Portable sealed-history conversion tests; synthetic inputs do not qualify GPUs."""

import copy
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_graph_profile import NOW, apply_patch, fixtures, graph


def sealed_history(directory):
    """Use the public contract fixtures in the retained artifact layout."""
    deployment, _, observation = fixtures(datetime(2026, 10, 8, 8, 15, 18, tzinfo=timezone.utc))
    pinned = copy.deepcopy(graph.profile())
    pinned["provenance"]["confirmation"] = str(directory)
    pod = copy.deepcopy(deployment["spec"]["template"])
    container = pod["spec"]["containers"][0]
    args = copy.deepcopy(container["args"])
    container["env"] = [row for row in container["env"] if row["name"] not in graph.ENVIRONMENT]
    observation["pod"]["spec"]["containers"][0]["args"] += ["--base-gpu-id", "0"]
    # Match the helper's preserved environment followed by the reviewed values.
    observation["pod"]["spec"]["containers"][0]["env"] = copy.deepcopy(container["env"]) + [
        {"name": name, "value": value} for name, value in graph.ENVIRONMENT.items()]
    names = {"pod": "evidence/admitted.json", "image": "evidence/image.json", "runtime_versions": "output/runtime-versions.json",
             "source_before": "output/source-before.json", "source_after": "output/source-after.json",
             "model_before": "output/model-before.json", "model_after": "output/model-after.json",
             "compiler_identity": "output/compiler-identity.json", "torch_graphs_before": "output/torch-graphs-before.json",
             "torch_graphs_after": "output/torch-graphs-after.json", "resolved_contract": "output/resolved-contract.json",
             "singleton_topology": "output/singleton-topology.json", "launch": "output/launch.json",
             "policy_before": "evidence/policy-before.json", "effective_policy_before": "evidence/effective-policy-before.json"}
    files = {"verified/probe-0/" + name: observation[key] for key, name in names.items()}
    files.update({"preflight.json": observation["preflight"], "probe-0-source/pod.json": pod,
                  "probe-0-source/probe-config.json": {"server_args": args},
                  "verified/probe-0/output/frozen-inputs.json": {"concurrent_cases": {
                      f"agent-{index}": [first] + [1] * 511
                      for index, first in enumerate((61037, 510, 310, 3836))}}})
    manifest = []
    for name, value in sorted(files.items()):
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = graph.canonical(value)
        path.write_bytes(raw)
        manifest.append(f"{hashlib.sha256(raw).hexdigest()}  {name}\n")
    raw_manifest = "".join(manifest).encode("ascii")
    (directory / "FINAL-ARTIFACTS.sha256").write_bytes(raw_manifest)
    pinned["provenance"]["confirmation_manifest_sha256"] = hashlib.sha256(raw_manifest).hexdigest()
    return pinned


class GraphHistoryTests(unittest.TestCase):
    def test_sealed_historical_example_is_never_current_selected_or_cache_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            pinned = sealed_history(Path(directory) / "sealed")
            output = Path(directory) / "historical"
            with patch.object(graph, "profile", return_value=pinned):
                graph.historical_example(output)
            deployment = graph.read_json(output / "deployment.json")
            observation = graph.read_json(output / "graph-observation.json")
            workload = graph.read_json(output / "workload.json")
            self.assertEqual(observation["preflight"]["observed_at"], "2026-10-08T08:15:18Z")
            self.assertNotIn("caches", observation)
            self.assertEqual(len(workload["cases"][0]["requests"]), 4)
            self.assertEqual([row["input_ids"][0] for row in workload["cases"][0]["requests"]], [61037, 510, 310, 3836])
            result = graph.plan(deployment, workload, observation, experimental=True, now=NOW)
            self.assertEqual(result["selected"], "incumbent")
            self.assertIsNotNone(result["conditional_patch"])
            self.assertEqual(apply_patch(deployment, result["patch"]), deployment)
            self.assertEqual(result["cache_admission"]["status"], "NOT RUN")

    def test_changed_manifest_or_artifact_is_rejected(self):
        for name, message in (("FINAL-ARTIFACTS.sha256", "historical graph manifest changed"),
                              ("preflight.json", "historical graph input changed")):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                evidence = Path(directory) / "sealed"
                pinned = sealed_history(evidence)
                path = evidence / name
                path.write_bytes(path.read_bytes() + b"\n")
                output = Path(directory) / "historical"
                with patch.object(graph, "profile", return_value=pinned):
                    with self.assertRaisesRegex(ValueError, message):
                        graph.historical_example(output)
                self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
