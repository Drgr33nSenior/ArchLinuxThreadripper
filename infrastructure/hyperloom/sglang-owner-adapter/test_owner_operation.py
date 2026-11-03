"""CPU acceptance of the bounded owner operation seam.

One real inert serving preparation and one real profile preparation are built
once per class into a temporary tree with the actual builders, sealer and
grading template. The caller, the retained replay binder and the native CPU
grading commands are fixtures. No SSH, GPU, real registration or checkpoint
outside the temporary tree is touched.
"""

import copy
import base64
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import sys
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
INFRA = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(INFRA / "lib/workstation"))
sys.path.insert(0, str(INFRA / "tests/workstation"))
sys.path.insert(0, str(HERE))
import hyperloom_exchange as exchange
import hyperloom_exchange_host as host
import hyperloom_owner_backend as backend
import owner_operation as operation
from test_hyperloom_latency import POLICY

I = operation.R2 / "hyperloom-sglang-implementation-20261102-v1"
CANDIDATE = operation.R2 / "phase2-128k-c4ab-ff396f8f-preparation-v2/author/package-v2/review/extend_attention_fwd_candidate.py"
OWNER_INPUTS = dict(path=str(I / "OWNER-BUILD-INPUTS.json"),
                    sha256="818c09475f59db611ccaac5db24cd263cc18e8b826b98474c7fc031d98ca731c")
SUPPORT_INPUTS = dict(path=str(I / "SUPPORT-BUILD-INPUTS-serving-v4.json"),
                      sha256="5100034a70717b11c29086bedcfa9504d950ddff003337f48d19013a3f3e3d6b")
PROFILE_INPUTS = dict(path=str(I / "OWNER-PROFILE-INPUTS.json"),
                      sha256="19cc75c73c9071b1a5930fa93cd640d5a204c5a5695fa6cf4d60eb319b3f1a9f")
PROFILE_SUPPORT = dict(path=str(I / "SUPPORT-BUILD-INPUTS-profile-v2.json"),
                       sha256="398096ea991139fb2e6566f606a32976547577efa52ae252923d0f7a81ccd24a")
WORKLOAD_SHA = "0b01b0312b496447be762c453b07efe80f90fdd273bd9badaf8c42c4b5d9519d"
FIXED = {"framework": "sglang", "model": "/frozen/model", "runner_type": "gfx12", "precision": "mxfp4",
         "envs": {"TP": "2"}, "workstation_workload": "balanced_c1_c2"}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def idle_checkpoint(**changes):
    state = {"schema": 1, "active_caller": None, "active_cpu_owner": None, "active_gpu_owner": None,
             "active_owner": None, "current_global_attempt_index": None, "reservation": None,
             "last_completed_global_attempt_index": 190, "cumulative_owner_seconds": 64039.1,
             "cumulative_caller_seconds": 65169.2, "independent_release_verified": True,
             "remaining_gpu_allowance": 0, "retry_authorized": False, "deployed": False, "selected": False,
             "status": "COMPONENT_COMPLETE_PARKED_UNSELECTED", "utc": "2026-11-02T01:06:14+00:00",
             "previous_checkpoint": None, "previous_checkpoint_sha256": None, "next_action": "PARK."}
    state.update(changes)
    return state


