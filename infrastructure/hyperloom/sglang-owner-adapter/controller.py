"""Trusted external controller for finite Hyperloom owner requests.

The optimizer can submit source bytes and fixed-stack configurations only.
This controller retains them privately, constructs the existing owner packet,
derives one registration from an explicit campaign grant, then returns only an
authenticated result. It does not install or start the optimizer, create a
qualification proof, or authorize adoption. No action occurs on import.
"""

import argparse
import json
from pathlib import Path
import sys
import time

HERE = Path(__file__).resolve().parent
INFRA = HERE.parents[2]
sys.path[:0] = [str(HERE), str(INFRA / "lib/workstation")]
import hyperloom_exchange as exchange
import hyperloom_exchange_host as host
from hyperloom_sglang import checked_bytes, checked_json, require
import exchange_ssh
import owner_operation
import component_operation


CAMPAIGN_SECONDS = 14400
ADAPTER = "infrastructure/hyperloom/sglang-owner-adapter/"
REQUIRED_SOURCES = frozenset(
    [ADAPTER + name + ".py" for name in ("controller", "owner_operation", "exchange_ssh", "build_owner",
                                        "build_support", "build_profile", "profile_c2", "component_operation",
                                        "compile_component", "component_dispatch", "component_scope", "build_component",
                                        "verify_serving")]
    + ["lib/workstation/" + name + ".py" for name in (
        "hyperloom_sglang", "hyperloom_exchange", "hyperloom_exchange_host", "hyperloom_owner_backend",
        "hyperloom_owner_result", "hyperloom_latency", "hyperloom_workload")])
IDENTITIES = ("campaign_sha256", "selection_sha256", "controller_sha256", "runtime_sha256",
              "workload_sha256", "selected_source_set_sha256")


class CandidateScreened(Exception):
    """A retained CPU rejection; no GPU owner was admitted."""


def pin(path):
    return dict(path=str(path), sha256=exchange.digest(exchange._read(path, 16 << 20)))


def write(path, value):
    exchange._publish(path, exchange.canonical(value))
    return pin(path)


def read(reference):
    require(isinstance(reference, dict) and set(reference) == {"path", "sha256"}, "explicit pinned input required")
    path = Path(reference["path"])
    require(path.is_absolute() and path.resolve() == path, "canonical pinned input required")
    return checked_json(path, reference["sha256"], 4 << 20)


def source_closure(pins):
    require(isinstance(pins, dict) and set(pins) == REQUIRED_SOURCES, "closed controller and builder sources required")
    for name, digest in pins.items():
        checked_bytes(INFRA / name, digest, 1 << 20)
    return exchange.digest(exchange.canonical(pins))


