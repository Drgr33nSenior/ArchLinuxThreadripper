"""Protected task acceptance: an independent verdict for one agent attempt.

The acceptance source (specification, hidden oracle tests, trusted and
known-bug references) is a directory apart from the agent workspace. The
evaluator pins its seal, so a rewritten source is never trusted. Only the
task's editable files cross from the workspace into a private evaluation copy;
tests, hooks and every other file the agent wrote never become the oracle.
Commands run only through coding_eval.run_task, which needs the network-free
Linux sandbox. Without it the oracle is ``unavailable``, never passed.

The final check opens the produced files and links itself and compares them
with an external-state observation that the evaluator supplies. A completion
claim, IDE or project feedback and agent-written tests are recorded as
evidence and never decide the verdict. docs/TASK-ACCEPTANCE.md is the contract.
"""
import argparse
import datetime
import difflib
import fnmatch
import http.client
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from assistant_acceptance import json_answer
from coding_eval import MAX_FILE, canonical, digest, json_equal, parse_object, read_json, run_task, write


SCHEMA = 1
SPEC_KIND = "workstation-task-acceptance-spec"
RESULT_KIND = "workstation-task-acceptance-result"
SEAL_KIND = "workstation-task-acceptance-seal"
SPEC_NAME = "acceptance.json"
PASSED, FAILED, UNAVAILABLE, NOT_APPLICABLE = "passed", "failed", "unavailable", "not-applicable"
STATUSES = (PASSED, FAILED, UNAVAILABLE, NOT_APPLICABLE)
VERDICTS = ("accepted", "rejected", "unavailable")
# Components decide the verdict. The first two establish trust in the acceptance
# source; the others grade the attempt. Evidence is recorded and never decides.
COMPONENTS = ("protection", "references", "oracle", "interference", "external_state", "deliverable", "linkage")
ATTEMPT_COMPONENTS = COMPONENTS[2:]
EVIDENCE = ("test_diff", "workspace_suite", "generated_tests", "guidance", "claim")
EXIT_STATUS = {"accepted": 0, "rejected": 1, "unavailable": 3}
MAX_FILES = 2048
MAX_TREE = 32 * 1024 * 1024
DIFF_CHARS = 4000
IDENT = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
SUBJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
SEAL = re.compile(r"[0-9a-f]{64}\Z")
TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "object": dict, "array": list}
RUN_KEYS = ("status", "exit_status", "elapsed_seconds", "output_sha256", "isolated", "reason")
OUTCOMES = {"tests-passed": "pass", "tests-failed": "fail", "timeout": "fail"}
LIMITATIONS = [
    "Commands run only in the network-free Linux sandbox; on another host the oracle is unavailable.",
    "Submitted files share a process with the protected tests. The canary detects a manufactured pass, "
    "not every manipulation that targets one test.",
    "Agent-written tests, IDE or project feedback and the completion claim are evidence and never decide the verdict.",
]


def relative_path(value):
    """Return a normalised relative POSIX path; refuse absolute, parent and empty parts."""
    if (not isinstance(value, str) or not value or len(value) > 512 or "\\" in value or "\0" in value
            or any(part in ("", ".", "..") for part in value.split("/"))):
        raise ValueError("path must be a normalised relative path")
    return value


def read_tree(root, ignore=(), strict=True):
    """Map relative POSIX paths to file bytes below ``root``; never follow a link.

    ``strict`` (the acceptance source) refuses links, special files and
    oversized files. Otherwise (an agent workspace) such an entry maps to None.
    """
    root = Path(root)
    if not root.is_dir():
        raise ValueError("expected a directory")
    files, total = {}, 0
    for directory, names, leaves in os.walk(root):
        here = Path(directory)
        links = [name for name in names if (here / name).is_symlink()]
        names[:] = sorted(name for name in names if name not in ignore and name not in links)
        for leaf in sorted(links + leaves):
            if leaf in ignore:
                continue
            path = here / leaf
            name = path.relative_to(root).as_posix()
            meta = path.lstat()
            data = None
            if stat.S_ISREG(meta.st_mode) and meta.st_size <= MAX_FILE:
                data = path.read_bytes()
                total += len(data)
            elif strict:
                raise ValueError("acceptance source must hold only bounded regular files: " + name)
            if len(files) >= MAX_FILES or total > MAX_TREE:
                raise ValueError("directory exceeds the acceptance file bounds")
            files[name] = data
    return files


def subtree(files, prefix):
    return {name[len(prefix) + 1:]: data for name, data in files.items() if name.startswith(prefix + "/")}


def seal_of(files):
    if SPEC_NAME not in files:
        raise ValueError("acceptance source lacks " + SPEC_NAME)
    return digest(canonical({"schema": SCHEMA, "kind": SEAL_KIND,
                             "files": {name: digest(data) for name, data in files.items()}}))


def seal(source):
    """Digest of every file in the acceptance source. The evaluator pins it outside agent paths."""
    return seal_of(read_tree(source))


