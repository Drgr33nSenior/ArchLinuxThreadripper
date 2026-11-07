"""Resumable chaining of the existing safe after-first-boot workstation stages.

`plan` prints the ordered steps and their status and executes nothing. `run`
executes only ungated steps whose prerequisites and privilege match, through the
existing workstationctl/bootstrap-arch commands. It records each attempt in the
private state directory and stops at the first failure, missing input, privilege
boundary or owner gate. It never performs an owner-gated action: it prints the
next owner command instead. Records hold argv, digests and exit status only.
"""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = 1
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
STOP_FAILED, STOP_USAGE, STOP_GATE, STOP_INPUT, STOP_PRIVILEGE = 1, 2, 3, 4, 5
INPUTS = {
    "install_config": ("--install-config", "reviewed nonsecret install.conf restored from external storage"),
    "workstation_config": ("--workstation-config", "reviewed workstation.conf (workstationctl --config)"),
    "service_site": ("--service-site", "completed private service site JSON outside Git"),
    "existing_claims": ("--existing-claims", "optional kubectl get pvc -o json metadata collected by the owner"),
    "gpu_chart": ("--gpu-chart", "downloaded AMD GPU Operator chart matching infrastructure/gpu-operator/chart.lock"),
    "rocm_python": ("--rocm-python", "optional coherent ROCm PyTorch Python executable"),
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def utc():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def file_sha256(path):
    result = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1048576), b""):
            result.update(chunk)
    return result.hexdigest()


def tree_sha256(path):
    """Digest names, types and content; symlinks are recorded, never followed."""
    path = Path(path)
    if path.is_symlink() or not path.exists():
        return None
    if path.is_file():
        return file_sha256(path)
    result = hashlib.sha256()
    for directory, names, files in os.walk(path, followlinks=False):
        names.sort()
        for name in sorted(names + files):
            item = Path(directory) / name
            relative = item.relative_to(path).as_posix()
            if item.is_symlink():
                result.update(f"L {relative} {os.readlink(item)}\n".encode())
            elif item.is_dir():
                result.update(f"D {relative}\n".encode())
            elif item.is_file():
                result.update(f"F {relative} {file_sha256(item)}\n".encode())
    return result.hexdigest()


def digest_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def quote(argv):
    return " ".join(shlex.quote(str(part)) for part in argv)


def default_runner(argv, cwd, stdout, stderr):
    try:
        return subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                              check=False).returncode
    except OSError as error:
        stderr.write(f"postinstall: cannot start {argv[0]}: {error.strerror}\n".encode())
        return 127


class Step:
    def __init__(self, ident, title, *, run_as="user", boot_bound=False, needs=(), uses=(), tools=(),
                 output=None, build=None, owner=None, reference="", note=""):
        self.ident, self.title, self.run_as, self.boot_bound = ident, title, run_as, boot_bound
        self.needs, self.uses, self.tools, self.output = tuple(needs), tuple(uses), tuple(tools), output
        self.build, self.owner, self.reference, self.note = build, owner, reference, note

    @property
    def gate(self):
        return self.owner is not None


def ws(o, *arguments):
    config = ["--config", o.inputs["workstation_config"]] if o.inputs.get("workstation_config") else []
    return [str(o.root / "bin/workstationctl"), *config, *map(str, arguments)]


def k3s_package_file(o):
    try:
        lock = dict(line.split("=", 1) for line in (o.root / "versions.lock").read_text().splitlines()
                    if "=" in line and not line.startswith("#"))
        version = lock["K3S_VERSION"].removeprefix("v").replace("+", ".")
    except (OSError, KeyError):
        version = "<K3S_VERSION>"
    return o.root / f"infrastructure/packages/k3s/k3s-workstation-bin-{version}-1-x86_64.pkg.tar.zst"


def ansible(o, *extra):
    hardware = o.output_of("hardware-collect")
    report = f"{hardware}/hardware.json" if hardware else "<hardware-collect output>/hardware.json"
    ansible_root = o.root / "infrastructure/ansible"
    return quote(["ansible-playbook", "-i", ansible_root / "inventory.local.ini", "--limit"]) + \
        " CHOSEN_LAB_ALIAS " + quote(["--ask-become-pass", "-e", f"@{ansible_root}/group_vars/site.local.yml",
                                      "-e", f"lab_hardware_report={report}", *extra, ansible_root / "site.yml"])