class Controller:
    def __init__(self, config, *, rpc=exchange_ssh.call, operation=owner_operation, component=component_operation):
        require(isinstance(config, dict) and config.get("schema") == 1, "controller configuration schema")
        self.config, self.rpc, self.operation = config, rpc, operation
        self.component = component
        self.bound = read(config["binding"])
        owner_operation._bound(self.bound)
        require(config["rpc"]["native_binding"]["sha256"] == config["binding"]["sha256"],
                "local and native exchange bindings differ")
        require(source_closure(config["source_pins"]) == self.bound["controller_sha256"],
                "controller identity differs from the current source closure")
        self.grant = read(config["campaign_grant"])
        grant = self.grant
        require(grant.get("schema") == 1 and grant.get("kind") == "hyperloom-sglang-campaign"
                and grant.get("execution_authorized") is True and grant.get("campaign_seconds") == CAMPAIGN_SECONDS
                and grant.get("objective") == "balanced-c1-c2-no-regressions"
                and grant.get("automatic_retry") is False and grant.get("adoption_authorized") is False
                and grant.get("existing_observer_only") is True
                and grant.get("watts_per_card") == 235, "explicit bounded campaign grant required")
        require(all(grant.get(key) == self.bound[key] for key in IDENTITIES), "campaign grant belongs to another stack")
        require(type(grant.get("started_unix_ms")) is int and type(grant.get("deadline_unix_ms")) is int
                and 0 < grant["deadline_unix_ms"] - grant["started_unix_ms"] <= CAMPAIGN_SECONDS * 1000
                and grant["deadline_unix_ms"] == self.bound["campaign_deadline_unix_ms"], "four-hour campaign deadline required")
        require(isinstance(grant.get("owner"), str) and grant["owner"].strip()
                and isinstance(grant.get("user_instruction"), str) and grant["user_instruction"].strip(),
                "retained user instruction required")
        require(grant.get("allowed_kinds") in (["serving", "profile"], ["serving", "profile", "component"]),
                "explicit serving/profile/component owner kinds required")
        self.root = owner_operation._canonical_dir(config["root"], "controller")
        for name in ("rpc", "jobs"):
            (self.root / name).mkdir(mode=0o700)
        self.sequence = 0
        self.parked = False
        self.terminal_attempted = False
        self.components = {}
        self.deadline = time.monotonic() + max(0, grant["deadline_unix_ms"] / 1000 - time.time())
        self.started = write(self.root / "CONTROLLER-STARTED.json", dict(schema=1,
            config_sha256=exchange.digest(exchange.canonical(config)), campaign_grant=config["campaign_grant"],
            controller_sha256=self.bound["controller_sha256"], deadline_unix_ms=grant["deadline_unix_ms"],
            qualification=False, adoption_authorized=False))

    def remaining(self):
        return max(0, min(self.deadline - time.monotonic(),
                          self.grant["deadline_unix_ms"] / 1000 - time.time()))

    def _rpc(self, action, *, job_id=None, result=None, expires=None):
        require(self.remaining() > 10, "campaign has no remaining transport budget")
        self.sequence += 1
        destination = self.root / "rpc" / ("%04d-%s" % (self.sequence, action))
        deadline = min(self.grant["deadline_unix_ms"], expires or self.grant["deadline_unix_ms"],
                       int((time.time() + self.remaining()) * 1000))
        return self.rpc(self.config["rpc"], action, destination, job_id=job_id, result=result, deadline_unix_ms=deadline)

    def _cancelled(self, request):
        response = self._rpc("cancelled", job_id=request["id"], expires=request["expires_unix_ms"])
        require(type(response.get("cancelled")) is bool, "explicit cancellation observation required")
        if not response["cancelled"]:
            require(response.get("request_sha256") == exchange.digest(exchange.canonical(request)),
                    "request changed after the private snapshot")
        return response["cancelled"]

    def _authorization(self, proposal, request, root):
        # This is a derivation from the retained campaign instruction, not a
        # fabricated new user answer and not a reuse of an older one-shot grant.
        grant = self.grant
        require(request["kind"] in grant["allowed_kinds"], "campaign does not cover this owner kind")
        candidate = request["sources"]
        require(len(candidate) == 1 and candidate[0]["name"] == owner_operation.EVALUABLE,
                "undeclared optimization surface")
        value = dict(schema=1, kind=request["kind"], campaign=proposal["campaign_root"],
            proposal_sha256=proposal["proposal"]["sha256"], execute_once_sha256=proposal["execute_once"]["sha256"],
            nonce=proposal["nonce"], global_gpu_attempt_index=proposal["index"],
            request_sha256=exchange.digest(exchange.canonical(request)), source_set_sha256=request["source_set_sha256"],
            candidate_sha256=candidate[0]["sha256"], campaign_sha256=self.bound["campaign_sha256"],
            controller_sha256=self.bound["controller_sha256"], owner=grant["owner"],
            owner_answer="Derived from the retained campaign instruction: " + grant["user_instruction"],
            approval=dict(self.config["campaign_grant"]), no_retry=True, one_invocation=True,
            deployment=False, promotion=False, publication=False, adoption_authorized=False, qualification=False,
            utc=owner_operation._utc(time.time()))
        return write(root / "DERIVED-OPERATION-AUTHORITY.json", value)

    def step(self):
        """Handle at most one queued owner request; never retry a consumed one."""
        require(not self.parked, "campaign is parked")
        try:
            return self._step()
        except Exception as error:
            # A take can fail before a complete, authenticated request exists.
            # Do not invent a request-bound result. Close the entire exchange
            # explicitly so both claimed and unclaimed waiters stop promptly.
            result = dict(schema=1, status="FAILED", owner_started=None, park_campaign=True,
                          reason=owner_operation._error(error), automatic_retry=False,
                          adoption_authorized=False, independent_release=False)
            write(self.root / "DISPATCH-FAILURE.json", result)
            self._park(result["reason"])
            return result

    def _step(self):
        source_closure(self.config["source_pins"])
        if self.remaining() <= 30:
            self.parked = True
            return dict(schema=1, status="BUDGET_EXHAUSTED", owner_started=False, park_campaign=True)
        listing = self._rpc("list")
        jobs = listing.get("jobs")
        require(isinstance(jobs, list) and len(jobs) <= host.MAX_QUEUED
                and all(isinstance(job, str) and exchange.JOB_ID.fullmatch(job) for job in jobs)
                and len(set(jobs)) == len(jobs), "bounded distinct exchange jobs required")
        if not jobs:
            return dict(schema=1, status="IDLE", remaining_seconds=self.remaining())
        job_id = jobs[0]
        snapshot = self._rpc("take", job_id=job_id)
        root = self.root / "jobs" / job_id
        root.mkdir(mode=0o700)
        private = host.retain_snapshot(snapshot, root / "snapshot", self.bound)
        request = snapshot["request"]
        outcome = dict(schema=1, id=job_id, campaign_sha256=self.bound["campaign_sha256"],
            controller_sha256=self.bound["controller_sha256"], request_sha256=exchange.digest(exchange.canonical(request)),
            config_sha256=request["config_sha256"], source_set_sha256=request["source_set_sha256"],
            status="FAILED", independent_release=False, owner_seal_sha256=None, payload={},
            owner_started=False, adoption_authorized=False, automatic_retry=False, qualification=False,
            park_campaign=True, reason=None)
        try:
            need = request["caller_seconds"] + request["post_release_seconds"]
            require(self.remaining() >= need and time.time() + need <= request["expires_unix_ms"] / 1000,
                    "whole owner and closeout no longer fit the campaign")
            if self._cancelled(request):
                outcome.update(status="CANCELLED", reason="cancelled before packet preparation", park_campaign=False)
            elif request["kind"] not in self.grant["allowed_kinds"]:
                outcome.update(status="UNSUPPORTED", reason="owner kind outside the campaign grant")
            else:
                source_closure(self.config["source_pins"])
                if request["kind"] != "component" and request["source_set_sha256"] != self.bound["selected_source_set_sha256"]:
                    require(self.components.get(request["source_set_sha256"], {}).get("eligible_for_serving") is True,
                            "changed source needs a successful component screen in this campaign")
                inputs = self.config["inputs"][request["kind"]]
                operation = self.component if request["kind"] == "component" else self.operation
                preparation = operation.prepare(private, self.bound, destination=root / "preparation",
                    owner_inputs=inputs["owner"], support_inputs=inputs["support"])
                write(root / "PREPARATION-RESULT.json", preparation)
                if preparation["status"] == "CPU_CANDIDATE_REJECTED":
                    outcome.update(status="FAILED", park_campaign=False, reason=preparation["reason"],
                                   payload={"component_compile": preparation})
                    raise CandidateScreened()
                require(preparation["status"] == "PREPARED_NOT_REGISTERED_OR_RUN",
                        "owner preparation refused: " + str(preparation.get("reason")))
                proposal = operation.propose(preparation["record"], self.bound, campaign_parent=root,
                    checkpoint_root=self.config["checkpoint_root"], mx_root=self.config["run_parent"])
                write(root / "PROPOSAL-RESULT.json", proposal)
                require(proposal["status"] == "PROPOSED_NOT_REGISTERED_OR_RUN",
                        "owner proposal refused: " + str(proposal.get("reason")))
                authorization = self._authorization(proposal, request, root)
                source_closure(self.config["source_pins"])
                outcome.update(owner_started=None, release_disposition="UNVERIFIED_PARK_CAMPAIGN")
                observed = operation.execute(proposal["record"], authorization, self.bound,
                    checkpoint_root=self.config["checkpoint_root"], cancelled=lambda: self._cancelled(request))
                outcome.update(observed)
                require(all(outcome[k] == request[k] for k in ("id", "campaign_sha256", "config_sha256", "source_set_sha256"))
                        and outcome["request_sha256"] == exchange.digest(exchange.canonical(request))
                        and outcome["controller_sha256"] == self.bound["controller_sha256"], "operation returned another request")
                if outcome["status"] == "REFUSED":
                    outcome.update(status="FAILED", park_campaign=True)
                if request["kind"] == "component" and outcome["status"] == "SUCCEEDED":
                    self.components[request["source_set_sha256"]] = outcome["payload"]["component_report"]
        except CandidateScreened:
            pass
        except Exception as error:
            outcome.update(status="FAILED", park_campaign=True, reason=owner_operation._error(error))
        return self._publish(root, request, outcome)

    def _publish(self, root, request, outcome):
        job_id = request["id"]
        retained = write(root / "OUTCOME.json", outcome)
        self.parked = bool(outcome["park_campaign"])
        try:
            published = self._rpc("publish", job_id=job_id, result=outcome, expires=request["expires_unix_ms"])
            require(published.get("published_sha256") == retained["sha256"], "published result identity mismatch")
            write(root / "PUBLICATION.json", published)
        except Exception as error:
            self.parked = True
            write(root / "PUBLICATION-FAILURE.json", dict(reason=owner_operation._error(error), retained=retained,
                automatic_retry=False, park_campaign=True))
            raise
        if self.parked:
            self._park(outcome.get("reason") or "owner operation requires campaign closeout")
        return outcome

    def _park(self, reason):
        self.parked = True
        if self.terminal_attempted:
            return
        self.terminal_attempted = True
        try:
            published = self._rpc("park", result={"reason":str(reason)[:2048]})
            write(self.root / "TERMINAL-PUBLICATION.json", published)
        except Exception as error:
            # Transport loss is retained locally. The campaign supervisor must
            # stop its client when this controller stops; it must not restart us.
            write(self.root / "TERMINAL-PUBLICATION-FAILURE.json",
                  dict(reason=owner_operation._error(error), automatic_retry=False))

    def serve(self):
        """Finite polling; every expensive action has its own tighter owner bound."""
        try:
            while not self.parked and self.remaining() > 30:
                if (self.root / "STOP.json").exists():
                    self._park("campaign supervisor requested closeout")
                    break
                result = self.step()
                if result["status"] == "IDLE":
                    time.sleep(min(2, self.remaining()))
        finally:
            self._park("campaign controller stopped")
            write(self.root / "CONTROLLER-STOPPED.json", dict(schema=1, remaining_seconds=self.remaining(),
                utc=owner_operation._utc(time.time()), adoption_authorized=False, automatic_retry=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--config-sha256", required=True)
    args = parser.parse_args()
    controller = Controller(read(dict(path=str(args.config), sha256=args.config_sha256)))
    result = controller.serve()
    print(json.dumps(result, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