def _require(condition, message):
    if not condition:
        raise ValueError("invalid acceptance specification: " + message)


def _object(value, required, optional=()):
    _require(isinstance(value, dict) and set(required) <= set(value) <= set(required) | set(optional),
             "an object has members " + ", ".join(required))
    return value


def _inapplicable(value):
    """True for the explicit declaration {"applicable": false, "reason": ...}."""
    if not isinstance(value, dict) or "applicable" not in value:
        return False
    _require(set(value) == {"applicable", "reason"} and value["applicable"] is False
             and isinstance(value["reason"], str) and 1 <= len(value["reason"]) <= 512,
             "a component that does not apply gives applicable=false and a reason")
    return True


def _identifier(value, seen):
    _require(isinstance(value, str) and IDENT.match(value) and value not in seen,
             "identifiers are unique lower-case names")
    seen.add(value)


def _json_path(value):
    _require(isinstance(value, list) and len(value) <= 16
             and all(isinstance(part, str) or type(part) is int for part in value),
             "a path is a list of object keys and array indexes")


def _command(value):
    _require(isinstance(value, list) and 1 <= len(value) <= 64
             and all(isinstance(argument, str) and len(argument) <= 4096 for argument in value)
             and value[0].startswith("/usr/bin/"), "a command is an argument list that starts with a /usr/bin program")


def _fields(value, seen):
    _require(isinstance(value, list) and len(value) <= 64, "field checks are a list")
    for check in value:
        _object(check, ("id", "path"), ("equals", "type"))
        _identifier(check["id"], seen)
        _json_path(check["path"])
        kind = check.get("type", "string")
        _require(("equals" in check) != ("type" in check) and isinstance(kind, str) and kind in TYPES,
                 "a field check has either equals or a known type")


def _selector(value, documents, artifacts):
    """Validate one linkage selector; return True when it reads the state observation."""
    kind = value.get("from") if isinstance(value, dict) else None
    if kind in ("state", "answer"):
        _json_path(_object(value, ("from", "path"))["path"])
        return kind == "state"
    _require(kind in ("artifact", "link", "artifact-sha256") and isinstance(value.get("name"), str),
             "a selector reads state, answer, artifact, link or artifact-sha256")
    if kind == "artifact-sha256":
        _require(set(value) == {"from", "name"} and value["name"] in artifacts, "a digest selector names a declared artifact")
    else:
        _json_path(_object(value, ("from", "name", "path"))["path"])
        _require((kind, value["name"]) in documents, "a selector names a declared JSON artifact or link")
    return False