def steps():
    """The documented stage order. Gates name owner actions; nothing here performs them."""
    return [
        Step("secure-boot-unlock", "Secure Boot owner keys, hardware-token unlock and cold-boot tests",
             owner=lambda o: [], reference="docs/INSTALLATION.md sections 5 and 6 (6A for tpm2-pin)"),
        Step("bootstrap-verify", "Installed boot, storage and signature verification", run_as="root",
             boot_bound=True, needs=("install_config",), tools=("bin/bootstrap-arch",),
             build=lambda o, out: [str(o.root / "bin/bootstrap-arch"), "--config", o.inputs["install_config"],
                                   "verify"]),
        Step("status", "Read-only workstation status", run_as="any", boot_bound=True,
             build=lambda o, out: ws(o, "status")),
        Step("swap-validate", "No-swap policy validation", run_as="any", boot_bound=True,
             build=lambda o, out: ws(o, "swap", "validate")),
        Step("packages", "Server package profile with the AMD graphics/ROCm baseline (pacman -Syu)",
             owner=lambda o: ["sudo " + quote(ws(o, "dev", "setup", "server",
                                                 o.root / "templates/workstation/packages.pacman"))],
             reference="docs/WORKSTATION.md#packages-and-services",
             note="Privileged pacman -Syu transaction. Reboot if the kernel or firmware changed, then rerun."),
        Step("hardware-collect", "Current-boot hardware evidence", run_as="root", boot_bound=True, output="dir",
             build=lambda o, out: ws(o, "hardware", "collect", out)),
        Step("resources-plan", "Offline CPU/RAM admission plan", run_as="root", uses=("hardware-collect",),
             output="dir", build=lambda o, out: ws(o, "resources", "plan",
                                                   f"{o.output_of('hardware-collect')}/hardware.json", out)),
        Step("rocm-validate", "Per-GPU HIP, PyTorch and RCCL validation", boot_bound=True, output="dir",
             build=lambda o, out: ws(o, "rocm", "validate", out,
                                     *([o.inputs["rocm_python"]] if o.inputs.get("rocm_python") else []))),
        Step("k3s-package", "Review, build, sign and install the pinned k3s-workstation-bin package",
             owner=lambda o: [f"cd {shlex.quote(str(o.root / 'infrastructure/packages/k3s'))} && makepkg --sign",
                              "sudo " + quote(["pacman", "-U", k3s_package_file(o)])],
             reference="docs/HOME-LAB.md#3-package-and-configure-bare-metal-k3s"),
        Step("k3s-host", "Apply the reviewed host/K3s configuration with service management off",
             owner=lambda o: [ansible(o, "-e", "lab_manage_service=false")],
             reference="docs/INSTALLATION.md#after-first-boot-stage-order",
             note="With ENABLE_SSH=false, inventory.local.ini holds '[ai_lab_baremetal]' and "
                  "'CHOSEN_LAB_ALIAS ansible_connection=local'. The report must be from this boot."),
        Step("k3s-start", "Review sysctls, modules and firewall, then start K3s",
             owner=lambda o: [ansible(o, "-e", "lab_manage_service=true", "-e", "lab_network_reviewed=true")],
             reference="docs/HOME-LAB.md#3-package-and-configure-bare-metal-k3s",
             note="Review the generated sysctls, modules, CNI and firewall policy first; this enables and starts k3s."),
        Step("gpu-operator-render", "Offline AMD GPU Operator render (no hooks)", needs=("gpu_chart",),
             tools=("infrastructure/gpu-operator/render.sh",), output="stdout",
             build=lambda o, out: ["bash", str(o.root / "infrastructure/gpu-operator/render.sh"),
                                   o.inputs["gpu_chart"]]),
        Step("gpu-operator-apply", "Approve operator RBAC, apply the render, then the DeviceConfig",
             owner=lambda o: [quote(["kubectl", "--context"]) + " <reviewed-context> " +
                              quote(["apply", "-f",
                                     f"{o.output_of('gpu-operator-render') or '<render>'}/rendered.yaml"]),
                              "kubectl --context <reviewed-context> apply -f " +
                              shlex.quote(str(o.root / "infrastructure/gpu-operator/deviceconfig.yaml"))],
             reference="docs/HOME-LAB.md#4-gpu-operator-and-allocation",
             note="Review the manager's cluster-wide RBAC first; apply the DeviceConfig only after its CRD exists."),
        Step("gpu-qualification", "Dual-GPU HIP/PyTorch/RCCL/reboot qualification and allocatable GPUs",
             owner=lambda o: [], reference="docs/HOME-LAB.md#6-validation-and-promotion and docs/ROCM.md"),
        Step("telemetry-render", "Offline service telemetry render", output="dir",
             build=lambda o, out: ws(o, "telemetry", "render", out, "apps/overlays/service")),
        Step("telemetry-verify", "Verify the telemetry render", uses=("telemetry-render",),
             build=lambda o, out: ws(o, "telemetry", "verify", o.output_of("telemetry-render"))),
        Step("service-render", "Offline assistant service render (replicas stay zero)", needs=("service_site",),
             output="dir", build=lambda o, out: ws(
                 o, "service", "render", o.inputs["service_site"], out, "--mode", "large", "--experience",
                 "assistant-memory" if o.with_memory else "assistant", *(["--existing-claims", o.inputs["existing_claims"]]
                                if o.inputs.get("existing_claims") else []))),
        Step("rag-stage-models", "Stage the pinned RAG embedding model", output="dir",
             build=lambda o, out: ws(o, "rag", "stage-models", out)),
        Step("rag-verify-models", "Verify the staged RAG model", uses=("rag-stage-models",),
             build=lambda o, out: ws(o, "rag", "verify-models", o.output_of("rag-stage-models"))),
        Step("telemetry-plan", "Telemetry admission plan", run_as="root",
             uses=("resources-plan", "telemetry-render"), output="dir",
             build=lambda o, out: ws(o, "telemetry", "plan", f"{o.output_of('resources-plan')}/resource-plan.json",
                                     o.output_of("telemetry-render"), out)),
        Step("service-plan", "Service capacity admission plan (a blocked budget stops here)", run_as="root",
             uses=("resources-plan", "service-render", "telemetry-render"), output="dir",
             build=lambda o, out: ws(o, "service", "plan", f"{o.output_of('resources-plan')}/resource-plan.json",
                                     o.output_of("service-render"), o.output_of("telemetry-render"), out)),
        Step("backup-credentials", "Host backup package, credentials and baseline backup",
             owner=lambda o: ["sudo " + quote(ws(o, "backup", "install")),
                              "sudo " + quote(ws(o, "backup", "credential", "repository")),
                              "sudo " + quote(ws(o, "backup", "credential", "password")),
                              "sudo " + quote(ws(o, "backup", "credential", "aws"))],
             reference="docs/OPERATIONS.md and docs/SERVICE-RECOVERY.md"),
        Step("service-credentials", "Private service credentials and Kubernetes Secrets",
             owner=lambda o: [quote(ws(o, "service", "credentials")) + " /private/new-service-credentials"],
             reference="docs/AI-SERVICE.md#prepare-and-roll-out step 4"),
        Step("web-search-key", "Web-search provider key Secret",
             owner=lambda o: ["kubectl --context <reviewed-context> --namespace ai-home-lab create secret generic "
                              "exa-search --from-file=api-key=/private/path/exa-api-key"],
             reference="docs/WEB-SEARCH.md#provision-and-connect"),
        Step("service-rollout", "Apply manifests, change replicas and switch sessions",
             owner=lambda o: [], reference="docs/AI-SERVICE.md#prepare-and-roll-out steps 3 and 5-8"),
    ]


