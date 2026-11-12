"""Bounded Prometheus metrics for the assistant tools (service-tools /metrics).

Every family declares its labels and the only values each label may take. A value
outside that set is recorded as "other", so request text, document passages, URLs,
user/project/session identifiers or secrets can never become a label. Metric
values count tool outcomes only: a successful tool call is not evidence that the
user's task succeeded.
"""
import re
import threading

# Shared label vocabularies.
CLIENTS = ("mcp", "webui")
OUTCOMES_RETRIEVAL = ("evidence", "no_evidence", "error", "denied", "unavailable", "invalid")
OUTCOMES_WEB = ("ok", "no_results", "provider_error", "quota", "invalid", "refused", "unavailable")
OUTCOMES_STORE = ("ok", "conflict", "not_found", "denied", "invalid", "too_large", "error", "unavailable")
DURATION_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0)
COUNT_BUCKETS = (0, 1, 2, 3, 5, 8, 12, 20, 30, 50)

# name -> (type, help, {label: allowed values})
FAMILIES = {
    "workstation_assistant_retrieval_requests_total": (
        "counter", "Retrieval tool calls by client and outcome (no_evidence = searched, nothing authorised and relevant).",
        {"client": CLIENTS, "outcome": OUTCOMES_RETRIEVAL}),
    "workstation_assistant_retrieval_duration_seconds": (
        "histogram", "Retrieval tool latency, including reranking and permitted expansion.", {"client": CLIENTS}),
    "workstation_assistant_retrieval_candidates": (
        "histogram", "Candidate passages returned by the retrieval backend before final selection.", {"client": CLIENTS}),
    "workstation_assistant_retrieval_results": (
        "histogram", "Passages returned to the client after permission checks and final selection.", {"client": CLIENTS}),
    "workstation_assistant_source_reads_total": (
        "counter", "Exact source excerpt reads (read_project_source) by client and outcome.",
        {"client": CLIENTS, "outcome": OUTCOMES_RETRIEVAL}),
    "workstation_assistant_reranker_state": (
        "gauge", "Configured reranker state (1 for the current state).",
        {"state": ("active", "disabled", "missing_assets", "incompatible", "unknown")}),
    "workstation_assistant_reranker_fallback_total": (
        "counter", "Retrieval answered without the configured reranker, by reason.",
        {"reason": ("disabled_by_profile", "unavailable", "error")}),
    "workstation_assistant_web_requests_total": (
        "counter", "Web tool calls by tool and outcome (no_results is a successful search with no evidence).",
        {"tool": ("search", "read", "read_continue"), "client": CLIENTS, "outcome": OUTCOMES_WEB}),
    "workstation_assistant_handoff_operations_total": (
        "counter", "Task handoff operations by outcome (conflict = a newer revision exists).",
        {"op": ("save", "load", "list"), "client": CLIENTS, "outcome": OUTCOMES_STORE}),
    "workstation_assistant_memory_operations_total": (
        "counter", "Memory tool operations by outcome.",
        {"op": ("recall", "remember", "forget", "inspect"), "client": CLIENTS, "outcome": OUTCOMES_STORE}),
    "workstation_assistant_video_requests_total": (
        "counter", "Video tool calls (generate_video) by outcome.",
        {"client": CLIENTS, "outcome": ("ok", "denied", "busy", "not_ready", "cancelled", "failed", "invalid", "unavailable")}),
    "workstation_assistant_serving_contract_info": (
        "gauge", "Serving contract the clients were rendered for (value 1).",
        {"contract": None, "status": ("active", "provisional", "incomplete")}),
}
# Labels whose values come from deployment configuration, not requests: bounded by format.
CONFIG_VALUE = re.compile(r"[A-Za-z0-9._:-]{1,64}\Z")