def validate_spec(spec, files):
    """Refuse a specification that is ambiguous, unbounded or declares no check."""
    _object(spec, ("schema", "kind", "task_id", "version", "oracle", "external_state", "deliverable", "linkage"),
            ("editable", "ignore", "tests"))
    _require(spec["schema"] == SCHEMA and spec["kind"] == SPEC_KIND, "unsupported schema or kind")
    _require(isinstance(spec["task_id"], str) and IDENT.match(spec["task_id"]) and isinstance(spec["version"], str)
             and 1 <= len(spec["version"]) <= 64, "task_id and version are short names")
    editable, ignore, seen = spec.get("editable", []), spec.get("ignore", []), set()
    _require(isinstance(editable, list) and len(editable) <= 64, "editable is a list of paths")
    _require(len({relative_path(name) for name in editable}) == len(editable), "editable paths are unique")
    _require(isinstance(ignore, list) and len(ignore) <= 32
             and all(isinstance(name, str) and name and "/" not in name for name in ignore),
             "ignore is a list of file or directory names")
    oracle = spec["oracle"]
    if _inapplicable(oracle):
        _require(not editable and "tests" not in spec, "editable files and tests need an oracle")
    else:
        _object(oracle, ("command", "canary", "seconds", "baseline", "references"))
        _command(oracle["command"])
        _require(type(oracle["seconds"]) is int and 1 <= oracle["seconds"] <= 300, "seconds is 1 to 300")
        _require(oracle["baseline"] in ("pass", "fail"), "baseline is pass or fail")
        _require(editable, "an oracle needs at least one editable file")
        fixture, hidden = subtree(files, "fixture"), subtree(files, "oracle")
        _require(hidden and not set(hidden) & (set(fixture) | set(editable)),
                 "oracle/ holds protected files that no workspace path shadows")
        canary = subtree(files, relative_path(oracle["canary"]))
        _require(canary and set(canary) <= set(hidden), "the canary replaces protected files of the same name")
        references = oracle["references"]
        _require(isinstance(references, list) and 1 <= len(references) <= 16, "references is a list")
        names = {"baseline", "canary", "post-change"}
        for reference in references:
            _object(reference, ("id", "overlay", "expected"))
            _identifier(reference["id"], names)
            overlay = subtree(files, relative_path(reference["overlay"]))
            _require(overlay and set(overlay) <= set(editable), "a reference overlay holds only editable files")
            _require(reference["expected"] in ("pass", "fail"), "a reference is expected to pass or fail")
        expected = [reference["expected"] for reference in references] + [oracle["baseline"]]
        _require("pass" in expected[:-1] and "fail" in expected, "the oracle needs a trusted reference and a known bug")
        if "tests" in spec:
            tests = _object(spec["tests"], ("command", "patterns"))
            _command(tests["command"])
            _require(isinstance(tests["patterns"], list) and 1 <= len(tests["patterns"]) <= 16
                     and all(isinstance(pattern, str) and pattern and "/" not in pattern for pattern in tests["patterns"]),
                     "test patterns match file names")
    state, deliverable, linkage = spec["external_state"], spec["deliverable"], spec["linkage"]
    declared = [not _inapplicable(component) for component in (state, deliverable, linkage)]
    if declared[0]:
        _fields(_object(state, ("checks",))["checks"], seen)
        _require(state["checks"] and all("equals" in check for check in state["checks"]),
                 "external_state lists checks that compare with an expected value")
    documents, artifacts = set(), set()
    if declared[1]:
        _object(deliverable, (), ("answer", "artifacts", "links"))
        _fields(deliverable.get("answer", []), seen)
        for kind in ("artifact", "link"):
            items = deliverable.get(kind + "s", [])
            _require(isinstance(items, list) and len(items) <= 32, "artifacts and links are lists")
            for item in items:
                _object(item, ("id", "format"), ("path", "answer_path", "fields"))
                _identifier(item["id"], seen)
                _require(item["format"] in ("json", "bytes") and ("fields" not in item or item["format"] == "json"),
                         "format is json or bytes; only json has fields")
                _require(("path" in item) != ("answer_path" in item) and (kind == "artifact" or "answer_path" in item),
                         "an artifact has a path or an answer_path; a link has an answer_path")
                if "path" in item:
                    relative_path(item["path"])
                else:
                    _json_path(item["answer_path"])
                _fields(item.get("fields", []), seen)
                if item["format"] == "json":
                    documents.add((kind, item["id"]))
                if kind == "artifact":
                    artifacts.add(item["id"])
        _require(any(deliverable.get(key) for key in ("answer", "artifacts", "links")), "deliverable declares a check")
    if declared[2]:
        _require(declared[0] and declared[1], "linkage needs external_state and deliverable")
        checks = _object(linkage, ("checks",))["checks"]
        _require(isinstance(checks, list) and 1 <= len(checks) <= 64, "linkage lists checks")
        for check in checks:
            _object(check, ("id", "left", "right"))
            _identifier(check["id"], seen)
            _require(_selector(check["left"], documents, artifacts) != _selector(check["right"], documents, artifacts),
                     "a linkage check compares one state value with one deliverable value")
    _require(any(declared) or not _inapplicable(oracle), "the specification declares no check")


def load_source(source):
    """Read and validate an acceptance source: specification, seal and its file trees."""
    return load_files(read_tree(source))


def load_files(files):
    sealed = seal_of(files)
    try:
        spec = parse_object(files[SPEC_NAME].decode())
    except UnicodeDecodeError:
        raise ValueError("invalid acceptance specification: not UTF-8 JSON") from None
    validate_spec(spec, files)
    overlays, canary = {}, {}
    if not _inapplicable(spec["oracle"]):
        overlays = {reference["id"]: subtree(files, reference["overlay"]) for reference in spec["oracle"]["references"]}
        canary = subtree(files, spec["oracle"]["canary"])
    return {"spec": spec, "spec_sha256": digest(files[SPEC_NAME]), "seal": sealed, "fixture": subtree(files, "fixture"),
            "hidden": subtree(files, "oracle"), "canary": canary, "overlays": overlays}


def prepare_workspace(source, destination):
    """Copy the agent-visible fixture to a new directory outside the acceptance source."""
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination.exists() or destination == source or source in destination.parents:
        raise ValueError("workspace must be a new directory outside the acceptance source")
    fixture = load_source(source)["fixture"]
    destination.mkdir(parents=True)
    for name, data in fixture.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def combine(rows):
    statuses = {row["status"] for row in rows}
    return FAILED if FAILED in statuses else UNAVAILABLE if UNAVAILABLE in statuses else PASSED


def lookup(value, path):
    for part in path:
        if isinstance(part, str) and isinstance(value, dict) and part in value:
            value = value[part]
        elif type(part) is int and isinstance(value, list) and 0 <= part < len(value):
            value = value[part]
        else:
            return False, None
    return True, value


def field(check, document):
    """Grade one declared field of a document that the checker opened itself."""
    status, value = document
    if status != PASSED:
        return {"id": check["id"], "status": status, "reason": value}
    found, actual = lookup(value, check["path"])
    if not found:
        reason = "field-missing"
    elif "equals" in check:
        reason = None if json_equal(actual, check["equals"]) else "field-mismatch"
    else:
        number = check["type"] in ("integer", "number") and isinstance(actual, bool)
        reason = None if isinstance(actual, TYPES[check["type"]]) and not number else "field-wrong-type"
    return {"id": check["id"], "status": PASSED} if reason is None else {"id": check["id"], "status": FAILED, "reason": reason}


