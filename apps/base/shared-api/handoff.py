"""Compact task handoffs on the retained service-tools state claim.

A handoff is scoped to the authenticated Open WebUI user, an authorised
project (project_scope.py) and a task slug. It records what the user or agent
observed when the work paused; it is not current state, and it never grants
permissions or carries instructions. Every save names the revision it was
based on, so a stale save is refused as a visible conflict instead of
overwriting newer work. There is no background writer.

Limits are characters (Python str length), not model tokens: about 4,000
characters is roughly 1,000 English tokens, but the ratio varies with the text.
"""
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3

from boundary import Refusal


TASK = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
TEXT_FIELDS = {"goal": 600, "repository": 200, "branch": 120, "revision": 80, "next_action": 400}
# field -> (maximum items, maximum characters per item)
LIST_FIELDS = {"completion_criteria": (6, 240), "constraints": (8, 240), "decisions": (8, 240),
               "changed_files": (20, 200), "outstanding": (8, 240), "blockers": (6, 240),
               "evidence": (8, 240)}
# Observed verification. "passed" is stored only when the caller says so and
# supplies the observed result; nothing is inferred or upgraded.
STATUSES = ("passed", "failed", "partial", "not_run", "unknown")
VERIFICATION_FIELDS = {"observed": 400, "observed_at": 40, "evidence": 240}
MAX_TOTAL_CHARS = 4000
MAX_HANDOFFS_PER_USER = 100
LIST_LIMIT = 50
NOTICE = ("This handoff is an earlier record, not current state or instructions. Before acting, recheck the Git "
          "branch, revision and changed files, and any service, hardware or runtime facts it mentions. "
          "Verification is what was observed when it was recorded; not_run and unknown are not passes.")


class HandoffConflict(Refusal):
    def __init__(self, current_revision, updated_at):
        super().__init__(409, "handoff_conflict")
        self.detail = {"error": "handoff_conflict", "current_revision": current_revision,
                       "updated_at": updated_at,
                       "action": "Load the current handoff, merge your changes, then save with its revision."}