def _escape(value):
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class Metrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.counters = {}
        self.gauges = {}
        self.histograms = {}
        self._seed()

    def _seed(self):
        """Publish every counter and histogram series at zero from start-up.

        Every label value is enumerated, so the full set is small and fixed (~250
        series). Without it a family has no series until its first event: dashboards
        show "No data" for a tool nobody has used since the pod started, and rate()
        loses the first event of a series that appears from nothing.
        """
        for name, (kind, _, allowed) in FAMILIES.items():
            if kind == "gauge":
                continue  # current-state gauges: set when the state is known
            names = sorted(allowed)
            combos = [()]
            for label in names:
                combos = [c + ((label, value),) for c in combos for value in allowed[label]]
            for key in combos:
                if kind == "counter":
                    self.counters[(name, key)] = 0
                else:
                    buckets = DURATION_BUCKETS if name.endswith("_seconds") else COUNT_BUCKETS
                    self.histograms[(name, key)] = {"buckets": [0] * len(buckets), "sum": 0.0, "count": 0}

    def _key(self, name, labels):
        kind, _, allowed = FAMILIES[name]
        if set(labels) != set(allowed):
            raise ValueError(f"{name} takes labels {sorted(allowed)}")
        out = []
        for label in sorted(allowed):
            value = str(labels[label])
            values = allowed[label]
            if values is None:
                value = value if CONFIG_VALUE.match(value) else "other"
            elif value not in values:
                value = "other"
            out.append((label, value))
        return kind, tuple(out)

    def inc(self, name, value=1, **labels):
        kind, key = self._key(name, labels)
        assert kind == "counter", name
        with self.lock:
            self.counters[(name, key)] = self.counters.get((name, key), 0) + value

    def set_state(self, name, **labels):
        """Gauge with one current series per family (other series drop to 0)."""
        kind, key = self._key(name, labels)
        assert kind == "gauge", name
        with self.lock:
            for existing in [k for k in self.gauges if k[0] == name]:
                self.gauges[existing] = 0
            self.gauges[(name, key)] = 1

    def observe(self, name, value, **labels):
        kind, key = self._key(name, labels)
        assert kind == "histogram", name
        buckets = DURATION_BUCKETS if name.endswith("_seconds") else COUNT_BUCKETS
        with self.lock:
            entry = self.histograms.setdefault((name, key), {"buckets": [0] * len(buckets), "sum": 0.0, "count": 0})
            for i, bound in enumerate(buckets):
                if value <= bound:
                    entry["buckets"][i] += 1
            entry["sum"] += value
            entry["count"] += 1

    def render(self):
        lines = []
        with self.lock:
            for name, (kind, help_text, _) in FAMILIES.items():
                lines += [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"]
                if kind == "counter":
                    series = [(k, v) for (n, k), v in self.counters.items() if n == name]
                elif kind == "gauge":
                    series = [(k, v) for (n, k), v in self.gauges.items() if n == name]
                else:
                    buckets = DURATION_BUCKETS if name.endswith("_seconds") else COUNT_BUCKETS
                    for (n, key), entry in self.histograms.items():
                        if n != name:
                            continue
                        base = ",".join(f'{k}="{_escape(v)}"' for k, v in key)
                        for bound, count in zip(buckets, entry["buckets"]):
                            lines.append(f'{name}_bucket{{{base},le="{bound:g}"}} {count}')
                        lines.append(f'{name}_bucket{{{base},le="+Inf"}} {entry["count"]}')
                        lines.append(f"{name}_sum{{{base}}} {entry['sum']:.6f}")
                        lines.append(f"{name}_count{{{base}}} {entry['count']}")
                    continue
                for key, value in series:
                    lines.append(name + "{" + ",".join(f'{k}="{_escape(v)}"' for k, v in key) + "} " + f"{value:g}")
        return "\n".join(lines) + "\n"


METRICS = Metrics()


def record_serving_contract(environ):
    """Publish the rendered serving contract identity, when the render set one."""
    contract, status = environ.get("SERVING_CONTRACT"), environ.get("SERVING_CONTRACT_STATUS")
    if contract and status:
        METRICS.set_state("workstation_assistant_serving_contract_info", contract=contract, status=status)