def bounded_file(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE:
        raise ValueError(f"{path.name} must be a regular bounded file")
    return path.read_bytes()


def open_file(root, name):
    """(status, bytes or reason) for a regular file inside ``root``; refuse links and escapes."""
    try:
        path = Path(root)
        for part in relative_path(name).split("/"):
            path = path / part
            meta = path.lstat()
            if stat.S_ISLNK(meta.st_mode):
                return FAILED, "symbolic-link"
    except ValueError:
        return FAILED, "path-not-relative"
    except OSError:
        return FAILED, "missing"
    if not stat.S_ISREG(meta.st_mode):
        return FAILED, "not-a-regular-file"
    if meta.st_size > MAX_FILE:
        return FAILED, "too-large"
    return PASSED, path.read_bytes()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *arguments, **options):
        return None


def open_url(url, origins, timeout=10):
    """GET a link on an origin the evaluator allowed: no proxy, redirect or credential."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return FAILED, "link-not-a-url"
    if parts.scheme not in ("http", "https") or not parts.netloc or len(url) > 2048:
        return FAILED, "link-not-a-url"
    if f"{parts.scheme}://{parts.netloc}" not in origins:
        return UNAVAILABLE, "link-origin-not-allowed"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(urllib.request.Request(url), timeout=timeout) as response:
            body = response.read(MAX_FILE + 1)
    except urllib.error.HTTPError as error:
        return FAILED, f"link-http-{error.code}"
    except (ValueError, http.client.InvalidURL):
        return FAILED, "link-not-a-url"
    except (urllib.error.URLError, http.client.HTTPException, OSError):
        return UNAVAILABLE, "link-unreachable"
    return (PASSED, body) if len(body) <= MAX_FILE else (FAILED, "link-too-large")


def read_answer(path):
    """(record, document) for the final answer: one JSON object, optionally in one fence."""
    if path is None:
        return {"status": UNAVAILABLE, "reason": "answer-not-supplied"}, (UNAVAILABLE, "answer-not-supplied")
    data = bounded_file(path)
    try:
        text, fenced = json_answer(data.decode().strip())
        return {"status": PASSED, "sha256": digest(data), "fenced": fenced}, (PASSED, parse_object(text))
    except ValueError:
        reason = "answer-not-a-json-object"
        return {"status": FAILED, "reason": reason, "sha256": digest(data)}, (FAILED, reason)


def read_state(path, workspace):
    """(record, document) for the evaluator's observation of the external state."""
    reason = "state-unresolved"
    if path is not None:
        resolved = Path(path).resolve()
        if resolved == workspace or workspace in resolved.parents:
            reason = "state-inside-workspace"
        else:
            try:
                value, sha256 = read_json(path)
                if isinstance(value, dict):
                    return {"status": PASSED, "sha256": sha256}, (PASSED, value)
            except (OSError, ValueError):
                pass
            reason = "state-unreadable"
    return {"status": UNAVAILABLE, "reason": reason}, (UNAVAILABLE, reason)


def located(item, answer):
    """The declared path, or the path or URL that the answer names."""
    if "path" in item:
        return PASSED, item["path"]
    status, value = answer
    if status != PASSED:
        return status, value
    found, target = lookup(value, item["answer_path"])
    return (PASSED, target) if found and isinstance(target, str) else (FAILED, "answer-field-missing")


def opened(item, status, data):
    """(row, document, digest document) for content that was opened, parsed as declared."""
    row = {"id": item["id"], "status": status}
    if status == PASSED:
        row.update(sha256=digest(data), bytes=len(data))
        if item["format"] == "bytes":
            return row, (PASSED, None), (PASSED, row["sha256"])
        try:
            return row, (PASSED, parse_object(data.decode())), (PASSED, row["sha256"])
        except ValueError:
            status, data = FAILED, "not-a-json-object"
    row.update(status=status, reason=data)
    return row, (status, data), (status, data)


