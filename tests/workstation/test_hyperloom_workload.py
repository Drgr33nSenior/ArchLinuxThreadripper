import base64
import copy
import importlib.util
import json
from pathlib import Path
import sys
import unittest
import zlib

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib/workstation"))
import hyperloom_workload as workload
from hyperloom_exchange import canonical, digest


def donor():
    rows, plans, assets = [], [], {}
    for i in range(16):
        task = "warmup" if i < 3 else "pair" if i in (8, 9) else "ledger" if i in (10, 14, 15) else "long"
        ids = [i + 1, 2, 3, 4]
        body = dict(messages=[{"role": "user", "content": "fixture " + str(i)}],
                    cache_salt="old-" + str(i), rid="old-" + str(i), max_completion_tokens=4096,
                    reasoning_effort="low", chat_template_kwargs={"enable_thinking": True},
                    ignore_eos=False, stream=False)
        expected = dict(case=str(i), context_length=131040, input_tokens=len(ids),
                        input_ids=ids, input_ids_sha256=digest(canonical(ids)),
                        messages_sha256=digest(canonical(body["messages"])),
                        task_grade={"kind": "exact_json", "expected": {"fixture": i}},
                        cache_policy={"intent": "cold", "minimum_cached_tokens": 0, "maximum_cached_tokens": 0})
        path, exp = f"r{i}.json", f"r{i}.expected.json"
        raw, exp_raw = canonical(body), canonical(expected)
        plan = dict(ordinal=i, case=str(i), category="timing", group=str(i), task=task, path=path,
                    expected=exp, input_tokens=len(ids), maximum_output_tokens=4096, internal_reserve_tokens=16,
                    context_envelope_tokens=131040, sha256=digest(raw), bytes=len(raw), expected_sha256=digest(exp_raw))
        row = dict(plan, block="paired", body=body, body_sha256=digest(raw), expected=expected)
        rows.append(row)
        plans.append(plan)
        assets[path], assets[exp] = raw, exp_raw
    settings = dict(requests=plans, paired_serving={"context_length": 131040, "token_pool_capacity": 262144})
    logical = {
        "CLIENT-BINDING.zlib": zlib.compress(canonical({"rows": rows})),
        "FINAL-REQUESTS.zlib": zlib.compress(canonical({k: base64.b64encode(v).decode() for k, v in assets.items()})),
    }
    return {"FINAL-SETTINGS.json": canonical(settings),
            "FINAL-REQUESTS.zlib": zlib.compress(canonical({k: base64.b64encode(v).decode() for k, v in logical.items()}))}