def profile_bundles(preparation):
    """Real packet schema, synthetic trace bytes; no framework or GPU import."""
    review = Path(preparation["author_root"]) / "package-v2/review"
    manifest = json.loads((review.parent / "MANIFEST.json").read_bytes())
    module = operation._source(review / "serving_profile.py", manifest["package_files"]["review/serving_profile.py"],
                               "_fixture_original_profile")
    frozen = module.REQUEST["requests"]
    ids = [r["rid"] for r in frozen]
    total = sum(r["input_tokens"] for r in frozen)
    workers = [dict(rank=rank, pid=100 + rank, identity_sha256=str(rank) * 64) for rank in (0, 1)]
    bundles = {}
    for arm, logical in (("bf16", "control"), ("kv4", "mtp")):
        expected = dict(arm=logical, session_sha256="1" * 64, spec_sha256="2" * 64, source_manifest_sha256="3" * 64)
        observations = {}
        if arm == "kv4":
            for worker in workers:
                rank = worker["rank"]
                row = dict(schema=1, rank=rank, pid=worker["pid"], worker_identity_sha256=worker["identity_sha256"],
                    **expected, request=module.REQUEST, errors=[], numerical_acceptance=False, timing_claim=False,
                    prefill_batches=60, prefill_tokens=total, decode_batches=4,
                    request_progress={r["rid"]: r["input_tokens"] for r in frozen},
                    captures=[], batch_coverage=dict(prefill=[], decode=[]))
                for phase, count in (("prefill", 3), ("decode", 4)):
                    for index in range(count):
                        batch = dict(batch_size=2, rids=list(ids), batch_index=index)
                        if phase == "prefill":
                            before, after = [(0, 2048), (total // 2 - 1024, total // 2 + 1024),
                                             (total - 2048, total)][index]
                            batch.update(prompt_tokens_before=before, prompt_tokens_after=after)
                        row["batch_coverage"][phase].append(dict(label=index, **batch))
                        trace = exchange.canonical(dict(traceEvents=[
                            dict(cat="kernel", ph="X", name="synthetic", ts=rank * 100 + index, dur=1 + index),
                            dict(cat="cpu_op", ph="X", name=phase, ts=0, dur=1)]))
                        raw = gzip.compress(trace, mtime=0)
                        name = "PROFILE-rank-%d-%s-%02d.json.gz" % (rank, phase, index)
                        observations[name] = raw
                        row["captures"].append(dict(phase=phase, index=index, file=name, status="COMPLETE", batch=batch,
                            bytes=len(raw), sha256=digest(raw), raw_bytes=len(trace), raw_sha256=digest(trace),
                            summary=module.trace_summary(trace)))
                observations["PROFILE-rank-%d.json" % rank] = exchange.canonical(row)
        module.validate_observations(observations, workers, expected)
        bundle = dict(schema=1, workers=workers, observations={name: dict(bytes=len(raw), sha256=digest(raw),
            base64=base64.b64encode(raw).decode()) for name, raw in observations.items()})
        raw = exchange.canonical(bundle)
        bundles[arm] = dict(raw=raw, reference=dict(owner_arm=arm, logical_arm=logical, binding=expected,
                                                 native_bundle_sha256=digest(raw)))
    return bundles


class Commands:
    """Fixture at the finite command boundary; records every invocation."""

    def __init__(self, root, handlers, sequence):
        self.ROOT, self.handlers, self.sequence, self.calls = Path(root), handlers, sequence, []

    def run(self, name, argv, seconds=60, *, stdin=None, cwd=None, output_limit=16 << 20):
        self.calls.append(dict(name=name, argv=list(argv), seconds=seconds, cwd=str(cwd)))
        self.sequence.append(name)
        handler = self.handlers.get(name)
        if handler is None:
            raise AssertionError("unexpected finite command " + name)
        failure, stdout = None, b""
        try:
            stdout = handler(list(argv), Path(cwd)) or b""
        except Exception as error:
            failure = error
        (self.ROOT / (name + ".stdout")).write_bytes(stdout)
        (self.ROOT / (name + ".stderr")).write_bytes(b"")
        receipt = dict(argv=list(argv), cwd=str(cwd), work_seconds=seconds, term_seconds=5, reap_seconds=5,
                       stdin_sha256=None, output_limit_bytes=output_limit, pid=4242, pgid=4242, signals=[],
                       exit_code=1 if failure else 0, child_reaped=True, owned_group_absent=True,
                       elapsed_seconds=930.5 if name == "caller-once" else 1.5,
                       stdout=dict(bytes=len(stdout), sha256=digest(stdout)), stderr=dict(bytes=0, sha256=digest(b"")))
        (self.ROOT / (name + ".json")).write_text(json.dumps(receipt, indent=2, sort_keys=True))
        if failure is not None:
            raise RuntimeError("command failed or owned process closure uncertain: " + name) from failure
        return receipt


class Binder:
    """Fixture at the retained-replay boundary; writes what bind_future.bind would."""

    def __init__(self, evidence, author_seal, sequence, *, fail=False, wrong_seal=False):
        self.evidence, self.author_seal, self.sequence = evidence, author_seal, sequence
        self.fail, self.wrong_seal, self.calls = fail, wrong_seal, []

    def bind(self, **request):
        self.calls.append(request)
        self.sequence.append("bind")
        if self.fail:
            raise ValueError("retained serving replay: fixture replay refused")
        destination = Path(request["destination"])
        destination.mkdir(mode=0o700)
        for arm in ("bf16", "kv4"):
            paired = destination / "serving-replay" / (arm + "-http-client-tree") / "paired"
            paired.mkdir(parents=True)
            (paired / "RUN-SUMMARY.json").write_bytes(exchange.canonical(self.evidence["summaries"][arm]))
        (destination / "cpu").mkdir()
        (destination / "cpu/ACTUAL-INPUT-BINDING.json").write_bytes(exchange.canonical({"fixture": True}))
        seal = ("0" * 64) if self.wrong_seal else request["operation_seal_sha256"]
        artifacts = {}
        for arm, item in self.evidence.get("bundles", {}).items():
            extracted = destination / "serving-replay" / (arm + "-artifacts")
            extracted.mkdir()
            (extracted / "native-http-bundle.json").write_bytes(item["raw"])
            artifacts[arm] = item["reference"]
        count = len(self.evidence["decoded"]["answers"])
        return dict(schema=1, status="ACTUAL_%d_INPUT_BOUND_BY_RECOMPUTED_OWNER_RESOURCE_RELEASE_HTTP" % count, rows=count,
                    cancellations=0, precision_accepted=False, qualification=False, native_quality_grade=False,
                    execution_authorized=False,
                    request={k: v for k, v in request.items() if k not in ("destination", "projection_inventory_sha256")},
                    replay_provenance=dict(artifacts=artifacts, seals=dict(
                        operation=dict(root=request["operation_root"], manifest_sha256=seal, members=258),
                        workload=dict(root="/home/developer/fixture", manifest_sha256=request["workload_seal_sha256"], members=165),
                        protected=dict(root="/var/tmp/fixture", manifest_sha256=request["protected_seal_sha256"], members=31),
                        source_author=dict(self.author_seal))))


class OwnerOperationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name).resolve()
        for name in ("requests", "replies", "snapshots", "preparations", "campaigns", "mx", "checkpoints", "authority"):
            (cls.root / name).mkdir()
        cls.bound = {"schema": 1, "backend": backend.NAME, "campaign_sha256": "a" * 64, "selection_sha256": "b" * 64,
                     "controller_sha256": "c" * 64, "runtime_sha256": "d" * 64, "workload_sha256": WORKLOAD_SHA,
                     "campaign_deadline_unix_ms": int((time.time() + 14400) * 1000),
                     "requests_root": str(cls.root / "requests"), "replies_root": str(cls.root / "replies"),
                     "sources": {operation.EVALUABLE: str(CANDIDATE)}, "benchmark": dict(FIXED),
                     "selected_source_set_sha256": "e" * 64, "profile_primary_window": "decode-0",
                     "caller_seconds": {"serving": 7160, "profile": 7160, "component": 2190},
                     "latency_policy": dict(POLICY), "automatic_retry": False, "adoption_authorized": False}
        cls.config = {"benchmark": dict(FIXED, timeout_seconds=7660)}
        cls.snapshot, cls.request = cls.take(cls.config, "serving")
        cls.preparation = operation.prepare(cls.snapshot, cls.bound, destination=cls.root / "preparations/serving",
                                            owner_inputs=OWNER_INPUTS, support_inputs=SUPPORT_INPUTS)
        if cls.preparation["status"] != "PREPARED_NOT_REGISTERED_OR_RUN":
            raise AssertionError("real serving preparation failed: " + str(cls.preparation.get("reason")))
        profile_config = {"benchmark": dict(FIXED, timeout_seconds=7660, profiler={"torch_profiler": {"enabled": True}})}
        cls.profile_snapshot, cls.profile_request = cls.take(profile_config, "profile")
        cls.profile = operation.prepare(cls.profile_snapshot, cls.bound, destination=cls.root / "preparations/profile",
                                        owner_inputs=PROFILE_INPUTS, support_inputs=PROFILE_SUPPORT)
        if cls.profile["status"] != "PREPARED_NOT_REGISTERED_OR_RUN":
            raise AssertionError("real profile preparation failed: " + str(cls.profile.get("reason")))

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    @classmethod
    def take(cls, config, kind, sources=None):
        bound = dict(cls.bound, sources=sources or cls.bound["sources"])
        job, request = exchange.submit(cls.root / "requests", campaign_sha256=bound["campaign_sha256"],
                                       selection_sha256=bound["selection_sha256"], kind=kind, config=config,
                                       sources=bound["sources"], caller_seconds=bound["caller_seconds"][kind],
                                       campaign_deadline_unix_ms=bound["campaign_deadline_unix_ms"])
        taken = host.dispatch("take", bound, job_id=request["id"])
        target = cls.root / "snapshots" / request["id"]
        host.retain_snapshot(taken, target, bound)
        return target, request

    def checkpoints(self, **changes):
        root = Path(tempfile.mkdtemp(dir=self.root / "checkpoints")).resolve()
        (root / "CHECKPOINT-527.json").write_text(json.dumps(idle_checkpoint(**changes), indent=2, sort_keys=True) + "\n")
        return root

    def propose(self, preparation=None, checkpoint_root=None):
        preparation = preparation or self.preparation
        prop = operation.propose(preparation["record"], self.bound,
                                 campaign_parent=Path(tempfile.mkdtemp(dir=self.root / "campaigns")).resolve(),
                                 checkpoint_root=checkpoint_root or self.checkpoints(),
                                 mx_root=Path(tempfile.mkdtemp(dir=self.root / "mx")).resolve())
        self.assertEqual(prop["status"], "PROPOSED_NOT_REGISTERED_OR_RUN", prop.get("reason"))
        return prop

    def authorization(self, prop, request=None, **changes):
        request = request or self.request
        stem = self.root / "authority" / (prop["nonce"] + "-" + str(len(list((self.root / "authority").iterdir()))))
        approval = stem.with_suffix(".approval.json")
        approval.write_bytes(exchange.canonical({"fixture_approval": True, "gpu_work": False}))
        record = dict(schema=1, kind=prop["kind"], campaign=prop["campaign_root"], proposal_sha256=prop["proposal"]["sha256"],
                      execute_once_sha256=prop["execute_once"]["sha256"], nonce=prop["nonce"],
                      global_gpu_attempt_index=prop["index"], request_sha256=exchange.digest(exchange.canonical(request)),
                      source_set_sha256=request["source_set_sha256"], candidate_sha256=operation.C4_SHA,
                      campaign_sha256=self.bound["campaign_sha256"], controller_sha256=self.bound["controller_sha256"],
                      owner="fixture-owner", owner_answer="Fixture approval of one bounded CPU test; no GPU work.",
                      approval=dict(path=str(approval), sha256=digest(approval.read_bytes())), no_retry=True,
                      one_invocation=True, deployment=False, promotion=False, publication=False,
                      adoption_authorized=False, qualification=False, utc="2026-11-02T00:00:00+00:00")
        record.update(changes)
        path = stem.with_suffix(".authorization.json")
        raw = exchange.canonical(record)
        path.write_bytes(raw)
        return dict(path=str(path), sha256=digest(raw))

    def evidence(self, preparation=None):
        """Synthetic owner evidence bound to the real prepared menu identities."""
        preparation = preparation or self.preparation
        workload = json.loads(Path(preparation["workload"]["path"]).read_bytes())
        plans = json.loads((Path(preparation["author_root"]) / "assembled-review/FINAL-SETTINGS.json").read_bytes())["requests"]
        count = len(plans)
        decoded = {"answers": []}
        quality = dict(status="NATIVE_CPU_FINAL_QUALITY_AND_RELEASE_VALIDATED", independent_release_verified=True,
                       quality_pass=True, long_answer_coverage_pass=True, decoded_rows=2 * count, natural_rows=2 * count,
                       ungraded_cap_work_rows=0, state_only_cancellations=0, qualification=False, selected=False,
                       retry_authorized=False, native_generation_performed=False, precision_accepted=False, grades=[])
        summaries = {}
        for physical, logical in (("bf16", "control"), ("kv4", "mtp")):
            rows = []
            for plan in plans:
                i = plan["ordinal"]
                raw_response = digest((physical + str(i)).encode())
                canonical_response = digest(("canonical" + physical + str(i)).encode())
                ids_sha = digest(("ids" + physical + str(i)).encode())
                segments = dict(reasoning_token_count=20, final_answer_token_count=79, closing_marker_index=20)
                contract = dict(case=plan["case"], request_sha256=plan["sha256"], raw_http_response_sha256=raw_response,
                                raw_response_sha256=canonical_response, response_token_ids_sha256=ids_sha,
                                status="NORMAL_STOP_COMPLETE", finish_reason="stop", metric_eligible=True,
                                response_id_count=100, reported_completion_tokens=100,
                                client_elapsed_seconds=(10.0 if logical == "control" else 9.0), prompt_token_count=4,
                                thinking_segments=segments)
                rows.append(dict(ordinal=i, case=plan["case"], body_sha256=plan["sha256"], contract=contract))
                decoded["answers"].append(dict(arm=physical, ordinal=i, case=plan["case"], request_body_sha256=plan["sha256"],
                    response_raw_sha256=raw_response, response_canonical_sha256=canonical_response,
                    output_ids_sha256=ids_sha, output_tokens=100, final_status="FINAL_SEGMENT_DECODED",
                    finish_reason="stop", wire_decode_matches=True, error=None, **segments))
                quality["grades"].append(dict(arm=physical, ordinal=i, case=plan["case"], status="QUALITY_PASS",
                    quality_passed=True, long_answer_coverage_passed=True, final_answer_token_count=79))
            groups = []
            for group in workload["groups"]:
                times = [rows[i]["contract"]["client_elapsed_seconds"] for i in group["ordinals"]]
                groups.append(dict(group_id=group["id"], ordinals=list(group["ordinals"]), kind=group["kind"],
                                   concurrency=group["concurrency"], measured=group["kind"] == "timing",
                                   excluded_warmup=group["kind"] == "warmup", per_request_latency_seconds=times,
                                   completion_tokens=100 * group["concurrency"], makespan_seconds=max(times) + .01))
            summaries[physical] = dict(stopped_early=False, paired_error=None, executed_case_count=count,
                                       frozen_case_count=count, rows=rows, groups=groups)
        return dict(summaries=summaries, decoded=decoded, quality=quality,
                    bundles=profile_bundles(preparation) if preparation["kind"] == "profile" else {})

    def caller(self, prop, mode="complete", preparation=None):
        """The caller boundary: writes what launcher/execute_once.py leaves behind."""
        preparation = preparation or self.preparation
        bindings = json.loads(Path(preparation["launcher_bindings"]["path"]).read_bytes())

        def handler(argv, cwd):
            run_root = Path(prop["native_root"])
            run_root.mkdir()
            (Path(prop["campaign_root"]) / "CALLER-INVOKED.json").write_text("{}\n")
            complete = mode in ("complete", "unreleased")
            steps = [dict(name="power-pre", exit_code=0, elapsed_seconds=0.3), dict(name="upload", exit_code=0, elapsed_seconds=0.8),
                     dict(name="launch", exit_code=0 if complete else 1, elapsed_seconds=900.0)]
            summary = dict(schema=1, status="COMPLETE" if complete else "INCOMPLETE_RETAINED", steps=steps,
                           global_operation=prop["index"], automatic_retry=False, run_root=str(run_root),
                           physical_success=complete, qualification=False, selection=False)
            (run_root / "EXECUTION-SUMMARY.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
            manifest = b"fixture manifest\n"
            (run_root / "FINAL-ARTIFACTS.sha256").write_bytes(manifest)
            seal = digest(manifest)
            run_root.with_name(run_root.name + ".closeout.json").write_text(json.dumps(
                dict(root=str(run_root), manifest_sha256=seal, members=1, qualification=False)))
            (run_root / "collected/workload").mkdir(parents=True)
            (run_root / "collected/protected").mkdir(parents=True)
            (run_root / "collected/workload" / (Path(bindings["target_root"]).name + ".closeout.json")).write_text(json.dumps(
                dict(root=bindings["target_root"], manifest_sha256="7" * 64, member_count=165, qualification=False)))
            (run_root / "collected/protected" / (Path(bindings["protected_root"]).name + ".closeout.json")).write_text(json.dumps(
                dict(root=bindings["protected_root"], workload_evidence_root=bindings["target_root"], manifest_sha256="8" * 64,
                     physical_success=mode == "complete", qualification=False)))
            if not complete:
                raise RuntimeError("caller exit 1")
            return (json.dumps(dict(root=str(run_root), manifest_sha256=seal)) + "\n"
                    + json.dumps(dict(seal=seal, status="COMPLETE"), sort_keys=True) + "\n").encode()
        return handler

    def grading(self, evidence, mutate=None):
        quality = copy.deepcopy(evidence["quality"])
        decoded = copy.deepcopy(evidence["decoded"])
        summary = quality

        def grade(argv, cwd):
            nonlocal summary
            if mutate is not None:
                summary = mutate(decoded, quality) or quality
            (cwd / "retained-cpu/output/quality").mkdir(parents=True)
            (cwd / "retained-cpu/output/quality/DECODE-RESULT.json").write_bytes(exchange.canonical(decoded))
            (cwd / "RESULT-SUMMARY.json").write_bytes(exchange.canonical(summary))
            return b""

        def readout(argv, cwd):
            return exchange.canonical(quality)
        return {"cpu-grade-once": grade, "cpu-readout-once": readout}

    def run_once(self, prop, *, mode="complete", bind_fail=False, wrong_seal=False, mutate=None, cancelled=None,
                 checkpoint_root=None, authorization=None, commands=None, evidence=None):
        sequence = []
        preparation = self.profile if prop["kind"] == "profile" else self.preparation
        request = self.profile_request if prop["kind"] == "profile" else self.request
        evidence = evidence or self.evidence(preparation)
        handlers = {"caller-once": self.caller(prop, mode, preparation), **self.grading(evidence, mutate)}
        commands = commands or Commands(prop["campaign_root"], handlers, sequence)
        binder = Binder(evidence, preparation["seals"]["author"], sequence, fail=bind_fail, wrong_seal=wrong_seal)
        result = operation.execute(prop["record"], authorization or self.authorization(prop, request), self.bound,
                                   checkpoint_root=checkpoint_root or Path(prop["checkpoint"]["path"]).parent,
                                   cancelled=cancelled or (lambda: False), commands_module=commands, binder=binder.bind,
                                   now=request["created_unix_ms"] / 1000 + 5)
        return result, commands, binder, sequence

    def assert_untouched(self, prop, checkpoint_root):
        campaign = Path(prop["campaign_root"])
        self.assertEqual(sorted(p.name for p in campaign.iterdir()),
                         ["OWNER-PROPOSAL.json", "REGISTRATION-PROPOSED.json", "execute_once.py"])
        self.assertEqual([p.name for p in Path(checkpoint_root).iterdir()], ["CHECKPOINT-527.json"])
        self.assertFalse(Path(prop["native_root"]).exists())

    def test_preparation_is_inert_sealed_and_source_bound(self):
        prep = self.preparation
        self.assertEqual(prep["kind"], "serving")
        self.assertFalse(prep["launch_authorized"] or prep["registered"] or prep["gpu_work"] or prep["native_action"])
        sealer = operation.sealer()
        for label, root in (("author", prep["author_root"]), ("preparation", prep["preparation_root"])):
            self.assertEqual(sealer.verify(Path(root), prep["seals"][label]["manifest_sha256"]), prep["seals"][label])
        binding = json.loads(Path(prep["source_binding"]["path"]).read_bytes())
        self.assertEqual(binding["request_sha256"], exchange.digest(exchange.canonical(self.request)))
        self.assertEqual(binding["candidate_sha256"], operation.C4_SHA)
        self.assertEqual(binding["source"]["name"], operation.EVALUABLE)
        self.assertEqual(prep["package"]["workload_sha256"], WORKLOAD_SHA)
        self.assertEqual(prep["limits"], dict(owner_seconds=6360, caller_seconds=7160, native_launch_seconds=6440,
                                              model_starts=2, worker_seconds=2280, client_seconds=2040,
                                              cpu_grading_seconds=320, requests=60, natural_grades=60, native_decodes=60))
        self.assertEqual(prep["receipts"]["owner_build"]["argv"][:4], [operation.PYTHON, "-I", "-S", "-B"])
        self.assertFalse((Path(prep["preparation_root"]) / "REGISTRATION.json").exists())
        self.assertEqual(operation.CALLER_WORK_SECONDS, 7150)

    def test_unsupported_source_surface_refuses_before_any_build(self):
        snapshot, _ = self.take(self.config, "serving", sources={"decode_attention_fwd_candidate.py": str(CANDIDATE)})
        spy = Commands(self.root, {}, [])
        record = operation.prepare(snapshot, self.bound, destination=self.root / "preparations/unsupported",
                                   owner_inputs=OWNER_INPUTS, support_inputs=SUPPORT_INPUTS, commands_module=spy)
        self.assertEqual(record["status"], "UNSUPPORTED")
        self.assertIn("extend_attention_fwd_candidate.py", record["reason"])
        self.assertEqual(spy.calls, [])
        self.assertFalse((self.root / "preparations/unsupported").exists())

    def test_cache_configuration_requires_a_distinct_declared_runtime(self):
        root = Path(tempfile.mkdtemp(dir=self.root)).resolve()
        donor = I / "warm-c2-policy-repair-v1/author-cap3/package-v2/review"
        runtime = dict(schema=1, selection_sha256=self.bound["selection_sha256"],
            configuration_overrides={"mamba_max_states_per_path": 3}, selected_runtime_behavior_unchanged=False,
            adopted=False, physically_qualified=False, serving_arms={})
        for arm in ("control", "mtp"):
            binding = json.loads((donor / ("HTTP-BINDING-" + arm + ".json")).read_bytes())["binding_template"]
            runtime["serving_arms"][arm] = {k: binding[k] for k in ("canonical_server_cli", "resolved_contract")}
        runtime_pin = operation._write(root / "runtime.json", runtime)
        bound = dict(self.bound, runtime_sha256=runtime_pin["sha256"], runtime_configuration=runtime_pin)
        template = operation._json(Path(OWNER_INPUTS["path"]))
        cap3 = dict(template, mamba_max_states_per_path=3)
        with self.assertRaises(ValueError):
            operation._runtime_configuration(self.bound, cap3)
        with self.assertRaises(ValueError):
            operation._runtime_configuration(dict(bound, runtime_sha256=self.bound["runtime_sha256"]), cap3)
        with self.assertRaises(ValueError):
            operation._runtime_configuration(bound, template)
        for value in (True, 3.0, -1, 0):
            with self.subTest(value=value), self.assertRaises(ValueError):
                operation._runtime_configuration(bound, dict(template, mamba_max_states_per_path=value))
        inputs = operation._write(root / "inputs.json", cap3)
        snapshot, request = self.take(self.config, "serving")
        preparation = operation.prepare(snapshot, bound, destination=root / "preparation", owner_inputs=inputs,
                                        support_inputs=SUPPORT_INPUTS, now=request["created_unix_ms"] / 1000)
        self.assertEqual(preparation["status"], "PREPARED_NOT_REGISTERED_OR_RUN", preparation.get("reason"))
        source = operation._json(Path(preparation["source_binding"]["path"]))
        self.assertFalse(source["model_precision_context_flags_power_overlays_unchanged"])
        self.assertEqual(source["runtime_configuration"], runtime_pin)
        self.assertEqual(source["runtime_sha256"], runtime_pin["sha256"])
        self.assertEqual(source["runtime_config_deviation"]["value"], 3)
        prop = operation.propose(preparation["record"], bound, campaign_parent=root,
                                 checkpoint_root=self.checkpoints(), mx_root=self.root / "mx")
        self.assertEqual(prop["status"], "PROPOSED_NOT_REGISTERED_OR_RUN", prop.get("reason"))
        proposed = operation._json(Path(prop["proposal"]["path"]))
        self.assertIn("--mamba-max-states-per-path 3", proposed["numerical_contract"])
        self.assertNotIn("flags, power cap and selected overlays unchanged", proposed["numerical_contract"])
        self.assertFalse(proposed["registered"] or proposed["gpu_allowance"])

    def test_mutated_snapshot_source_refused_before_build(self):
        mutated = self.root / "snapshots/mutated"
        shutil.copytree(self.snapshot, mutated)
        with (mutated / "sources" / operation.EVALUABLE).open("ab") as stream:
            stream.write(b"\n# drift\n")
        spy = Commands(self.root, {}, [])
        record = operation.prepare(mutated, self.bound, destination=self.root / "preparations/mutated",
                                   owner_inputs=OWNER_INPUTS, support_inputs=SUPPORT_INPUTS, commands_module=spy)
        self.assertEqual(record["status"], "REFUSED")
        self.assertIn("snapshot source bytes differ", record["reason"])
        self.assertEqual(spy.calls, [])
        self.assertFalse((self.root / "preparations/mutated").exists())

    def test_component_kind_is_not_this_seam(self):
        snapshot = self.root / "snapshots/component"
        (snapshot / "sources").mkdir(parents=True)
        source = CANDIDATE.read_bytes()
        (snapshot / "sources" / operation.EVALUABLE).write_bytes(source)
        inventory = [dict(name=operation.EVALUABLE, sha256=digest(source), bytes=len(source))]
        config_raw = exchange.canonical(self.config)
        created = int(time.time() * 1000)
        request = dict(schema=1, id="f" * 32, kind="component", campaign_sha256=self.bound["campaign_sha256"],
                       selection_sha256=self.bound["selection_sha256"], config_sha256=digest(config_raw),
                       source_set_sha256=digest(exchange.canonical(inventory)), created_unix_ms=created,
                       expires_unix_ms=created + 2910 * 1000, caller_seconds=2190, post_release_seconds=120,
                       sources=inventory, automatic_retry=False, adoption_authorized=False)
        (snapshot / "REQUEST.json").write_bytes(exchange.canonical(request))
        (snapshot / "CONFIG.json").write_bytes(config_raw)
        (snapshot / "CLAIM.json").write_bytes(exchange.canonical(dict(schema=1, id=request["id"],
            request_sha256=digest(exchange.canonical(request)), controller_sha256="c" * 64, claimed_unix_ms=created,
            gpu_admission=False)))
        spy = Commands(self.root, {}, [])
        record = operation.prepare(snapshot, self.bound, destination=self.root / "preparations/component",
                                   owner_inputs=OWNER_INPUTS, support_inputs=SUPPORT_INPUTS, commands_module=spy)
        self.assertEqual(record["status"], "UNSUPPORTED")
        self.assertEqual(spy.calls, [])

    def test_candidate_outside_declared_scope_refused_by_real_builder(self):
        candidate = self.root / "scope-candidate.py"
        candidate.write_bytes(CANDIDATE.read_bytes() + b"\n\ndef undeclared_surface():\n    return 1\n")
        snapshot, _ = self.take(self.config, "serving", sources={operation.EVALUABLE: str(candidate)})
        destination = self.root / "preparations/scope"
        record = operation.prepare(snapshot, self.bound, destination=destination,
                                   owner_inputs=OWNER_INPUTS, support_inputs=SUPPORT_INPUTS)
        self.assertEqual(record["status"], "REFUSED")
        self.assertEqual(record["stage"], "owner-build")
        self.assertIn("owner-build", record["reason"])
        self.assertTrue((destination / "build/owner-build.stderr").exists())
        self.assertIn(b"candidate changed module structure", (destination / "build/owner-build.stderr").read_bytes())
        self.assertFalse((destination / "author").exists())
        self.assertFalse((destination / "FINAL-ARTIFACTS.sha256").exists())

    def test_proposal_has_donor_shape_with_only_fresh_pins(self):
        checkpoint_root = self.checkpoints()
        prop = self.propose(checkpoint_root=checkpoint_root)
        proposal = json.loads(Path(prop["proposal"]["path"]).read_bytes())
        donor = json.loads(operation.DONOR_PROPOSAL.read_bytes())
        self.assertEqual(set(proposal), set(donor))
        self.assertEqual(prop["index"], 191)
        self.assertEqual(proposal["global_gpu_attempt_index"], 191)
        self.assertFalse(proposal["registered"] or proposal["gpu_allowance"])
        self.assertTrue(proposal["proposal"])
        self.assertEqual(proposal["status"], "PROPOSED_ONE_HYPERLOOM_SERVING")
        prep = self.preparation
        campaign = Path(prop["campaign_root"])
        self.assertEqual(proposal["execute_once_sha256"], digest((campaign / "execute_once.py").read_bytes()))
        self.assertEqual(proposal["execute_once_sha256"], prep["caller"]["sha256"])
        self.assertEqual(proposal["caller_plan_sha256"], digest(Path(prep["caller_plan"]["path"]).read_bytes()))
        self.assertEqual(proposal["source_seal"], prep["seals"]["author"])
        self.assertEqual(proposal["seals"], {prep["preparation_root"]: prep["seals"]["preparation"]["manifest_sha256"],
                                             prep["author_root"]: prep["seals"]["author"]["manifest_sha256"]})
        self.assertEqual(proposal["experiment_contract"], prep["source_binding"])
        for key in operation.PIN_FIELDS:
            self.assertEqual(proposal[key], prep["package"][key])
        self.assertEqual(proposal["prior_model_scope_owner_seconds_charged"], 64039.1)
        self.assertEqual(proposal["maximum_cumulative_model_scope_owner_seconds"], 70399.1)
        self.assertEqual((proposal["owner_seconds"], proposal["caller_total_ceiling_seconds"], proposal["worker_seconds"],
                          proposal["client_seconds"], proposal["model_starts_allowed"], proposal["generations"]),
                         (6360, 7160, 2280, 2040, 2, 60))
        self.assertIn("235 W", proposal["power_wake"])
        self.assertEqual(Path(prop["native_root"]).name, "hyperloom-serving-run-" + prop["nonce"])
        self.assert_untouched(prop, checkpoint_root)
        with self.subTest("busy checkpoint refuses a proposal"):
            busy = self.checkpoints(active_caller="/busy", active_owner="other")
            refused = operation.propose(prep["record"], self.bound, campaign_parent=self.root / "campaigns",
                                        checkpoint_root=busy, mx_root=self.root / "mx")
            self.assertEqual(refused["status"], "REFUSED")
            self.assertIn("busy", refused["reason"])

    def test_wrong_preparation_or_authorization_pin_refuses_without_registration(self):
        prop = self.propose()
        checkpoint_root = Path(prop["checkpoint"]["path"]).parent
        good = self.authorization(prop)
        cases = {
            "proposal pin": (dict(prop["record"], sha256="0" * 64), good),
            "authorization pin": (prop["record"], dict(good, sha256="0" * 64)),
            "other proposal": (prop["record"], self.authorization(prop, proposal_sha256="1" * 64)),
            "other request": (prop["record"], self.authorization(prop, request_sha256="2" * 64)),
            "other source set": (prop["record"], self.authorization(prop, source_set_sha256="3" * 64)),
            "other index": (prop["record"], self.authorization(prop, global_gpu_attempt_index=prop["index"] + 1)),
            "no owner answer": (prop["record"], self.authorization(prop, owner_answer="  ")),
            "approval drift": (prop["record"], self.authorization(prop, approval=dict(path=good["path"], sha256="4" * 64))),
            "retry claimed": (prop["record"], self.authorization(prop, no_retry=False)),
            "missing field": (prop["record"], self.authorization(prop, approval=None)),
        }
        for label, (proposal, authorization) in cases.items():
            with self.subTest(label):
                spy = Commands(prop["campaign_root"], {}, [])
                result = operation.execute(proposal, authorization, self.bound, checkpoint_root=checkpoint_root,
                                           cancelled=lambda: False, commands_module=spy, binder=None)
                self.assertEqual(result["status"], "REFUSED", result.get("reason"))
                self.assertFalse(result["owner_started"] or result["publishable"])
                self.assertEqual(spy.calls, [])
        self.assert_untouched(prop, checkpoint_root)

    def test_busy_changed_or_contested_checkpoint_refuses(self):
        prop = self.propose()
        variants = {
            "busy": self.checkpoints(active_caller="/other", active_owner="other", current_global_attempt_index=191),
            "changed accounting": self.checkpoints(cumulative_owner_seconds=64040.1),
            "changed index": self.checkpoints(last_completed_global_attempt_index=191),
            "unreleased previous": self.checkpoints(independent_release_verified=False),
        }
        for label, checkpoint_root in variants.items():
            with self.subTest(label):
                result, commands, binder, _ = self.run_once(prop, checkpoint_root=checkpoint_root)
                self.assertEqual(result["status"], "REFUSED")
                self.assertEqual(commands.calls, [])
                self.assertEqual([p.name for p in checkpoint_root.iterdir()], ["CHECKPOINT-527.json"])
        with self.subTest("competing registration between read and exclusive write"):
            checkpoint_root = self.checkpoints()

            def competitor():
                (checkpoint_root / "CHECKPOINT-528.json").write_text("{}\n")
                return False

            result, commands, binder, _ = self.run_once(prop, checkpoint_root=checkpoint_root, cancelled=competitor)
            self.assertEqual(result["status"], "REFUSED")
            self.assertIn("competing registration", result["reason"])
            self.assertEqual(commands.calls, [])
            self.assertFalse((Path(prop["campaign_root"]) / "REGISTRATION.json").exists())
        self.assertEqual(sorted(p.name for p in Path(prop["campaign_root"]).iterdir()),
                         ["OWNER-PROPOSAL.json", "REGISTRATION-PROPOSED.json", "execute_once.py"])

    def test_cancellation_never_starts_an_owner(self):
        with self.subTest("cancelled before registration"):
            prop = self.propose()
            result, commands, binder, _ = self.run_once(prop, cancelled=lambda: True)
            self.assertEqual(result["status"], "CANCELLED")
            self.assertFalse(result["owner_started"])
            self.assertEqual(commands.calls, [])
            self.assertFalse((Path(prop["campaign_root"]) / "REGISTRATION.json").exists())
            self.assertEqual([p.name for p in Path(prop["checkpoint"]["path"]).parent.iterdir()], ["CHECKPOINT-527.json"])
            self.assertTrue((Path(prop["campaign_root"]) / "OWNER-EXECUTION.json").exists())
        with self.subTest("cancelled immediately before invocation"):
            prop = self.propose()
            answers = iter([False, True])
            result, commands, binder, _ = self.run_once(prop, cancelled=lambda: next(answers))
            self.assertEqual(result["status"], "CANCELLED")
            self.assertFalse(result["owner_started"])
            self.assertTrue(result["registered"])
            self.assertEqual(commands.calls, [])
            self.assertEqual(binder.calls, [])
            campaign = Path(prop["campaign_root"])
            self.assertTrue((campaign / "REGISTRATION.json").exists())
            checkpoint_root = Path(prop["checkpoint"]["path"]).parent
            self.assertEqual(sorted(p.name for p in checkpoint_root.iterdir()),
                             ["CHECKPOINT-527.json", "CHECKPOINT-528.json", "CHECKPOINT-529.json"])
            closed = json.loads((checkpoint_root / "CHECKPOINT-529.json").read_bytes())
            self.assertEqual(closed["status"], "HYPERLOOM_SERVING_CANCELLED_BEFORE_OWNER_START")
            self.assertIsNone(closed["active_owner"])
            self.assertEqual(closed["last_completed_global_attempt_index"], 190)
            self.assertEqual(closed["unused_registration"]["sha256"], digest((campaign / "REGISTRATION.json").read_bytes()))
            self.assertTrue(closed["independent_release_verified"])
            again = operation.execute(prop["record"], self.authorization(prop), self.bound, checkpoint_root=checkpoint_root,
                                      cancelled=lambda: False, commands_module=Commands(campaign, {}, []))
            self.assertEqual(again["status"], "REFUSED")
            self.assertIn("single use", again["reason"])

    def test_successful_release_and_grading_projects_the_report_once(self):
        prop = self.propose()
        result, commands, binder, sequence = self.run_once(prop)
        self.assertEqual(result["status"], "SUCCEEDED", result.get("reason"))
        campaign = Path(prop["campaign_root"])
        registration_sha = digest((campaign / "REGISTRATION.json").read_bytes())
        caller = commands.calls[0]
        self.assertEqual(caller["name"], "caller-once")
        self.assertEqual(caller["argv"], [operation.PYTHON, "-B", str(campaign / "execute_once.py"),
                                          "--registration-sha256", registration_sha])
        self.assertEqual((caller["seconds"], caller["cwd"]), (7150, str(campaign)))
        self.assertEqual([c["name"] for c in commands.calls], ["caller-once", "cpu-grade-once", "cpu-readout-once"])
        self.assertEqual(commands.calls[1]["seconds"], 310)
        self.assertEqual(commands.calls[1]["argv"], [operation.PYTHON, "-B", "workstation.py", "--execute-once"])
        self.assertEqual(sequence, ["caller-once", "bind", "cpu-grade-once", "cpu-readout-once"])
        grant = json.loads((campaign / "REGISTRATION.json").read_bytes())
        proposal = json.loads((campaign / "REGISTRATION-PROPOSED.json").read_bytes())
        self.assertEqual(set(grant), set(proposal))
        self.assertEqual({k: grant[k] for k in proposal if k not in operation.VARIABLE},
                         {k: proposal[k] for k in proposal if k not in operation.VARIABLE})
        self.assertEqual((grant["registered"], grant["proposal"], grant["gpu_allowance"], grant["status"]),
                         (True, False, True, "REGISTERED_ONE_HYPERLOOM_SERVING"))
        self.assertEqual(grant["approval_record"], grant["registration_authority"])
        self.assertTrue(result["independent_release"])
        self.assertEqual(result["release_disposition"], "RELEASED")
        self.assertFalse(result["park_campaign"])
        self.assertEqual(result["owner_seal_sha256"], digest(b"fixture manifest\n"))
        self.assertEqual(binder.calls[0]["package_root"], str(Path(self.preparation["author_root"]) / "package-v2"))
        self.assertEqual(binder.calls[0]["projection_inventory_sha256"], self.preparation["quality_template"]["inventory_sha256"])
        report = result["payload"]["benchmark_report"]
        self.assertEqual(report["workstation_latency"]["observation_id"], prop["nonce"])
        self.assertEqual(report["paired_comparison"]["verdict"], "KEEP")
        self.assertFalse(report["adoption_authorized"])
        envelope = dict(status="SUCCEEDED", independent_release=True, payload=result["payload"],
                        owner_seal_sha256=result["owner_seal_sha256"], request_sha256=result["request_sha256"])
        self.assertEqual(backend.validate_report(envelope, self.request, self.bound), report)
        checkpoint_root = Path(prop["checkpoint"]["path"]).parent
        closed = json.loads((checkpoint_root / "CHECKPOINT-529.json").read_bytes())
        self.assertEqual(closed["status"], "HYPERLOOM_SERVING_COMPLETE_RELEASED_GRADED_PARKED")
        self.assertEqual(closed["last_completed_global_attempt_index"], 191)
        self.assertIsNone(closed["active_caller"])
        self.assertEqual(closed["cumulative_caller_seconds"], round(65169.2 + 930.5, 1))
        self.assertEqual(closed["cumulative_owner_seconds"], round(64039.1 + 900.0, 1))
        self.assertTrue(closed["independent_release_verified"])
        record = json.loads((campaign / "OWNER-EXECUTION.json").read_bytes())
        self.assertEqual(record["status"], "SUCCEEDED")
        self.assertEqual(json.loads((campaign / "caller-once.json").read_bytes())["work_seconds"], 7150)
        again, spy, _, _ = self.run_once(prop)
        self.assertEqual(again["status"], "REFUSED")
        self.assertEqual(spy.calls, [])

    def test_caller_failure_is_retained_without_release_or_grading(self):
        prop = self.propose()
        result, commands, binder, _ = self.run_once(prop, mode="incomplete")
        self.assertEqual(result["status"], "FAILED")
        self.assertTrue(result["owner_started"] and result["publishable"])
        self.assertFalse(result["independent_release"] or result["grading_performed"])
        self.assertEqual(result["release_disposition"], "UNVERIFIED_PARK_CAMPAIGN")
        self.assertTrue(result["park_campaign"])
        self.assertEqual([c["name"] for c in commands.calls], ["caller-once"])
        self.assertEqual(binder.calls, [])
        self.assertIn("finite caller failed", result["reason"])
        self.assertEqual(result["receipts"]["caller"]["exit_code"], 1)
        self.assertTrue(Path(result["receipts"]["execution_summary"]["path"]).exists())
        closed = json.loads((Path(prop["checkpoint"]["path"]).parent / "CHECKPOINT-529.json").read_bytes())
        self.assertEqual(closed["status"], "HYPERLOOM_SERVING_OWNER_UNRESOLVED_PARK_CAMPAIGN")
        self.assertEqual(closed["active_owner"], "hyperloom-serving")
        self.assertEqual(closed["current_global_attempt_index"], 191)
        self.assertFalse(closed["independent_release_verified"])
        self.assertFalse(closed["retry_authorized"])
        self.assertTrue((Path(prop["campaign_root"]) / "OWNER-EXECUTION.json").exists())

    def test_missing_release_parks_the_campaign_before_any_grading(self):
        with self.subTest("protected closeout without physical success"):
            prop = self.propose()
            result, commands, binder, _ = self.run_once(prop, mode="unreleased")
            self.assertEqual(result["status"], "FAILED")
            self.assertTrue(result["park_campaign"])
            self.assertFalse(result["independent_release"] or result["grading_performed"])
            self.assertEqual(binder.calls, [])
            self.assertEqual([c["name"] for c in commands.calls], ["caller-once"])
        with self.subTest("original replay refuses the retained operation"):
            prop = self.propose()
            result, commands, binder, _ = self.run_once(prop, bind_fail=True)
            self.assertEqual(result["status"], "FAILED")
            self.assertEqual(result["release_disposition"], "UNVERIFIED_PARK_CAMPAIGN")
            self.assertEqual(len(binder.calls), 1)
            self.assertEqual([c["name"] for c in commands.calls], ["caller-once"])
            closed = json.loads((Path(prop["checkpoint"]["path"]).parent / "CHECKPOINT-529.json").read_bytes())
            self.assertEqual(closed["status"], "HYPERLOOM_SERVING_OWNER_UNRESOLVED_PARK_CAMPAIGN")

    def test_authenticated_workload_failure_closes_but_never_grades_or_retries(self):
        prop = self.propose()
        observed = dict(operation_seal_sha256="a" * 64, workload_seal_sha256="b" * 64,
                        protected_seal_sha256="c" * 64, launch_elapsed_seconds=900.0,
                        workload_failed=True, grading_performed=False)
        with patch.object(operation, "_failed_release", return_value=observed) as authenticate:
            result, commands, binder, _ = self.run_once(prop, mode="incomplete")
        self.assertEqual(authenticate.call_count, 1)
        self.assertEqual(authenticate.call_args.args[0], Path(prop["native_root"]))
        self.assertEqual(authenticate.call_args.args[2], prop["index"])
        self.assertEqual(authenticate.call_args.args[4]["exit_code"], 1)
        self.assertEqual(result["status"], "FAILED")
        self.assertTrue(result["independent_release"] and result["workload_failed"] and result["park_campaign"])
        self.assertEqual(result["release_disposition"], "RELEASED_WORKLOAD_FAILED")
        self.assertEqual(result["owner_seal_sha256"], observed["operation_seal_sha256"])
        self.assertFalse(result["grading_performed"] or result["automatic_retry"])
        self.assertEqual(result["payload"], {})
        self.assertIn("finite caller failed", result["reason"])
        self.assertEqual(binder.calls, [])
        self.assertEqual([c["name"] for c in commands.calls], ["caller-once"])
        closed = json.loads((Path(prop["checkpoint"]["path"]).parent / "CHECKPOINT-529.json").read_bytes())
        self.assertEqual(closed["status"], "HYPERLOOM_SERVING_RELEASED_WORKLOAD_FAILED_PARKED")
        self.assertTrue(closed["independent_release_verified"])
        self.assertIsNone(closed["active_owner"])
        self.assertIsNone(closed["current_global_attempt_index"])
        self.assertEqual(closed["last_completed_global_attempt_index"], 191)
        self.assertEqual(closed["cumulative_owner_seconds"], round(64039.1 + 900.0, 1))
        self.assertEqual(closed["cumulative_caller_seconds"], round(65169.2 + 930.5, 1))

    def test_cpu_grade_and_readout_timeouts_are_retained_after_release(self):
        original=Commands.run
        for stage in ('cpu-grade-once','cpu-readout-once'):
            prop=self.propose()
            def run(commands,name,*args,**kwargs):
                if name==stage:
                    raise subprocess.TimeoutExpired(['fixture-'+stage],1)
                return original(commands,name,*args,**kwargs)
            with self.subTest(stage=stage),patch.object(Commands,'run',run):
                result,_,_,_=self.run_once(prop)
            self.assertEqual(result['status'],'FAILED')
            self.assertTrue(result['independent_release'])
            self.assertTrue(result['park_campaign'])
            self.assertIn('TimeoutExpired',result['reason'])
            self.assertTrue(Path(result['record']['path']).is_file())

    def test_wrong_owner_or_quality_binding_fails_after_observed_release(self):
        def corrupt_answer(decoded, quality):
            decoded["answers"][33]["response_raw_sha256"] = "0" * 64

        def failed_grade(decoded, quality):
            quality["grades"][0].update(status="QUALITY_REFUSAL", quality_passed=False)
            quality["quality_pass"] = False

        def readout_disagrees(decoded, quality):
            return dict(quality, decoded_rows=59)

        variants = {"wrong owner seal in the replay": dict(wrong_seal=True),
                    "native answer from another response": dict(mutate=corrupt_answer),
                    "quality failure after release": dict(mutate=failed_grade),
                    "independent readout disagrees": dict(mutate=readout_disagrees)}
        for label, options in variants.items():
            with self.subTest(label):
                prop = self.propose()
                result, commands, binder, _ = self.run_once(prop, **options)
                self.assertEqual(result["status"], "FAILED")
                self.assertNotIn("benchmark_report", result["payload"])
                if options.get("wrong_seal"):
                    self.assertFalse(result["independent_release"])
                    self.assertTrue(result["park_campaign"])
                    self.assertEqual([c["name"] for c in commands.calls], ["caller-once"])
                else:
                    self.assertTrue(result["independent_release"])
                    self.assertTrue(result["park_campaign"])
                    closed = json.loads((Path(prop["checkpoint"]["path"]).parent / "CHECKPOINT-529.json").read_bytes())
                    self.assertEqual(closed["status"], "HYPERLOOM_SERVING_RELEASED_QUALITY_FAILED_PARKED")
                    self.assertEqual(closed["last_completed_global_attempt_index"], 191)

    def test_real_derived_caller_admits_the_registration_this_seam_writes(self):
        """admit() only reads and verifies; the named run root under the real MX is never created."""
        import importlib.util
        prop = operation.propose(self.preparation["record"], self.bound,
                                 campaign_parent=Path(tempfile.mkdtemp(dir=self.root / "campaigns")).resolve(),
                                 checkpoint_root=self.checkpoints(), mx_root=operation.MX)
        self.assertEqual(prop["status"], "PROPOSED_NOT_REGISTERED_OR_RUN", prop.get("reason"))
        run_root = Path(prop["native_root"])
        self.assertEqual(run_root.parent, operation.MX)
        self.assertFalse(run_root.exists())
        answers = iter([False, True])
        result, commands, _, _ = self.run_once(prop, cancelled=lambda: next(answers))
        self.assertEqual((result["status"], result["owner_started"], commands.calls), ("CANCELLED", False, []))
        campaign = Path(prop["campaign_root"])
        spec = importlib.util.spec_from_file_location("_fixture_campaign_caller", campaign / "execute_once.py")
        caller = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(caller)
        grant, admitted = caller.admit(result["registration"]["sha256"])
        self.assertEqual(admitted, run_root)
        self.assertEqual(grant["status"], "REGISTERED_ONE_HYPERLOOM_SERVING")
        self.assertEqual(grant["global_gpu_attempt_index"], prop["index"])
        with self.assertRaises(AssertionError):
            caller.admit("0" * 64)
        self.assertFalse(run_root.exists())
        self.assertFalse((campaign / "CALLER-INVOKED.json").exists())

    def test_default_collaborators_are_the_pinned_helper_and_fresh_template_binder(self):
        prep = self.preparation
        root = Path(tempfile.mkdtemp(dir=self.root / "campaigns")).resolve()
        helper = Path(prep["preparation_root"]) / "launcher/caller-preview/local_commands.py"
        self.assertEqual(digest(helper.read_bytes()), operation.COMMANDS_SHA)
        module = operation.commands(helper, root)
        self.assertEqual(module.ROOT, root)
        receipt = module.run("local-echo", [operation.PYTHON, "-I", "-B", "-c", "print('inert')"], 30, cwd=str(root),
                             output_limit=1 << 20)
        self.assertEqual((receipt["exit_code"], receipt["work_seconds"], receipt["term_seconds"], receipt["reap_seconds"]),
                         (0, 30, 5, 5))
        self.assertEqual((root / "local-echo.stdout").read_bytes(), b"inert\n")
        self.assertTrue(receipt["child_reaped"] and receipt["owned_group_absent"])
        binder = operation._template_binder(prep["quality_template"]["root"], prep["quality_template"]["inventory_sha256"])
        self.assertTrue(callable(binder.bind) and callable(binder.require_replay_binding))
        self.assertEqual(binder.SOURCE_PINS["seal_evidence.py"], operation.SEALER_SHA)
        with self.assertRaises(ValueError):
            operation._template_binder(prep["quality_template"]["root"], "0" * 64)
        with self.assertRaises(ValueError):
            operation.commands(Path(prep["preparation_root"]) / "launcher/execute_once.py", root)

    def test_import_performs_no_child_network_or_framework_action(self):
        import subprocess
        probe = (
            "import sys\n"
            "def audit(event, args):\n"
            "    if event == 'import' and args[0].split('.')[0] in {'torch', 'triton', 'sglang', 'numpy'}:\n"
            "        raise RuntimeError('framework import')\n"
            "    if event in {'subprocess.Popen', 'os.system', 'os.fork', 'os.posix_spawn', 'socket.__new__'} or event.startswith('os.exec'):\n"
            "        raise RuntimeError('child or network action: ' + event)\n"
            "sys.addaudithook(audit)\n"
            "sys.path.insert(0, %r)\n"
            "import owner_operation\n"
            "print(owner_operation.CALLER_WORK_SECONDS, owner_operation.POST_RELEASE_SECONDS)\n" % str(HERE))
        completed = subprocess.run([operation.PYTHON, "-I", "-B", "-c", probe], capture_output=True, timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode()[-2000:])
        self.assertEqual(completed.stdout.strip(), b"7150 440")

    def test_profile_projects_all_seven_c2_windows_after_native_grades(self):
        checkpoint_root = self.checkpoints()
        prop = self.propose(self.profile, checkpoint_root)
        self.assertEqual(prop["kind"], "profile")
        proposal = json.loads(Path(prop["proposal"]["path"]).read_bytes())
        self.assertEqual((proposal["generations"], proposal["label"]), (10, "hyperloom-profile"))
        result, _, _, _ = self.run_once(prop)
        self.assertEqual(result["status"], "SUCCEEDED", result["reason"])
        report = result["payload"]["benchmark_report"]
        self.assertTrue(report["diagnostic_only"] and report["profile_coverage"]["complete"])
        self.assertEqual(report["quality_gate"]["decoded_rows"], 10)
        self.assertIsNone(report["workstation_latency"])
        self.assertIsNone(report["workstation_latency_control"])
        self.assertEqual(report["throughput"], {})
        import hyperloom_owner_profile
        output = Path(tempfile.mkdtemp(dir=self.root)).resolve()
        windows = hyperloom_owner_profile.materialize_captures(report, self.profile_request, output)
        self.assertEqual(len(windows), 7)
        self.assertEqual([w["batch"]["batch_size"] for w in windows], [2] * 7)

    def test_exhausted_admission_never_builds(self):
        spy = Commands(self.root, {}, [])
        result = operation.prepare(self.snapshot, self.bound, destination=self.root / "preparations/expired",
            owner_inputs=OWNER_INPUTS, support_inputs=SUPPORT_INPUTS, commands_module=spy,
            now=self.request["created_unix_ms"] / 1000 + 61)
        self.assertEqual(result["status"], "REFUSED")
        self.assertIn("budget", result["reason"])
        self.assertEqual(spy.calls, [])

    def test_replay_exhaustion_stops_before_grading(self):
        prop = self.propose()
        ticks = iter([0, 0, 1000])
        with patch.object(operation.time, "monotonic", side_effect=lambda: next(ticks, 1000)):
            result, commands, _, _ = self.run_once(prop)
        self.assertEqual(result["status"], "FAILED", result["reason"])
        self.assertTrue(result["park_campaign"])
        self.assertEqual([c["name"] for c in commands.calls], ["caller-once"])

    def test_changed_checkpoint_during_grade_parks_without_stale_closeout(self):
        prop = self.propose()
        path = Path(prop["checkpoint"]["path"]).parent / "CHECKPOINT-529.json"
        def concurrent_checkpoint(decoded, quality):
            path.write_text("{}\n")
        result, _, _, _ = self.run_once(prop, mutate=concurrent_checkpoint)
        self.assertEqual(result["status"], "FAILED")
        self.assertTrue(result["park_campaign"] and result["independent_release"])
        self.assertEqual(result["payload"], {})
        self.assertIn("closing checkpoint refused", result["reason"])
        self.assertEqual(path.read_bytes(), b"{}\n")

    def test_incomplete_or_cross_bound_profile_never_becomes_a_report(self):
        for mode in ("c1-decode", "missing-trace", "wrong-worker", "foreign-native-bundle", "foreign-pair"):
            with self.subTest(mode):
                prop = self.propose(self.profile)
                evidence = self.evidence(self.profile)
                item = evidence["bundles"]["kv4"]
                bundle = json.loads(item["raw"])
                if mode == "c1-decode":
                    name = "PROFILE-rank-0.json"
                    row = json.loads(base64.b64decode(bundle["observations"][name]["base64"]))
                    row["batch_coverage"]["decode"][0]["rids"].pop()
                    row["batch_coverage"]["decode"][0]["batch_size"] = 1
                    raw = exchange.canonical(row)
                    bundle["observations"][name] = dict(bytes=len(raw), sha256=digest(raw), base64=base64.b64encode(raw).decode())
                elif mode == "missing-trace":
                    del bundle["observations"]["PROFILE-rank-1-decode-03.json.gz"]
                elif mode == "wrong-worker":
                    bundle["workers"][1]["pid"] = 999
                elif mode == "foreign-pair":
                    # Keep the synthetic HTTP/native join internally consistent,
                    # while breaking its binding to the profiled request pair.
                    evidence["decoded"]["answers"][8]["request_body_sha256"] = "0" * 64
                    row = evidence["summaries"]["kv4"]["rows"][3]
                    row["body_sha256"] = row["contract"]["request_sha256"] = "0" * 64
                item["raw"] = exchange.canonical(bundle)
                item["reference"]["native_bundle_sha256"] = ("0" * 64 if mode == "foreign-native-bundle" else digest(item["raw"]))
                result, _, _, _ = self.run_once(prop, evidence=evidence)
                self.assertEqual(result["status"], "FAILED", result["reason"])
                self.assertTrue(result["independent_release"] and result["park_campaign"])
                self.assertEqual(result["payload"], {})

    def test_finite_replay_child_refuses_network_and_retains_failure(self):
        root = Path(tempfile.mkdtemp(dir=self.root)).resolve()
        template = root / "template"
        template.mkdir()
        raw = b"def bind(**kwargs):\n    import socket\n    socket.socket()\n"
        (template / "bind_future.py").write_bytes(raw)
        inventory = exchange.canonical(dict(files={"bind_future.py": dict(sha256=digest(raw))}))
        (template / "PROJECTION-INVENTORY.json").write_bytes(inventory)
        prep = dict(quality_template=dict(root=str(template), inventory_sha256=digest(inventory)))
        runner = operation.commands(Path(self.preparation["preparation_root"]) / "launcher/caller-preview/local_commands.py", root)
        with self.assertRaises(RuntimeError):
            operation._finite_replay(runner, root, prep, {}, time.monotonic() + 60)
        receipt = json.loads((root / "retained-replay-once.json").read_bytes())
        self.assertEqual(receipt["work_seconds"], 40)
        self.assertTrue(receipt["child_reaped"] and receipt["owned_group_absent"])
        self.assertIn(b"network action refused", (root / "retained-replay-once.stderr").read_bytes())


class FailedReleaseEvidenceTests(unittest.TestCase):
    """Read operation 200's sealed bytes; mutations stay in memory only."""

    @classmethod
    def setUpClass(cls):
        cls.root = operation.MX / "hyperloom-serving-run-febc0ff03bf85323"
        cls.seal = "0a975b3da490c15a67f5c333605484aa968bb1dc6da176b9550ba97025bc54c4"
        operation.sealer().verify(cls.root, cls.seal)
        job = I / "campaign-v5/controller/jobs/3a591eb2b13b85b67a1e9d9e57dc625a"
        prep = operation._json(job / "PREPARATION-RESULT.json")
        cls.verified = operation._verified_preparation(prep["record"], prep["binding_identities"])
        cls.campaign = job / "hyperloom-serving-campaign-febc0ff03bf85323"
        cls.registration = operation._pin(cls.campaign / "REGISTRATION.json")
        cls.receipt = operation._json(cls.campaign / "caller-once.json")
        cls.stdout = (cls.campaign / "caller-once.stdout").read_bytes()
        cls.bindings = operation._json(Path(prep["launcher_bindings"]["path"]))
        cls.workload = cls.root / "collected/workload" / Path(cls.bindings["target_root"]).name
        cls.protected = cls.root / "collected/protected" / Path(cls.bindings["protected_root"]).name
        cls.manifest = operation._json(cls.workload / "MANIFEST.json")
        template = Path(prep["quality_template"]["root"])
        inventory = operation._json(template / "PROJECTION-INVENTORY.json")
        cls.reader = operation._source(template / "retained_replay.py",
            inventory["files"]["retained_replay.py"]["sha256"], "_test_failed_release_reader")

    @classmethod
    def tearDownClass(cls):
        operation.sealer().verify(cls.root, cls.seal)

    def authenticate(self, **changes):
        args = dict(run_root=self.root, verified=self.verified, index=200, registration=self.registration,
                    caller_receipt=self.receipt, stdout=self.stdout)
        args.update(changes)
        return operation._failed_release(**args)

    def test_sealed_operation200_release_is_replayed_without_quality(self):
        result = self.authenticate()
        self.assertEqual(result["operation_seal_sha256"], self.seal)
        self.assertEqual([p["phase"] for p in result["phases"]], ["locked-admission", "release-bf16", "independent"])
        self.assertEqual([p["processes_checked"] for p in result["phases"][1:]], [114, 114])
        self.assertTrue(result["workload_failed"])
        self.assertFalse(result["grading_performed"])

    def test_seal_index_and_natural_caller_exit_are_required(self):
        for changes in (
            dict(stdout=exchange.canonical(dict(status="INCOMPLETE_RETAINED", seal="0" * 64))),
            dict(index=201), dict(registration=dict(self.registration, sha256="0" * 64)),
            dict(caller_receipt=dict(self.receipt, child_reaped=False)),
            dict(caller_receipt=dict(self.receipt, owned_group_absent=False)),
            dict(caller_receipt=dict(self.receipt, signals=[15])),
            dict(caller_receipt=dict(self.receipt, exit_code=True)),
        ):
            with self.subTest(changes=list(changes)), self.assertRaises((ValueError, AssertionError)):
                self.authenticate(**changes)

    def test_missing_root_closeout_observer_refuses(self):
        original_read, original_source = self.reader.read, operation._source
        def read(path):
            if Path(path).name == "observe-root-failed-closeout.json":
                raise FileNotFoundError("missing retained failed-closeout observer")
            return original_read(path)
        def source(path, pin, name, *args, **kwargs):
            return self.reader if Path(path).name == "retained_replay.py" else original_source(path, pin, name, *args, **kwargs)
        with patch.object(self.reader, "read", side_effect=read), patch.object(operation, "_source", side_effect=source):
            with self.assertRaisesRegex(FileNotFoundError, "missing retained"):
                self.authenticate()

    def test_failed_release_observations_refuse_in_memory_corruption(self):
        mutations = {
            "foreign session": ("independent.json", lambda v: v.update(session_sha256="0" * 64)),
            "foreign root": ("independent.json", lambda v: v.update(root="/home/developer/foreign")),
            "foreign collector": ("independent.json", lambda v: v.update(collector_source_sha256="0" * 64)),
            "foreign metadata": ("independent.json", lambda v: v.update(metadata_source_sha256="0" * 64)),
            "remaining holder": ("independent.json", lambda v: v["holders"].update(exit_code=0, stdout="123")),
            "remaining allocation": ("independent.json", lambda v: v["allocation"].update(gpu_requests=1)),
            "missing process": ("independent.json", lambda v: v["checked_processes"].pop()),
            "present cgroup": ("independent-cgroup.json", lambda v: v["scopes"][0].update(absent=False)),
            "observer unreaped": ("independent-owner.json", lambda v: v["supervision"].update(child_reaped=False)),
            "observer survivors": ("independent-owner.json", lambda v: v["supervision"].update(survivors=[v["supervision"]["root_process"]])),
            "missing independent": ("CONTROLLER-RESULT.json", lambda v: v["observer_requests"].pop()),
            "unresolved journal": ("CONTROLLER-RESULT.json", lambda v: v["campaign"].update(unresolved=True)),
            "unreaped workload": ("CONTROLLER-RESULT.json", lambda v: v["campaign"]["arms"][0]["supervision"].update(child_reaped=False)),
        }
        original = self.reader.read
        for name, (filename, mutate) in mutations.items():
            def read(path):
                value = original(path)
                if Path(path).name == filename:
                    mutate(value)
                # Keep duplicate campaign observations consistent, so the actual
                # absence predicate, not merely the equality join, is exercised.
                if Path(path).name == "CONTROLLER-RESULT.json" and filename in ("independent.json", "independent-cgroup.json"):
                    value = copy.deepcopy(value)
                    target = value["campaign"]["independent_postcheck"]
                    mutate(target if filename == "independent.json" else target["root_resource_release"])
                return value
            with self.subTest(name=name), patch.object(self.reader, "read", side_effect=read):
                with self.assertRaises((ValueError, AssertionError, KeyError, RuntimeError)):
                    operation._failed_campaign_release(self.reader, self.workload, self.protected, self.bindings, self.manifest)


if __name__ == "__main__":
    unittest.main(verbosity=2)