def final_components(spec, workspace, answer, state, origins):
    """Grade external state, the deliverable and their linkage as three separate components."""
    answer_record, answer_document = read_answer(answer)
    state_record, state_document = read_state(state, workspace)
    documents = {"answer": answer_document, "state": state_document}
    components = {}
    declared = spec["external_state"]
    if _inapplicable(declared):
        components["external_state"] = {"status": NOT_APPLICABLE, "reason": declared["reason"]}
    else:
        checks = [field(check, state_document) for check in declared["checks"]]
        components["external_state"] = {"status": combine(checks), "observation": state_record, "checks": checks}
    declared = spec["deliverable"]
    if _inapplicable(declared):
        components["deliverable"] = {"status": NOT_APPLICABLE, "reason": declared["reason"]}
    else:
        checks = [field(check, answer_document) for check in declared.get("answer", [])]
        rows = {"artifacts": [], "links": []}
        for kind in ("artifact", "link"):
            for item in declared.get(kind + "s", []):
                status, target = located(item, answer_document)
                named = {}
                if status == PASSED and kind == "artifact":
                    status, target = open_file(workspace, target)
                    target = target if status == PASSED else "artifact-" + target
                elif status == PASSED:
                    named["url_sha256"] = digest(target.encode())
                    status, target = open_url(target, origins)
                row, document, digests = opened(item, status, target)
                row.update(named)
                rows[kind + "s"].append(row)
                documents[kind, item["id"]] = document
                if kind == "artifact":
                    documents["artifact-sha256", item["id"]] = digests
                checks += [field(check, document) for check in item.get("fields", [])]
        components["deliverable"] = {"status": combine(checks + rows["artifacts"] + rows["links"]),
                                     "answer": answer_record, **rows, "checks": checks}
    declared = spec["linkage"]
    if _inapplicable(declared):
        components["linkage"] = {"status": NOT_APPLICABLE, "reason": declared["reason"]}
    else:
        checks = []
        for check in declared["checks"]:
            sides = []
            for selector in (check["left"], check["right"]):
                kind = selector["from"]
                status, value = documents[kind if kind in ("state", "answer") else (kind, selector["name"])]
                if status == PASSED and kind != "artifact-sha256":
                    found, value = lookup(value, selector["path"])
                    status, value = (PASSED, value) if found else (FAILED, "field-missing")
                sides.append((status, value))
            row = {"id": check["id"], "status": combine([{"status": status} for status, _ in sides])}
            if row["status"] == PASSED:
                row.update(left_sha256=digest(canonical(sides[0][1])), right_sha256=digest(canonical(sides[1][1])))
                if not json_equal(sides[0][1], sides[1][1]):
                    row.update(status=FAILED, reason="mismatch")
            else:
                row["reason"] = next(value for status, value in sides if status == row["status"])
            checks.append(row)
        components["linkage"] = {"status": combine(checks), "checks": checks}
    return components


def execute(run, tree, command, seconds):
    """Run one command on a private copy of ``tree`` through the injected executor."""
    with tempfile.TemporaryDirectory(prefix="task-acceptance-") as temporary:
        root = Path(temporary) / "tree"
        root.mkdir()
        for name, data in tree.items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        result = run(root, {}, command, seconds=seconds)
    return {key: result[key] for key in RUN_KEYS if key in result}


def outcome(record):
    return OUTCOMES.get(record["status"], "unavailable")


def submitted_files(workspace, editable):
    """Editable files as the workspace holds them; a missing file is submitted as deleted."""
    files, problems = {}, []
    for name in editable:
        status, data = open_file(workspace, name)
        if status == PASSED:
            files[name] = data
        elif data != "missing":
            problems.append("editable-" + data)
    return files, problems


def oracle_components(source, workspace, run):
    """Establish the reference outcomes, then grade the submitted files and the canary."""
    spec, fixture, hidden = source["spec"], source["fixture"], source["hidden"]
    oracle = spec["oracle"]
    if _inapplicable(oracle):
        return {name: {"status": NOT_APPLICABLE, "reason": oracle["reason"]}
                for name in ("references", "oracle", "interference")}

    def judge(tree, canary=False):
        # Protected files are written last: nothing from a workspace can replace them. The canary
        # run differs from the oracle run only in the content of protected files.
        protected = {**hidden, **source["canary"]} if canary else hidden
        return execute(run, {**tree, **protected}, oracle["command"], oracle["seconds"])

    trees = {reference["id"]: {**fixture, **source["overlays"][reference["id"]]} for reference in oracle["references"]}
    trusted = next(reference["id"] for reference in oracle["references"] if reference["expected"] == "pass")
    rows = [("baseline", oracle["baseline"], fixture)]
    rows += [(reference["id"], reference["expected"], trees[reference["id"]]) for reference in oracle["references"]]
    rows.append(("canary", "fail", trees[trusted]))
    outcomes = []
    for name, expected, tree in rows:
        record = judge(tree, canary=name == "canary")
        observed = outcome(record)
        outcomes.append({"id": name, "expected": expected, "observed": observed, "run": record,
                         "status": PASSED if observed == expected else UNAVAILABLE if observed == "unavailable" else FAILED})
    references = {"status": combine(outcomes), "outcomes": outcomes}
    files, problems = submitted_files(workspace, spec["editable"])
    evaluated = [{"path": name, "sha256": digest(files[name]) if name in files else None,
                  "changed": files.get(name) != fixture.get(name)} for name in spec["editable"]]
    if references["status"] != PASSED:
        # A reference that does not behave as declared makes the oracle itself unproven.
        blocked = {"status": UNAVAILABLE, "reason": "reference-outcomes-not-established"}
        return {"references": references, "oracle": {**blocked, "evaluated": evaluated}, "interference": blocked}
    if problems:
        return {"references": references, "oracle": {"status": FAILED, "reason": problems[0], "evaluated": evaluated},
                "interference": {"status": UNAVAILABLE, "reason": "submitted-files-not-evaluated"}}
    tree = {**{name: data for name, data in fixture.items() if name not in spec["editable"]}, **files}
    post, canary = judge(tree), judge(tree, canary=True)
    graded = {"status": {"pass": PASSED, "fail": FAILED}.get(outcome(post), UNAVAILABLE), "evaluated": evaluated,
              "baseline": outcomes[0]["observed"], "post_change": outcome(post), "run": post}
    if graded["status"] != PASSED:
        graded["reason"] = "post-change-failed" if graded["status"] == FAILED else post["status"]
    # The canary fails for every honest implementation; a pass was manufactured by the submitted files.
    interference = {"status": {"fail": PASSED, "pass": FAILED}.get(outcome(canary), UNAVAILABLE), "canary": canary}
    if interference["status"] != PASSED:
        interference["reason"] = "oracle-canary-passed" if interference["status"] == FAILED else canary["status"]
    return {"references": references, "oracle": graded, "interference": interference}