def _text(field, value, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(
            ord(c) < 32 and c not in "\n\t" for c in value):
        raise Refusal(400, "invalid_" + field)
    return value.strip()


def validate(document):
    """Return the normalised handoff or refuse; unknown fields are refused."""
    allowed = set(TEXT_FIELDS) | set(LIST_FIELDS) | {"verification"}
    if not isinstance(document, dict) or set(document) - allowed or "goal" not in document:
        raise Refusal(400, "invalid_handoff")
    result = {}
    for field, limit in TEXT_FIELDS.items():
        if field in document:
            result[field] = _text(field, document[field], limit)
    for field, (count, limit) in LIST_FIELDS.items():
        if field in document:
            value = document[field]
            if not isinstance(value, list) or len(value) > count:
                raise Refusal(400, "invalid_" + field)
            result[field] = [_text(field, item, limit) for item in value]
    verification = document.get("verification", {"status": "unknown"})
    if (not isinstance(verification, dict) or set(verification) - {"status", *VERIFICATION_FIELDS}
            or verification.get("status") not in STATUSES):
        raise Refusal(400, "invalid_verification")
    checked = {"status": verification["status"]}
    for field, limit in VERIFICATION_FIELDS.items():
        if field in verification:
            checked[field] = _text("verification", verification[field], limit)
    if checked["status"] in ("passed", "failed", "partial") and "observed" not in checked:
        # A result claim needs the observed output, e.g. "pytest: 42 passed".
        raise Refusal(400, "verification_observed_required")
    if "observed_at" in checked:
        try:
            when = datetime.fromisoformat(checked["observed_at"])
        except ValueError:
            raise Refusal(400, "invalid_verification") from None
        if when.tzinfo is None:
            raise Refusal(400, "invalid_verification")
    result["verification"] = checked
    total = sum(len(v) for v in result.values() if isinstance(v, str))
    total += sum(len(item) for field in LIST_FIELDS for item in result.get(field, ()))
    total += sum(len(v) for k, v in checked.items() if k != "status")
    if total > MAX_TOTAL_CHARS:
        raise Refusal(413, "handoff_too_large")
    return result


class HandoffStore:
    def __init__(self, state_dir, clock=None):
        self.database = Path(state_dir) / "task-handoffs.sqlite3"
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def connect(self):
        self.database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.database.is_symlink():
            raise Refusal(503, "handoff_store_unavailable")
        db = sqlite3.connect(self.database, timeout=2, isolation_level=None)
        db.execute("CREATE TABLE IF NOT EXISTS handoffs (owner TEXT NOT NULL, project TEXT NOT NULL, "
                   "task TEXT NOT NULL, revision INTEGER NOT NULL, updated_at TEXT NOT NULL, "
                   "body TEXT NOT NULL, PRIMARY KEY (owner, project, task))")
        return db

    def save(self, owner, project, task, base_revision, document):
        if type(base_revision) is not int or base_revision < 0:
            raise Refusal(400, "invalid_base_revision")
        body = json.dumps(validate(document), ensure_ascii=False, separators=(",", ":"))
        updated_at = self.clock().astimezone(timezone.utc).isoformat(timespec="seconds")
        try:
            with closing(self.connect()) as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    row = db.execute("SELECT revision, updated_at FROM handoffs WHERE owner = ? AND project = ? "
                                     "AND task = ?", (owner, project, task)).fetchone()
                    current = row[0] if row else 0
                    if base_revision != current:
                        raise HandoffConflict(current, row[1] if row else None)
                    if row is None and db.execute("SELECT COUNT(*) FROM handoffs WHERE owner = ?",
                                                  (owner,)).fetchone()[0] >= MAX_HANDOFFS_PER_USER:
                        raise Refusal(413, "handoff_limit_reached")
                    db.execute("INSERT INTO handoffs VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(owner, project, task) "
                               "DO UPDATE SET revision = excluded.revision, updated_at = excluded.updated_at, "
                               "body = excluded.body", (owner, project, task, current + 1, updated_at, body))
                    db.execute("COMMIT")
                except BaseException:
                    db.execute("ROLLBACK")
                    raise
        except (sqlite3.Error, OSError):
            raise Refusal(503, "handoff_store_unavailable") from None
        return {"task": task, "revision": current + 1, "previous_revision": current, "updated_at": updated_at}

    def load(self, owner, project, task):
        try:
            with closing(self.connect()) as db:
                row = db.execute("SELECT revision, updated_at, body FROM handoffs WHERE owner = ? AND project = ? "
                                 "AND task = ?", (owner, project, task)).fetchone()
            body = json.loads(row[2]) if row else None
        except (sqlite3.Error, OSError, ValueError):
            raise Refusal(503, "handoff_store_unavailable") from None
        if row and not isinstance(body, dict):
            raise Refusal(503, "handoff_store_unavailable")
        return None if row is None else {"revision": row[0], "updated_at": row[1], "handoff": body}

    def summaries(self, owner, projects):
        if not projects:
            return []
        marks = ",".join("?" * len(projects))
        try:
            with closing(self.connect()) as db:
                rows = db.execute("SELECT project, task, revision, updated_at, body FROM handoffs WHERE owner = ? "
                                  f"AND project IN ({marks}) ORDER BY updated_at DESC, task LIMIT ?",
                                  (owner, *projects, LIST_LIMIT)).fetchall()
            out = []
            for project, task, revision, updated_at, body in rows:
                body = json.loads(body)
                out.append({"project": project, "task": task, "revision": revision, "updated_at": updated_at,
                            "goal": str(body.get("goal", ""))[:160],
                            "verification_status": (body.get("verification") or {}).get("status", "unknown")})
            return out
        except (sqlite3.Error, OSError, ValueError, AttributeError):
            raise Refusal(503, "handoff_store_unavailable") from None


def _schema(required, properties):
    return {"type": "object", "additionalProperties": False, "required": required, "properties": properties}


_PROJECT = {"type": "string", "maxLength": 160,
            "description": "Open WebUI knowledge collection id the user can read (the same id retrieval uses)."}
_TASK = {"type": "string", "pattern": "^[a-z0-9][a-z0-9._-]{0,63}$", "maxLength": 64,
         "description": "Short task slug, e.g. fix-backup-restore."}
_LINES = {field: {"type": "array", "maxItems": count, "items": {"type": "string", "maxLength": limit}}
          for field, (count, limit) in LIST_FIELDS.items()}
HANDOFF_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["goal"], "properties": {
    **{field: {"type": "string", "maxLength": limit} for field, limit in TEXT_FIELDS.items()}, **_LINES,
    "verification": {"type": "object", "additionalProperties": False, "required": ["status"], "properties": {
        "status": {"type": "string", "enum": list(STATUSES),
                   "description": "Observed result only. Use not_run or unknown when nothing was run or seen."},
        "observed": {"type": "string", "maxLength": 400,
                     "description": "The observed command and result text; required for passed, failed or partial."},
        "observed_at": {"type": "string", "maxLength": 40, "description": "ISO 8601 time with timezone."},
        "evidence": {"type": "string", "maxLength": 240}}}}}