class WorkloadTests(unittest.TestCase):
    def test_six_balanced_cases_have_three_distinct_samples(self):
        menu = workload.derive(donor())
        self.assertEqual(len(menu["rows"]), 30)
        self.assertEqual(len(menu["groups"]), 20)
        self.assertEqual(sorted(c["concurrency"] for c in menu["cases"].values()), [1, 1, 1, 2, 2, 2])
        for case in menu["cases"].values():
            self.assertEqual([s["repetition"] for s in case["samples"]], [0, 1, 2])
            for sample in case["samples"]:
                self.assertEqual(len(sample["ordinals"]), case["concurrency"])
        self.assertEqual(len({r["body"]["rid"] for r in menu["bound"]}), 30)

    def test_prompts_tokens_and_answer_oracles_are_preserved(self):
        files = donor()
        original = json.loads(workload._unpack(base64.b64decode(
            json.loads(workload._unpack(files["FINAL-REQUESTS.zlib"]))["CLIENT-BINDING.zlib"])))["rows"]
        menu = workload.derive(files)
        for case, indices, intent in workload.CASES:
            for sample in menu["cases"][case]["samples"]:
                for ordinal, old in zip(sample["ordinals"], indices, strict=True):
                    got = menu["bound"][ordinal]
                    for field in ("input_ids", "input_ids_sha256", "messages_sha256", "task_grade"):
                        self.assertEqual(got["expected"][field], original[old]["expected"][field])
                    self.assertEqual(got["body"]["messages"], original[old]["body"]["messages"])

    def test_warm_rows_only_reuse_their_own_cold_seed(self):
        menu = workload.derive(donor())
        cold = {}
        for ordinal, row in enumerate(menu["bound"]):
            policy = row["expected"]["cache_policy"]
            salt = row["body"]["cache_salt"]
            if policy["intent"] == "cold":
                self.assertNotIn(salt, cold)
                cold[salt] = ordinal
                self.assertEqual(policy["maximum_cached_tokens"], 0)
            else:
                seed = menu["cache"][ordinal]["seed"]
                self.assertEqual(cold[salt], seed)
                self.assertEqual(menu["bound"][seed]["body"]["messages"], row["body"]["messages"])
                self.assertEqual(policy["minimum_cached_tokens"], 3)
                self.assertEqual(policy["maximum_cached_tokens"], 3)
        self.assertEqual(len(cold), 21)

    def test_schedule_is_identical_between_arms_and_c2_is_one_group(self):
        menu = workload.derive(donor())
        self.assertEqual(menu["protocol"]["order"]["control"], menu["protocol"]["order"]["mtp"])
        self.assertEqual(sorted(i for g in menu["groups"] for i in g["ordinals"]), list(range(30)))
        for g in menu["groups"]:
            self.assertEqual(len(g["ordinals"]), g["concurrency"])

    def test_donor_body_oracle_binding_cannot_drift(self):
        files = donor()
        settings = json.loads(files["FINAL-SETTINGS.json"])
        settings["requests"][3]["sha256"] = "0" * 64
        files["FINAL-SETTINGS.json"] = canonical(settings)
        with self.assertRaisesRegex(ValueError, "identity"):
            workload.derive(files)

    def test_trailing_or_incomplete_compression_is_refused(self):
        raw = zlib.compress(b"fixture")
        for bad in (raw + b"extra", raw[:-2], zlib.compress(b"x" * 100)):
            with self.assertRaises(ValueError):
                workload._unpack(bad, 16)

    def test_deterministic_menu_does_not_mutate_donor(self):
        files = donor()
        before = copy.deepcopy(files)
        self.assertEqual(workload.derive(files), workload.derive(files))
        self.assertEqual(files, before)

    def test_profile_menu_contains_only_warmups_and_one_cold_pair(self):
        menu = workload.derive_profile(donor())
        self.assertEqual(len(menu["rows"]), 5)
        self.assertEqual([g["concurrency"] for g in menu["groups"]], [1, 2, 2])
        self.assertEqual([g["ordinals"] for g in menu["groups"]], [[0], [1, 2], [3, 4]])
        self.assertTrue(menu["diagnostic_only"])
        self.assertEqual(menu["cases"], {})
        for row in menu["bound"]:
            self.assertEqual(row["expected"]["cache_policy"]["maximum_cached_tokens"], 0)

    def test_profile_assets_and_native_binding_join_after_reindexing(self):
        menu = workload.derive_profile(donor())
        logical = {k: base64.b64decode(v) for k, v in json.loads(workload._unpack(menu["packed"])).items()}
        assets = {k: base64.b64decode(v) for k, v in json.loads(workload._unpack(logical["FINAL-REQUESTS.zlib"])).items()}
        self.assertEqual(len(assets), 10)
        bound = json.loads(workload._unpack(logical["CLIENT-BINDING.zlib"]))["rows"]
        for ordinal, (plan, row) in enumerate(zip(menu["rows"], bound, strict=True)):
            self.assertEqual(plan["ordinal"], ordinal)
            self.assertEqual(row["ordinal"], ordinal)
            self.assertEqual(plan["path"], row["path"])
            self.assertEqual(plan["sha256"], digest(assets[plan["path"]]))
            self.assertEqual(canonical(row["body"]), assets[plan["path"]])
            self.assertEqual(canonical(row["expected"]), assets[plan["expected"]])


class SourceScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = ROOT / "infrastructure/hyperloom/sglang-owner-adapter/build_owner.py"
        spec = importlib.util.spec_from_file_location("_test_hyperloom_build_owner", path)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_declared_function_body_can_change_without_execution(self):
        baseline = b"import os\ndef _fwd_kernel(x):\n    return x\ndef unrelated():\n    return 1\n"
        candidate = baseline.replace(b"return x", b"raise RuntimeError('not executed')")
        self.assertEqual(self.module.source_scope(baseline, candidate), ["_fwd_kernel"])

    def test_import_signature_decorator_and_unrelated_changes_refused(self):
        baseline = b"import os\ndef _fwd_kernel(x):\n    return x\ndef unrelated():\n    return 1\n"
        for candidate in (
            baseline.replace(b"import os", b"import subprocess"),
            baseline.replace(b"_fwd_kernel(x)", b"_fwd_kernel(x, y)"),
            baseline.replace(b"def _fwd_kernel", b"@something\ndef _fwd_kernel"),
            baseline.replace(b"return 1", b"return 2"),
        ):
            with self.assertRaises(ValueError):
                self.module.source_scope(baseline, candidate)


if __name__ == "__main__":
    unittest.main()