def suite(source, run, others, submitted, names):
    """Validate agent-side tests by what they report for the baseline and each reference."""
    spec = source["spec"]
    oracle, editable = spec["oracle"], {name: data for name, data in source["fixture"].items() if name in spec["editable"]}
    variants = [("baseline", oracle["baseline"], editable)]
    variants += [(reference["id"], reference["expected"], {**editable, **source["overlays"][reference["id"]]})
                 for reference in oracle["references"]]
    if submitted is not None:
        variants.append(("post-change", None, submitted))
    outcomes = []
    for name, expected, files in variants:
        record = execute(run, {**others, **files}, spec["tests"]["command"], oracle["seconds"])
        outcomes.append({"id": name, "expected": expected, "observed": outcome(record), "run": record})
    known = [row for row in outcomes if row["expected"] is not None]
    reasons = []
    if any(row["expected"] == "pass" and row["observed"] == "fail" for row in known):
        reasons.append("fails-trusted")
    if not any(row["expected"] == "fail" and row["observed"] == "fail" for row in known):
        reasons.append("detects-no-known-bug")
    unavailable = any(row["observed"] == "unavailable" for row in known)
    return {"status": UNAVAILABLE if unavailable else "invalid" if reasons else "validated",
            "reasons": [] if unavailable else reasons, "files": names, "outcomes": outcomes,
            "reproducer": {"baseline": outcomes[0]["observed"],
                           "post_change": outcomes[-1]["observed"] if submitted is not None else "not-evaluated"}}


def text_diff(name, before, after):
    try:
        old, new = before.decode().splitlines(), after.decode().splitlines()
    except UnicodeDecodeError:
        return {}
    lines = list(difflib.unified_diff(old, new, "protected/" + name, "workspace/" + name, lineterm="", n=1))
    text = "\n".join(lines)
    return {"added_lines": sum(line.startswith("+") and not line.startswith("+++") for line in lines),
            "removed_lines": sum(line.startswith("-") and not line.startswith("---") for line in lines),
            "diff": text[:DIFF_CHARS], "diff_truncated": len(text) > DIFF_CHARS}


def workspace_evidence(source, workspace, run):
    """Record what the agent changed beside the editable files and validate its own tests."""
    spec = source["spec"]
    names = ("test_diff", "workspace_suite", "generated_tests")
    if _inapplicable(spec["oracle"]):
        return {name: {"status": NOT_APPLICABLE} for name in names}
    try:
        present = read_tree(workspace, spec.get("ignore", ()), strict=False)
    except ValueError:
        return {name: {"status": UNAVAILABLE, "reason": "workspace-exceeds-bounds"} for name in names}
    protected = {name: data for name, data in source["fixture"].items() if name not in spec["editable"]}
    changed = {name: data for name, data in present.items()
               if name not in spec["editable"] and (name not in protected or protected[name] != data)}
    deleted = sorted(name for name in protected if name not in present)
    entries = [{"path": name, "change": "deleted", "protected_sha256": digest(protected[name])} for name in deleted]
    for name, data in sorted(changed.items()):
        entry = {"path": name, "change": "modified" if name in protected else "added",
                 "workspace_sha256": digest(data) if data is not None else None}
        if name in protected:
            entry["protected_sha256"] = digest(protected[name])
            entry.update(text_diff(name, protected[name], data) if data is not None else {})
        entries.append(entry)
    evidence = {"test_diff": {"status": "changed" if entries else "clean", "entries": entries}}
    if "tests" not in spec:
        status = {"status": "not-declared"}
    elif not entries:
        status = {"status": "none"}
    elif None in changed.values():
        status = {"status": UNAVAILABLE, "reason": "unreadable-workspace-file"}
    else:
        status = None
    if status is not None:
        return {**evidence, "workspace_suite": status, "generated_tests": status}
    files, problems = submitted_files(workspace, spec["editable"])
    submitted = None if problems else files
    visible = {**{name: data for name, data in protected.items() if name in present}, **changed}

    def is_test(name):
        return any(fnmatch.fnmatchcase(name.rsplit("/", 1)[-1], pattern) for pattern in spec["tests"]["patterns"])

    authored = sorted(name for name in changed if is_test(name))
    # The whole agent-visible suite shows whether the changes stopped it from detecting known bugs.
    evidence["workspace_suite"] = suite(source, run, visible, submitted, sorted(changed) + deleted)
    # Agent-written tests alone, without the unchanged visible tests that would mask a vacuous test.
    alone = {name: data for name, data in visible.items() if name in changed or not is_test(name)}
    evidence["generated_tests"] = suite(source, run, alone, submitted, authored) if authored else {"status": "none"}
    return evidence


