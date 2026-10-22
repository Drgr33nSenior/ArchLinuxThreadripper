"""Check retained local benchmark evidence; run separately from portable tests."""

from pathlib import Path
import tempfile
import unittest

from test_graph_profile import NOW, apply_patch, graph


class RetainedGraphHistoryTests(unittest.TestCase):
    def test_real_sealed_historical_example_is_never_current_selected_or_cache_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "historical"
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


if __name__ == "__main__":
    unittest.main()
