"""Content-safe request-trace events for requests that carry a run context.

A measurement runner may send one X-Workstation-Run-Context header: the
run-record-v1 identity of the attempt (task, task version, run, attempt, arm,
causal parent and cause) and the digest of its run plan. For such a request the
gateway and service-tools write one fixed-schema JSON line to the container log
when the request ends. A request without the header writes nothing, so ordinary
traffic stays unlogged.

An event holds the validated identifiers, a fixed operation name, a fixed
result, the HTTP status, a fixed code and two timestamps. It never holds a
path, query, header, body, user, key or exception text. Validation bounds the
format and length of each identifier. It cannot recognise a secret that fits
that format, so a caller must never derive an identifier from secret or personal
data. Identifiers never become metric labels.

lib/workstation/run_record.py builds the header value and joins the events
(docs/RUN-RECORDS.md). The tests keep both parsers identical.
"""
import json
import os
import re
import time


HEADER = b"x-workstation-run-context"
MAX_CONTEXT = 1024
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
SHA256 = re.compile(r"[a-f0-9]{64}\Z")
TOKEN = re.compile(r"[a-z0-9][a-z0-9_:.-]{0,63}\Z")
FIELDS = (("task", "task_id"), ("task_version", "task_version"), ("run", "run_id"),
          ("attempt", "attempt_id"), ("arm", "arm_id"), ("parent", "parent_id"), ("cause", "cause"))
RESULTS = ("ok", "tool_error", "refused", "limited", "backend_error", "cancelled", "error")


def parse(value):
    """(identity, plan_sha256) from a header value; ValueError for anything else."""
    if isinstance(value, bytes):
        value = value.decode("ascii")
    parts = value.split(";") if len(value) <= MAX_CONTEXT else ()
    if len(parts) != len(FIELDS) + 2 or parts[0] != "v1":
        raise ValueError("unsupported run context")
    identity = {}
    for part, (name, key) in zip(parts[1:-1], FIELDS):
        label, _, item = part.partition("=")
        if label != name:
            raise ValueError("run context fields are missing or out of order")
        identity[key] = item
    label, _, plan = parts[-1].partition("=")
    parent = identity["parent_id"] = None if identity["parent_id"] == "-" else identity["parent_id"]
    if (label != "plan" or SHA256.match(plan) is None
            or any(ID.match(identity[key]) is None for key in ("task_id", "task_version", "run_id", "attempt_id", "arm_id"))
            or (parent is not None and ID.match(parent) is None) or identity["cause"] not in ("initial", "retry")
            or (identity["cause"] == "initial") != (parent is None) or parent == identity["attempt_id"]):
        raise ValueError("invalid run context")
    return identity, plan


def descriptor_writer(descriptor):
    """Write each event as one line to an inherited descriptor (the container log)."""
    def write(record):
        try:
            # One write below PIPE_BUF: lines from concurrent requests never interleave.
            os.write(descriptor, (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode())
        except OSError:
            pass  # A lost event never fails the request it describes.
    return write


class Trace:
    """One identified request; finish() writes its event once."""

    def __init__(self, events, operation, identity, plan_sha256):
        self.events, self.operation = events, operation
        self.identity, self.plan_sha256 = identity, plan_sha256
        self.started = events.clock()
        self.finished = False

    def finish(self, result, status=None, code=None, operation=None):
        if self.finished:
            return
        self.finished = True
        operation = operation or self.operation
        if self.events.write is not None:
            self.events.write({
                "schema": 1, "kind": "request-trace", "interface": "run-record-v1", "service": self.events.service,
                "operation": operation if isinstance(operation, str) and TOKEN.match(operation) else "other",
                "identity": self.identity, "plan_sha256": self.plan_sha256,
                "started_unix_ns": self.started, "ended_unix_ns": max(self.started, self.events.clock()),
                "result": result if result in RESULTS else "error",
                "status": status if type(status) is int and 100 <= status <= 599 else None,
                "code": code if isinstance(code, str) and TOKEN.match(code) else None})


class Untraced:
    """A request without a run context: nothing is recorded."""

    def finish(self, result, status=None, code=None, operation=None):
        pass


UNTRACED = Untraced()


class Events:
    """Request-trace events of one service; ``write`` receives each event (None: validate only)."""

    def __init__(self, service, write=None, clock=time.time_ns):
        self.service, self.write, self.clock = service, write, clock

    def begin(self, operation, headers):
        """Trace for a request with a run context, UNTRACED without one; ValueError when it is malformed."""
        value = headers.get(HEADER)
        return UNTRACED if value is None else Trace(self, operation, *parse(value))