def guidance_record(path):
    """IDE or project feedback from the agent's repair loop: recorded, never authoritative."""
    if path is None:
        return {"supplied": False, "authoritative": False}
    data = bounded_file(path)
    try:
        value = parse_object(data.decode())
    except UnicodeDecodeError:
        value = {}
    if not all(type(value.get(key)) is bool for key in ("failures", "unavailable")):
        raise ValueError("guidance must record failures and unavailable as booleans")
    return {"supplied": True, "authoritative": False, "sha256": digest(data),
            "failures": value["failures"], "unavailable": value["unavailable"]}


def verify_source(source, workspace, expected_seal):
    """(protection record, loaded source or None): trust only a sealed source apart from the workspace."""
    record = {"status": FAILED, "expected_seal_sha256": expected_seal, "seal_sha256": None}
    if source == workspace or source in workspace.parents or workspace in source.parents:
        return {**record, "reason": "source-overlaps-workspace"}, None
    try:
        files = read_tree(source)
        record["seal_sha256"] = seal_of(files)
    except ValueError:
        return {**record, "reason": "source-unreadable"}, None
    if expected_seal is None:
        return {**record, "status": UNAVAILABLE, "reason": "no-expected-seal"}, None
    if record["seal_sha256"] != expected_seal:
        return {**record, "reason": "seal-mismatch"}, None
    # The verified bytes are the ones used; the source is not read again.
    return {**record, "status": PASSED}, load_files(files)


def verdict(components):
    """The fixed rule from component statuses to the verdict."""
    statuses = {name: components[name]["status"] for name in COMPONENTS}
    if statuses["protection"] == PASSED and all(status in (PASSED, NOT_APPLICABLE) for status in statuses.values()):
        return "accepted"
    if statuses["protection"] == PASSED and any(statuses[name] == FAILED for name in ATTEMPT_COMPONENTS):
        return "rejected"
    return "unavailable"


def reasons(components):
    """Stable codes for every component that did not pass."""
    codes = []
    for name in COMPONENTS:
        component = components[name]
        if component["status"] in (PASSED, NOT_APPLICABLE):
            continue
        if "reason" in component:
            codes.append(f"{name}:{component['reason']}")
        for key in ("outcomes", "artifacts", "links", "checks"):
            codes += [f"{name}:{row['id']}" for row in component.get(key, ()) if row["status"] != PASSED]
    return codes


def check_task(source, workspace, expected_seal=None, *, answer=None, state=None, origins=(), claim=None,
               guidance=None, run_id=None, attempt_id=None, run=run_task):
    """Return the acceptance result for one attempt. No option skips a declared check.

    ``run`` defaults to the sandboxed coding_eval.run_task. Replace it only to
    run authored fixtures in tests, never for model output.
    """
    source, workspace = Path(source).resolve(), Path(workspace).resolve()
    if not workspace.is_dir():
        raise ValueError("workspace must be a directory")
    if expected_seal is not None and not (isinstance(expected_seal, str) and SEAL.match(expected_seal)):
        raise ValueError("expected seal must be a SHA-256 digest")
    if any(value is not None and not (isinstance(value, str) and SUBJECT.match(value)) for value in (run_id, attempt_id)):
        raise ValueError("run and attempt identifiers are short names")
    if claim is not None and type(claim) is not bool:
        raise ValueError("claim is true, false or absent")
    evidence = {"guidance": guidance_record(guidance)}
    protection, loaded = verify_source(source, workspace, expected_seal)
    if loaded is None:
        # An unverified source is never parsed for checks and nothing from it runs.
        blocked = {"status": UNAVAILABLE, "reason": "acceptance-source-not-verified"}
        components = {name: dict(blocked) for name in COMPONENTS[1:]}
        evidence.update({name: dict(blocked) for name in EVIDENCE[:3]})
    else:
        # The final checks read the evaluator's inputs first, so an input error stops before any command runs.
        final = final_components(loaded["spec"], workspace, answer, state, tuple(origins))
        components = {**oracle_components(loaded, workspace, run), **final}
        evidence.update(workspace_evidence(loaded, workspace, run))
    components = {"protection": protection, **components}
    decided = verdict(components)
    feedback = evidence["guidance"]
    feedback["contradicted"] = (feedback["supplied"] and decided != "accepted"
                                and not (feedback["failures"] or feedback["unavailable"]))
    evidence["claim"] = {"completed": claim, "decides_verdict": False,
                         "contradicted": claim is True and decided != "accepted"}
    spec = loaded["spec"] if loaded else {}
    return {"schema": SCHEMA, "kind": RESULT_KIND, "task_id": spec.get("task_id"), "spec_version": spec.get("version"),
            "spec_sha256": loaded["spec_sha256"] if loaded else None,
            "subject": {"run_id": run_id, "attempt_id": attempt_id},
            "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "checker_sha256": digest(Path(__file__).read_bytes()),
            "verdict": decided, "reasons": reasons(components),
            "components": {name: components[name] for name in COMPONENTS},
            "evidence": {name: evidence[name] for name in EVIDENCE}, "limitations": LIMITATIONS}