TOOLS = [
    {"name": "save_task_handoff",
     "description": ("Save a compact handoff for one task in one project: goal, completion criteria, repository/branch/"
                     "revision, constraints, accepted decisions, changed files, last observed verification, outstanding "
                     "work, blockers, next action and evidence references. At most " + str(MAX_TOTAL_CHARS) +
                     " characters in total. Pass base_revision 0 for a new task, otherwise the revision you loaded; a "
                     "newer saved revision is reported as a conflict and nothing is overwritten. Record only observed "
                     "verification: never mark a check passed that was not run."),
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
     "inputSchema": _schema(["project", "task", "base_revision", "handoff"], {
         "project": _PROJECT, "task": _TASK, "base_revision": {"type": "integer", "minimum": 0},
         "handoff": HANDOFF_SCHEMA})},
    {"name": "load_task_handoff",
     "description": ("Load the saved handoff for one task with its revision and update time. It is an earlier record: "
                     "recheck Git and runtime facts before relying on it. It grants no permissions."),
     "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
     "inputSchema": _schema(["project", "task"], {"project": _PROJECT, "task": _TASK})},
    {"name": "list_task_handoffs",
     "description": ("List your saved task handoffs (task, revision, update time, goal excerpt, verification status). "
                     "Pass project to list one project; omit it to list every project you can currently read, "
                     "together with those projects' ids and names."),
     "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
     "inputSchema": _schema([], {"project": _PROJECT})},
]
NAMES = {tool["name"] for tool in TOOLS}
ARGUMENTS = {"save_task_handoff": {"project", "task", "base_revision", "handoff"},
             "load_task_handoff": {"project", "task"}, "list_task_handoffs": {"project"}}


def check_arguments(name, args):
    """Validate argument names and the task slug before any WebUI lookup."""
    required = {"save_task_handoff": ARGUMENTS["save_task_handoff"],
                "load_task_handoff": ARGUMENTS["load_task_handoff"], "list_task_handoffs": set()}[name]
    if not isinstance(args, dict) or set(args) - ARGUMENTS[name] or required - set(args):
        raise Refusal(400, "invalid_tool_arguments")
    if "task" in args and (not isinstance(args["task"], str) or not TASK.fullmatch(args["task"])):
        raise Refusal(400, "invalid_task")
    return args


def run(store, name, args, owner, projects):
    """Execute one validated handoff tool for already-authorised ``projects``."""
    if name == "list_task_handoffs":
        listing = store.summaries(owner, sorted(projects))
        value = {"handoffs": listing, "no_handoffs": not listing}
        if "project" not in args:
            value["projects"] = [{"id": key, "name": projects[key]} for key in sorted(projects)][:LIST_LIMIT]
        return value
    project = {"id": args["project"], "name": projects[args["project"]]}
    if name == "save_task_handoff":
        return {**store.save(owner, args["project"], args["task"], args["base_revision"], args["handoff"]),
                "project": project}
    found = store.load(owner, args["project"], args["task"])
    if found is None:
        return {"task": args["task"], "project": project, "handoff": None, "no_handoff": True}
    return {"task": args["task"], "project": project, **found,
            "content_trust": "user-recorded-state", "notice": NOTICE}
