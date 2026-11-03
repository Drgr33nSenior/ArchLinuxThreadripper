"""CPU acceptance for the explicit opt-in recurrent checkpoint trimming experiment.

The pool model is conditional on the pinned-source reading in
CLAUDE-WARM-C2-REVIEW-v1; nothing here is GPU evidence. Build tests run the
owner builder in a fresh interpreter against the retained selected donor and
skip when that donor is not present on this machine.
"""

import base64
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zlib

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
LIB = ROOT / "lib/workstation"
sys.path[:0] = [str(HERE), str(LIB)]
import cache_policy_model as model
import hyperloom_workload as workload
from hyperloom_exchange import canonical, digest

IMPL = ROOT / ("scratchpad/mxfp4-ultraquant-feasibility-20261026-v1/fp8-fastpath-research-v2/"
               "hyperloom-sglang-implementation-20261102-v1")
PINNED_INPUTS = IMPL / "OWNER-BUILD-INPUTS.json"
PINNED_INPUTS_SHA = "818c09475f59db611ccaac5db24cd263cc18e8b826b98474c7fc031d98ca731c"
# Selected sixteen-request C4 donor: native prompt token counts per ordinal.
DONOR_TOKENS = (2040, 2040, 2040, 119990, 119990, 119977, 119977, 119977, 59993, 59980,
                2047, 2047, 724, 724, 2047, 2047)
DONOR_TASKS = ("warmup",) * 3 + ("long-retrieval",) * 5 + ("pair-retrieval",) * 2 \
    + ("ledger",) * 2 + ("service-config",) * 2 + ("ledger",) * 2
# Retained owner-serving-preparation-v4 packet (legacy -1 behaviour) after-pins.
LEGACY_PINS = {
    "HTTP-BINDING-control.json": "80ab3cd75295c803ef65415abd1e9ef020c5bda06ccf4a9d41dca2b8f237c7a6",
    "HTTP-BINDING-mtp.json": "628c99f8dd66d7d5b92b53d3f801cf1ce253ebf10964af3dadb27db1843e6277",
    "mtp_contract.py": "176e98d74b0ca3e768bca94a46a2c0622173623afe610259d82992914081adf1",
    "serving_integration.py": "9d1c9e1e0b88fce42d18eff203f1ddbe2e29343e58c60be24cd3ee815793337d",
}
FLAG = "--mamba-max-states-per-path"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def donor():
    """Fixture donor with the selected prompt lengths; messages and oracles are synthetic."""
    rows, plans, assets = [], [], {}
    for i, (tokens, task) in enumerate(zip(DONOR_TOKENS, DONOR_TASKS, strict=True)):
        ids = [7] * tokens
        body = dict(messages=[{"role": "user", "content": "fixture " + str(i)}],
                    cache_salt="old-" + str(i), rid="old-" + str(i), max_completion_tokens=4096,
                    reasoning_effort="low", chat_template_kwargs={"enable_thinking": True},
                    ignore_eos=False, stream=False)
        expected = dict(case=str(i), context_length=131040, input_tokens=tokens,
                        input_ids=ids, input_ids_sha256=digest(canonical(ids)),
                        messages_sha256=digest(canonical(body["messages"])),
                        task_grade={"kind": "exact_json", "expected": {"fixture": i}},
                        cache_policy={"intent": "cold", "minimum_cached_tokens": 0, "maximum_cached_tokens": 0})
        path, exp = f"r{i}.json", f"r{i}.expected.json"
        raw, exp_raw = canonical(body), canonical(expected)
        plan = dict(ordinal=i, case=str(i), category="timing", group=str(i), task=task, path=path,
                    expected=exp, input_tokens=tokens, maximum_output_tokens=4096, internal_reserve_tokens=16,
                    context_envelope_tokens=131040, sha256=digest(raw), bytes=len(raw), expected_sha256=digest(exp_raw))
        rows.append(dict(plan, block="paired", body=body, body_sha256=digest(raw), expected=expected))
        plans.append(plan)
        assets[path], assets[exp] = raw, exp_raw
    settings = dict(requests=plans, paired_serving={"context_length": 131040, "token_pool_capacity": 262144})
    logical = {
        "CLIENT-BINDING.zlib": zlib.compress(canonical({"rows": rows})),
        "FINAL-REQUESTS.zlib": zlib.compress(canonical({k: base64.b64encode(v).decode() for k, v in assets.items()})),
    }
    return {"FINAL-SETTINGS.json": canonical(settings),
            "FINAL-REQUESTS.zlib": zlib.compress(canonical({k: base64.b64encode(v).decode() for k, v in logical.items()}))}