def validate_result(value):
    """Refuse a record that is malformed or whose verdict its components do not support."""
    keys = {"schema", "kind", "task_id", "spec_version", "spec_sha256", "subject", "created_utc", "checker_sha256",
            "verdict", "reasons", "components", "evidence", "limitations"}
    if not isinstance(value, dict) or set(value) != keys or value["schema"] != SCHEMA or value["kind"] != RESULT_KIND:
        raise ValueError("not a task acceptance result of this schema")
    try:
        return _validated(value)
    except (AttributeError, KeyError, TypeError):
        raise ValueError("malformed task acceptance result") from None


def _validated(value):
    components, evidence, subject = value["components"], value["evidence"], value["subject"]
    if (not isinstance(components, dict) or set(components) != set(COMPONENTS)
            or not all(isinstance(row, dict) and row.get("status") in STATUSES for row in components.values())
            or components["protection"]["status"] == NOT_APPLICABLE):
        raise ValueError("result must record every component with a known status")
    if value["verdict"] != verdict(components) or value["reasons"] != reasons(components):
        raise ValueError("verdict or reasons do not follow from the recorded components")
    if (not isinstance(evidence, dict) or set(evidence) != set(EVIDENCE)
            or not all(isinstance(row, dict) for row in evidence.values())
            or evidence["claim"].get("decides_verdict") is not False or evidence["guidance"].get("authoritative") is not False):
        raise ValueError("result must record evidence that does not decide the verdict")
    if (not isinstance(subject, dict) or set(subject) != {"run_id", "attempt_id"}
            or any(item is not None and not (isinstance(item, str) and SUBJECT.match(item)) for item in subject.values())):
        raise ValueError("result subject must hold run_id and attempt_id")
    verified = components["protection"]["status"] == PASSED
    if (isinstance(value["task_id"], str) and bool(IDENT.match(value["task_id"]))) != verified:
        raise ValueError("task_id is recorded exactly when the acceptance source was verified")
    return value


def load_result(path):
    """Read and validate a result file; the interface for consumers that join results to run records."""
    return validate_result(read_json(path)[0])


def main(arguments=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="action", required=True)
    command = commands.add_parser("seal", help="print the seal of an acceptance source")
    command.add_argument("source")
    command = commands.add_parser("workspace", help="copy the agent-visible fixture to a new workspace")
    command.add_argument("source")
    command.add_argument("destination")
    command = commands.add_parser("check", help="write the acceptance result; exit 0 accepted, 1 rejected, 3 unavailable")
    command.add_argument("source")
    command.add_argument("output", help="new result file outside the workspace")
    command.add_argument("--workspace", required=True)
    command.add_argument("--seal", required=True, help="seal pinned outside every agent-editable path")
    command.add_argument("--answer", help="file with the final answer exactly as returned")
    command.add_argument("--state", help="JSON observation of the external state, taken by the evaluator")
    command.add_argument("--allow-origin", action="append", default=[], help="scheme://host[:port] whose links may be opened")
    command.add_argument("--claim", choices=("completed", "not-completed"), help="the model's completion claim; recorded only")
    command.add_argument("--guidance", help="JSON IDE or project feedback from the repair loop; recorded only")
    command.add_argument("--run-id")
    command.add_argument("--attempt-id")
    command = commands.add_parser("validate", help="validate a result file; print its verdict")
    command.add_argument("result")
    args = parser.parse_args(arguments)
    try:
        if args.action == "seal":
            print(seal(args.source))
        elif args.action == "workspace":
            prepare_workspace(args.source, args.destination)
        elif args.action == "validate":
            print(load_result(args.result)["verdict"])
        else:
            output, workspace = Path(args.output).resolve(), Path(args.workspace).resolve()
            if workspace == output or workspace in output.parents:
                raise ValueError("result must be written outside the workspace")
            claim = None if args.claim is None else args.claim == "completed"
            result = check_task(args.source, workspace, args.seal, answer=args.answer, state=args.state,
                                origins=args.allow_origin, claim=claim, guidance=args.guidance,
                                run_id=args.run_id, attempt_id=args.attempt_id)
            write(output, result)
            print(" ".join([result["verdict"], *result["reasons"]]))
            return EXIT_STATUS[result["verdict"]]
    except (OSError, ValueError) as error:
        print("error:", error, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