class Postinstall:
    def __init__(self, state, inputs, *, root=ROOT, runner=default_runner, euid=None, boot_id=None,
                 out=sys.stdout, with_memory=False):
        self.root = Path(root)
        # Opt-in Graphiti memory profile; part of the render argv, so toggling it re-runs the render.
        self.with_memory = with_memory
        self.state = Path(os.path.abspath(state))
        self.inputs = {key: (value if key == "rocm_python" else os.path.abspath(value))
                       for key, value in inputs.items() if value}
        self.runner, self.out = runner, out
        self.euid = os.geteuid() if euid is None else euid
        self.boot_id = read_boot_id() if boot_id is None else boot_id
        self.steps = steps()
        self.by_id = {step.ident: step for step in self.steps}
        self.records = {}
        self.owner = None
        self.file_digests = {}
        for key, value in self.inputs.items():
            if key != "rocm_python":
                require(os.path.isfile(value), f"{INPUTS[key][0]} is not a readable regular file: {value}")
                self.file_digests[key] = file_sha256(value)

    # State directory: private, owner-checked and never followed through symlinks.
    def check_directory(self, path, owner):
        meta = os.lstat(path)
        require(not stat.S_ISLNK(meta.st_mode), f"refusing symlinked state path: {path}")
        require(stat.S_ISDIR(meta.st_mode), f"state path is not a directory: {path}")
        require(stat.S_IMODE(meta.st_mode) == 0o700, f"state directory must be mode 0700: {path}")
        require(meta.st_uid == owner, f"state directory has an unexpected owner: {path}")
        return meta

    def open_state(self, create):
        require(os.path.realpath(self.state) == str(self.state),
                f"refusing a state path through a symlink; use its resolved path: {self.state}")
        if not os.path.lexists(self.state):
            if not create:
                return False
            os.mkdir(self.state, 0o700)
            os.chmod(self.state, 0o700)
        meta = os.lstat(self.state)
        require(self.euid == 0 or meta.st_uid == self.euid, "state directory belongs to another user")
        self.owner = (meta.st_uid, meta.st_gid)
        self.check_directory(self.state, meta.st_uid)
        for name in ("records", "logs", "outputs"):
            path = self.state / name
            if not os.path.lexists(path) and create:
                os.mkdir(path, 0o700)
                os.chmod(path, 0o700)
                self.adopt(path)
            if os.path.lexists(path):
                self.check_directory(path, meta.st_uid)
        self.load_records()
        return True

    def adopt(self, path, descriptor=None):
        """Root-run files stay readable by the state owner; command outputs are left as created."""
        if self.euid == 0 and self.owner and self.owner[0] != 0:
            if descriptor is None:
                os.chown(path, *self.owner, follow_symlinks=False)
            else:
                os.fchown(descriptor, *self.owner)

    def create_file(self, path):
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        self.adopt(path, descriptor)
        return os.fdopen(descriptor, "wb")

    def load_records(self):
        directory = self.state / "records"
        for step in self.steps:
            path = directory / f"{step.ident}.json"
            if not os.path.lexists(path):
                continue
            require(stat.S_ISREG(os.lstat(path).st_mode), f"record is not a regular file: {path}")
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as stream:
                record = json.loads(stream.read())
            require(record.get("schema") == SCHEMA and record.get("step") == step.ident, f"invalid record: {path}")
            self.records[step.ident] = record

    def write_record(self, record):
        final = self.state / "records" / f"{record['step']}.json"
        temporary = final.with_name(f".{record['step']}.{os.getpid()}.tmp")
        with self.create_file(temporary) as stream:
            stream.write((json.dumps(record, indent=2, sort_keys=True) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, final)
        self.records[record["step"]] = record

    # Status evaluation.
    def output_of(self, ident):
        record = self.records.get(ident)
        return record.get("output") if record and record.get("exit_status") == 0 else None

    def missing(self, step):
        missing = [f"{INPUTS[key][0]} FILE ({INPUTS[key][1]})" for key in step.needs if key not in self.inputs]
        missing += [f"{tool} (not in this source tree; use a full reviewed checkout)"
                    for tool in step.tools if not (self.root / tool).is_file()]
        return missing

    def input_files(self, step, argv):
        """Digests of the reviewed files this step's argv consumes, plus the version lock."""
        files = {key: digest for key, digest in self.file_digests.items() if self.inputs[key] in argv}
        lock = self.root / "versions.lock"
        files["versions.lock"] = file_sha256(lock) if lock.is_file() else None
        return files

    def input_digest(self, step):
        argv = step.build(self, "{output}")
        upstream = {ident: self.records[ident].get("output_sha256") for ident in step.uses}
        return digest_json({"step": step.ident, "argv": argv, "files": self.input_files(step, argv),
                            "upstream": upstream})

    def evaluate(self):
        items, gate, done = [], None, set()
        for step in self.steps:
            record = self.records.get(step.ident)
            if gate:
                items.append((step, "blocked-by-gate", gate))
            elif step.gate:
                if record and record.get("kind") == "owner-attestation":
                    done.add(step.ident)
                    items.append((step, "done", f"owner-attested {record.get('attested_utc')}"))
                else:
                    gate = step.ident
                    items.append((step, "owner-gate", step.reference))
            elif self.missing(step):
                items.append((step, "blocked-by-input", "; ".join(self.missing(step))))
            elif any(ident not in done for ident in step.uses):
                items.append((step, "pending", "after " + ", ".join(i for i in step.uses if i not in done)))
            elif not record:
                items.append((step, "pending", "not run"))
            elif record.get("exit_status") != 0:
                items.append((step, "pending", f"last attempt failed (exit {record.get('exit_status')})"))
            elif record.get("input_sha256") != self.input_digest(step):
                items.append((step, "pending", "inputs changed"))
            elif step.boot_bound and record.get("boot_id") != self.boot_id:
                items.append((step, "pending", "boot-bound; not yet run in this boot"))
            else:
                done.add(step.ident)
                items.append((step, "done", record.get("finished_utc", "")))
        return items

    # Guidance.
    def rerun(self, attest=None):
        config = ["--config", self.inputs["workstation_config"]] if self.inputs.get("workstation_config") else []
        argv = [str(self.root / "bin/workstationctl"), *config, "postinstall", "run", str(self.state)]
        for key, (flag, _) in INPUTS.items():
            if key != "workstation_config" and self.inputs.get(key):
                argv += [flag, self.inputs[key]]
        argv += ["--with-memory"] if self.with_memory else []
        return quote(argv + (["--owner-completed", attest] if attest else []))

    def privilege_mismatch(self, step):
        return (step.run_as == "root" and self.euid != 0) or (step.run_as == "user" and self.euid == 0)

    def guidance(self, step, status, detail):
        if step.gate:
            lines = [f"STOP: owner gate '{step.ident}': {step.title}.",
                     f"  Reference: {step.reference}"] + ([f"  Note: {step.note}"] if step.note else [])
            commands = step.owner(self)
            lines += ["  Owner runs locally (never an agent):"] + [f"    {command}" for command in commands] \
                if commands else ["  Owner follows the reference procedure locally (never an agent)."]
            lines += ["  After the owner completes and checks it, the owner records completion with:",
                      f"    {self.rerun(step.ident)}"]
            return "\n".join(lines)
        if status == "blocked-by-input":
            return f"STOP: '{step.ident}' needs: {detail}\n  Then rerun: {self.rerun()}"
        if step.run_as == "root":
            return f"STOP: '{step.ident}' must run as root. Next command:\n    sudo {self.rerun()}"
        return (f"STOP: '{step.ident}' must run as the named non-root user (no sudo). Next command:\n"
                f"    {self.rerun()}")

    def plan(self):
        self.open_state(create=False)
        role = "root" if self.euid == 0 else f"user uid {self.euid}"
        print(f"Post-install plan for {self.state} (boot {self.boot_id or 'unknown'}; {role}). Nothing was run.",
              file=self.out)
        items = self.evaluate()
        for number, (step, status, detail) in enumerate(items, 1):
            actor = "owner" if step.gate else step.run_as
            print(f"{number:3} {status:17} {actor:5} {step.ident:20} {step.title}"
                  + (f" [{detail}]" if detail and status != "done" else ""), file=self.out)
        pending = next((item for item in items if item[1] != "done"), None)
        print(self.guidance(*pending) if pending and (pending[0].gate or pending[1] == "blocked-by-input" or
                                                      self.privilege_mismatch(pending[0]))
              else (f"Next: {self.rerun()}" if pending else "All post-install steps are complete."), file=self.out)
        return 0

    # Execution.
    def run(self, attest=None):
        require(attest is None or (attest in self.by_id and self.by_id[attest].gate),
                f"--owner-completed must name an owner gate, not {attest}")
        self.open_state(create=True)
        lock = os.open(self.state / ".lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            self.adopt(self.state / ".lock", lock)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError("another postinstall run holds this state directory") from error
            return self.run_locked(attest)
        finally:
            os.close(lock)

    def run_locked(self, attest):
        attempted = set()
        while True:
            pending = next((item for item in self.evaluate() if item[1] != "done"), None)
            if pending is None:
                print("All post-install steps are complete.", file=self.out)
                return 0
            step, status, detail = pending
            if step.gate and step.ident == attest:
                self.write_record({"schema": SCHEMA, "step": step.ident, "kind": "owner-attestation",
                                   "attested_utc": utc(), "uid": os.getuid(), "boot_id": self.boot_id,
                                   "note": "owner-reported completion; postinstall did not perform or verify it"})
                print(f"Recorded owner completion of '{step.ident}'.", file=self.out)
                attest = None
                continue
            stop = (STOP_GATE if step.gate else STOP_INPUT if status == "blocked-by-input"
                    else STOP_PRIVILEGE if self.privilege_mismatch(step) else None)
            if stop:
                if attest:
                    print(f"Not recorded: --owner-completed {attest}; '{step.ident}' must be settled first.",
                          file=self.out)
                print(self.guidance(*pending), file=self.out)
                return stop
            require(step.ident not in attempted, f"step did not settle after execution: {step.ident}")
            require(not step.boot_bound or self.boot_id, "boot-bound steps need the current boot_id")
            attempted.add(step.ident)
            status = self.execute(step)
            if status != 0:
                record = self.records[step.ident]
                print(f"FAILED: '{step.ident}' exited {status}. Log: {record['log']}\n"
                      f"  Review it, correct the cause, then rerun: {self.rerun()}", file=self.out)
                return STOP_FAILED

    def execute(self, step):
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        output = self.state / "outputs" / f"{step.ident}-{stamp}" if step.output else None
        argv = step.build(self, str(output) if step.output == "dir" else None)
        log_path = self.state / "logs" / f"{step.ident}-{stamp}.log"
        digest = self.input_digest(step)
        print(f"RUN {step.ident}: {quote(argv)}", file=self.out, flush=True)
        started = utc()
        with self.create_file(log_path) as log:
            if step.output == "stdout":
                os.mkdir(output, 0o700)
                os.chmod(output, 0o700)
                self.adopt(output)
                with self.create_file(output / "rendered.yaml") as stdout:
                    status = self.runner(argv, cwd=str(self.root), stdout=stdout, stderr=log)
            else:
                status = self.runner(argv, cwd=str(self.root), stdout=log, stderr=log)
        record = {"schema": SCHEMA, "step": step.ident, "kind": "command", "argv": argv, "run_as": step.run_as,
                  "euid": self.euid, "boot_bound": step.boot_bound, "boot_id": self.boot_id,
                  "input_sha256": digest, "input_files_sha256": self.input_files(step, argv),
                  "upstream_output_sha256": {i: self.records[i].get("output_sha256") for i in step.uses},
                  "exit_status": status, "started_utc": started, "finished_utc": utc(),
                  "output": str(output) if output else None,
                  "output_sha256": tree_sha256(output) if output else None,
                  "log": str(log_path), "log_sha256": file_sha256(log_path)}
        self.write_record(record)
        return status


def read_boot_id(path=BOOT_ID):
    try:
        value = path.read_text().strip()
    except OSError:
        return None
    return value if re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", value) else None


def main(argv=None):
    parser = argparse.ArgumentParser(prog="workstationctl postinstall", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("plan", "run"))
    parser.add_argument("state", help="private state directory (created 0700 by run; symlinks refused)")
    for key, (flag, text) in INPUTS.items():
        parser.add_argument(flag, dest=key, help=text)
    parser.add_argument("--with-memory", action="store_true",
                        help="render the opt-in Graphiti memory profile (assistant-memory)")
    parser.add_argument("--owner-completed", metavar="GATE",
                        help="owner only: record that the owner completed this gate; never pass it as an agent")
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        orchestrator = Postinstall(args.state, {key: getattr(args, key) for key in INPUTS},
                                   with_memory=args.with_memory)
        if args.action == "plan":
            require(args.owner_completed is None, "--owner-completed is only valid with run")
            return orchestrator.plan()
        return orchestrator.run(args.owner_completed)
    except (OSError, ValueError, KeyError) as error:
        print(f"workstationctl postinstall: {error}", file=sys.stderr)
        return STOP_USAGE


if __name__ == "__main__":
    sys.exit(main())