def by_case(menu, results):
    return {menu["bound"][o]["expected"]["case"]: cached for o, cached in results.items()}


class PoolModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.menu = workload.derive(donor())

    def test_fixture_matches_the_selected_donor_when_present(self):
        inputs = PINNED_INPUTS
        if not inputs.is_file():
            self.skipTest("selected donor not present")
        root = Path(json.loads(inputs.read_bytes())["donor_root"])
        settings = json.loads((root / "author/package-v2/review/FINAL-SETTINGS.json").read_bytes())
        self.assertEqual(tuple(r["input_tokens"] for r in settings["requests"]), DONOR_TOKENS)
        self.assertEqual(tuple(r["task"] for r in settings["requests"]), DONOR_TASKS)

    def test_frozen_warm_policy_and_namespaces_are_the_retained_operation_200_values(self):
        self.assertEqual(workload.WARM_PERCENT, 95)
        rows = {r["expected"]["case"]: r for r in self.menu["bound"]}
        retained = {"long-c1-warm-r0": (113990, 119989, "hyperloom-long-c1-cold-r0-0"),
                    "long-c2-warm-r0-a": (56993, 59992, "hyperloom-long-c2-cold-r0-0"),
                    "long-c2-warm-r0-b": (56981, 59979, "hyperloom-long-c2-cold-r0-1")}
        for case, (minimum, maximum, salt) in retained.items():
            policy = rows[case]["expected"]["cache_policy"]
            self.assertEqual((policy["minimum_cached_tokens"], policy["maximum_cached_tokens"]), (minimum, maximum))
            self.assertEqual(rows[case]["body"]["cache_salt"], salt)
        self.assertEqual(len(self.menu["rows"]), 30)

    def test_cap_minus_one_reproduces_operation_200_under_b_first_admission(self):
        for finish_new_leaf in (False, True):
            with self.subTest(finish_new_leaf=finish_new_leaf):
                cases = by_case(self.menu, model.replay(self.menu, -1, finish_new_leaf, "b-first"))
                self.assertEqual(cases["long-c1-warm-r0"], 118784)
                self.assertEqual((cases["long-c2-warm-r0-a"], cases["long-c2-warm-r0-b"]), (0, 0))
                for row in self.menu["bound"]:
                    if row["expected"]["cache_policy"]["intent"] == "cold":
                        self.assertEqual(cases[row["expected"]["case"]], 0)

    def test_cap_three_meets_every_warm_minimum_for_three_repetitions(self):
        for finish_new_leaf in (False, True):
            for admission in ("b-first", "a-first"):
                with self.subTest(finish_new_leaf=finish_new_leaf, admission=admission):
                    cases = by_case(self.menu, model.replay(self.menu, 3, finish_new_leaf, admission))
                    warm = 0
                    for row in self.menu["bound"]:
                        policy = row["expected"]["cache_policy"]
                        cached = cases[row["expected"]["case"]]
                        if policy["intent"] == "cold":
                            self.assertEqual(cached, 0)
                            continue
                        warm += 1
                        self.assertGreaterEqual(cached, policy["minimum_cached_tokens"])
                        self.assertLessEqual(cached, policy["maximum_cached_tokens"])
                        expected = 118784 if "c1" in row["expected"]["case"] else 59392
                        self.assertEqual(cached, expected)
                    self.assertEqual(warm, 9)

    def test_matched_frontier_is_locked_before_the_cow_allocation(self):
        pool = model.Pool(-1, False)
        pool.group([("cold", "p", 4096, 10)])            # frontier 2048, prompt leaf 4096
        for i in range(model.POOL - 3):
            pool.group([(f"f{i}", f"q{i}", 1, 1)])
        frontier = pool.chains["p"][2048]
        self.assertTrue(frontier.value)
        matched = pool.match("p", 4096)
        self.assertIs(matched, frontier)
        self.assertTrue(frontier.locked)
        while pool.used() < model.POOL:
            pool.allocate()
            pool.pinned.add(len(pool.pinned))
        pool.allocate()                                  # a full pool evicts someone else
        self.assertTrue(frontier.value)

    def test_model_refuses_native_invalid_caps(self):
        for cap in (0, -2, True, 2.0):
            with self.assertRaises(ValueError):
                model.Pool(cap, False)
        with self.assertRaises(ValueError):
            model.replay(self.menu, 3, False, "c-first")


class OptInTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.builder = load("_test_hyperloom_build_owner_opt_in", HERE / "build_owner.py")

    def test_absent_input_is_legacy_and_only_three_opts_in(self):
        self.assertIsNone(self.builder.mamba_trim_cap({"schema": 1}))
        self.assertEqual(self.builder.mamba_trim_cap({"mamba_max_states_per_path": 3}), 3)
        for bad in (0, -2, -1, 1, 2, 4, True, False, "3", 3.0, None, [3]):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "reviewed integer 3"):
                self.builder.mamba_trim_cap({"mamba_max_states_per_path": bad})

    def test_build_refuses_bad_opt_in_before_creating_the_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp).resolve() / "packet"
            for bad in (0, -2, True):
                with self.assertRaisesRegex(ValueError, "reviewed integer 3"):
                    self.builder.build({"schema": 1, "mamba_max_states_per_path": bad}, destination)
                self.assertFalse(destination.exists())


@unittest.skipUnless(PINNED_INPUTS.is_file(), "selected donor inputs not present")
class InertBuildTests(unittest.TestCase):
    """Two fresh-process builds: legacy (no opt-in) and the cap-3 experiment."""

    @classmethod
    def setUpClass(cls):
        raw = PINNED_INPUTS.read_bytes()
        assert digest(raw) == PINNED_INPUTS_SHA
        cls.base = json.loads(raw)
        if not Path(cls.base["donor_root"]).is_dir():
            raise unittest.SkipTest("selected donor root not present")
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name).resolve()
        cls.legacy = cls.build(root, "legacy", cls.base)
        cls.cap3 = cls.build(root, "cap3", dict(cls.base, mamba_max_states_per_path=3))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def build(cls, root, name, inputs):
        raw = canonical(inputs)
        path = root / (name + "-inputs.json")
        path.write_bytes(raw)
        out = root / name
        env = {"PATH": os.environ.get("PATH", "")}
        argv = [sys.executable, "-I", "-S", "-B", str(HERE / "build_owner.py"), "--inputs", str(path),
                "--inputs-sha256", digest(raw), "--output", str(out)]
        done = subprocess.run(argv, capture_output=True, text=True, env=env, cwd=str(root), timeout=600)
        if done.returncode != 0:
            raise AssertionError(name + " build failed:\n" + done.stderr[-4000:])
        return out

    def read(self, root, name):
        return (root / "assembled-review" / name).read_bytes()

    def test_absent_opt_in_keeps_the_retained_legacy_contract_bytes(self):
        delta = json.loads((self.legacy / "PACKET-DELTA.json").read_bytes())
        for name, pin in LEGACY_PINS.items():
            self.assertEqual(delta[name]["after_sha256"], pin, name)
        self.assertNotIn("serving_entry.py", delta)
        ids = json.loads((self.legacy / "PACKAGE-IDENTITIES.json").read_bytes())
        self.assertNotIn("runtime_config_deviation", ids)
        self.assertNotIn("mamba_max_states_per_path", ids)
        for arm in ("control", "mtp"):
            template = json.loads(self.read(self.legacy, f"HTTP-BINDING-{arm}.json"))["binding_template"]
            self.assertNotIn(FLAG, template["canonical_server_cli"])
            self.assertNotIn("mamba_max_states_per_path", template["resolved_contract"]["prefix_cache"])

    def test_workload_menu_and_warm_policy_are_byte_identical(self):
        self.assertEqual((self.legacy / "WORKLOAD.json").read_bytes(), (self.cap3 / "WORKLOAD.json").read_bytes())
        for name in ("FINAL-SETTINGS.json", "FINAL-REQUESTS.zlib", "COMPOSITION-FACTS.zlib"):
            self.assertEqual(self.read(self.legacy, name), self.read(self.cap3, name), name)
        legacy = json.loads((self.legacy / "PACKAGE-IDENTITIES.json").read_bytes())
        cap3 = json.loads((self.cap3 / "PACKAGE-IDENTITIES.json").read_bytes())
        for key in ("workload_sha256", "requests_per_start", "starts", "caller_seconds", "owner_seconds",
                    "baseline_sha256", "candidate_sha256", "changed_functions", "kind"):
            self.assertEqual(legacy[key], cap3[key], key)
        self.assertEqual(cap3["candidate_sha256"], self.builder_c4())
        self.assertEqual(cap3["changed_functions"], [])

    def builder_c4(self):
        return "b5d145e55f391de7e515cfe164b6011444fe1696188e2c0f900e338ce36c22f0"

    def test_both_arm_bindings_carry_the_option_exactly_once(self):
        for arm in ("control", "mtp"):
            binding = json.loads(self.read(self.cap3, f"HTTP-BINDING-{arm}.json"))
            template = binding["binding_template"]
            cli = template["canonical_server_cli"]
            self.assertEqual(cli.count(FLAG), 1)
            at = cli.index(FLAG)
            self.assertEqual(cli[at:at + 2], [FLAG, "3"])
            self.assertEqual(cli[at - 2:at], ["--max-mamba-cache-size", "19"])
            self.assertEqual(template["resolved_contract"]["prefix_cache"]["mamba_max_states_per_path"], 3)
            legacy = json.loads(self.read(self.legacy, f"HTTP-BINDING-{arm}.json"))
            legacy_cli = legacy["binding_template"]["canonical_server_cli"]
            self.assertEqual(cli[:at] + cli[at + 2:], legacy_cli)
            legacy["binding_template"]["canonical_server_cli"] = cli
            legacy["binding_template"]["resolved_contract"]["prefix_cache"]["mamba_max_states_per_path"] = 3
            legacy["binding_template"]["bytecode_masks"] = template["bytecode_masks"]
            legacy["binding_template"]["sources"] = template["sources"]
            legacy["binding_template"]["source_manifest_sha256"] = template["source_manifest_sha256"]
            legacy["source_manifest_sha256"] = binding["source_manifest_sha256"]
            legacy["worker_template"] = binding["worker_template"]
            self.assertEqual(legacy, binding, arm)

    def test_generated_contract_resolves_the_cap_and_refuses_wrong_cli(self):
        review = self.cap3 / "assembled-review"
        sys.path.insert(0, str(review))
        try:
            contract = load("_test_hyperloom_cap3_mtp_contract", review / "mtp_contract.py")
            self.assertEqual(contract.cache_config()["mamba_max_states_per_path"], 3)
            binding = json.loads(self.read(self.cap3, "HTTP-BINDING-control.json"))["binding_template"]
            prefix = binding["resolved_contract"]["prefix_cache"]
            self.assertEqual(contract.validate_cache_config(prefix), prefix)
            cli = binding["canonical_server_cli"]
            contract.validate_mamba_trim_cli(cli)
            at = cli.index(FLAG)
            for wrong in (cli[:at] + cli[at + 2:], cli + [FLAG, "3"], cli[:at + 1] + ["2"] + cli[at + 2:],
                          cli[:at + 1] + ["-1"] + cli[at + 2:], cli[:at + 1] + ["0"] + cli[at + 2:], None):
                with self.assertRaises(ValueError):
                    contract.validate_mamba_trim_cli(wrong)
            for wrong in (dict(prefix, mamba_max_states_per_path=-1), dict(prefix, mamba_max_states_per_path=True),
                          {k: v for k, v in prefix.items() if k != "mamba_max_states_per_path"}):
                with self.assertRaises(ValueError):
                    contract.validate_cache_config(wrong)
            legacy = load("_test_hyperloom_legacy_mtp_contract", self.legacy / "assembled-review/mtp_contract.py")
            self.assertNotIn("mamba_max_states_per_path", legacy.cache_config())
            self.assertFalse(hasattr(legacy, "validate_mamba_trim_cli"))
        finally:
            sys.path.remove(str(review))

    def test_production_entry_and_source_audit_carry_the_binding(self):
        entry = self.read(self.cap3, "serving_entry.py").decode()
        audit = self.read(self.cap3, "serving_integration.py").decode()
        self.assertEqual(entry.count("mtp_contract.validate_mamba_trim_cli(value['canonical_server_cli'])"), 1)
        self.assertEqual(audit.count("_mtp_contract().validate_mamba_trim_cli(binding.get('canonical_server_cli'))"), 1)
        self.assertEqual(self.read(self.legacy, "serving_entry.py").decode().count("validate_mamba_trim_cli"), 0)
        delta = json.loads((self.cap3 / "PACKET-DELTA.json").read_bytes())
        for name in ("mtp_contract.py", "serving_entry.py", "serving_integration.py"):
            self.assertFalse(delta[name]["protected"], name)
            self.assertTrue((self.cap3 / "diffs" / (name + ".diff")).is_file(), name)
        legacy_delta = json.loads((self.legacy / "PACKET-DELTA.json").read_bytes())
        protected = {n for n, row in delta.items() if row["protected"]}
        self.assertEqual(protected, {n for n, row in legacy_delta.items() if row["protected"]})
        for name in protected:
            legacy, cap3 = self.read(self.legacy, name).splitlines(), self.read(self.cap3, name).splitlines()
            changed = [(a, b) for a, b in zip(legacy, cap3, strict=True) if a != b]
            if name == "benchmark_physical_controller.py":
                # Established derived pin only: the spec digest follows the changed bindings.
                self.assertEqual([a.split(b" = ")[0] for a, _ in changed], [b"SPEC_SHA256"], name)
            else:
                self.assertEqual(changed, [], name)

    def test_identities_declare_the_deviation_without_claiming_unchanged_behavior(self):
        ids = json.loads((self.cap3 / "PACKAGE-IDENTITIES.json").read_bytes())
        self.assertEqual(ids["mamba_max_states_per_path"], 3)
        deviation = ids["runtime_config_deviation"]
        self.assertEqual((deviation["option"], deviation["value"], deviation["selected_native_default"]), (FLAG, 3, -1))
        self.assertEqual(deviation["arms"], ["control", "mtp"])
        self.assertIs(deviation["selected_runtime_behavior_unchanged"], False)
        self.assertIs(deviation["adopted_runtime"], False)
        self.assertIs(deviation["requires_owner_decision_and_fresh_gpu_qualification"], True)
        for arm in ("control", "mtp"):
            self.assertEqual(deviation["bindings"][arm]["sha256"], digest(self.read(self.cap3, f"HTTP-BINDING-{arm}.json")))
        for flag in ("launch_authorized", "qualification", "selected", "adoption_authorized", "automatic_retry"):
            self.assertIs(ids[flag], False)
        manifest = json.loads((self.cap3 / "package-v2/MANIFEST.json").read_bytes())
        legacy_manifest = json.loads((self.legacy / "package-v2/MANIFEST.json").read_bytes())
        self.assertEqual(manifest["runtime_files"], legacy_manifest["runtime_files"])
        for arm in ("control", "mtp"):
            self.assertEqual(manifest["package_files"]["review/HTTP-BINDING-" + arm + ".json"],
                             deviation["bindings"][arm]["sha256"])


if __name__ == "__main__":
    unittest.main()
