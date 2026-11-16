#!/usr/bin/env python3
"""Elastic KV trial report: hand-backs (collect.sh's events.log) against SGLang's cache counters in Prometheus.

A "large recompute" is a request that had to prefill more than --threshold uncached prompt tokens. That is what a
returning conversation looks like when its cache was dropped, but a pasted document or a long fetched page looks the
same, so the report compares the rate in the window after hand-backs that dropped cache with the rate at other times.

Usage: kv_trial_report.py [--since 2026-11-14T18:42:00Z] [--window-min 30] [--threshold 10000]
                          [--prometheus URL] [--dir DIR]

--since defaults to the first event in the log. --prometheus defaults to the site value PROMETHEUS_URL (the
environment, then the site file; tools/README.md). --dir defaults to ~/ai-stack/kv-trial; the report is saved there.
"""
import argparse
import datetime as dt
import json
import re
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

SHRANK = re.compile(r"^(\S+) .*Elastic KV tail: shrank in ([\d.]+) s; released ([\d.]+) GiB, "
                    r"moved (\d+) tokens to the base, evicted (\d+)")
GREW = re.compile(r"^(\S+) .*Elastic KV tail: grew by (\d+) tokens")
TAIL = re.compile(r"^(\S+) .*Elastic KV tail: (.*)$")
ROUTINE = re.compile(r"self-test passed|buffers, base|running requests|on\. Base")
BUCKETS = (1000, 2000, 5000, 10000, 20000, 40000, 100000)
KEYS = ("requests", "large", "recomputed", "prompt", "cached")


TOOLS_LIB = Path(__file__).resolve().parents[1] / "lib" / "tools.sh"


def site_value(name):
    """NAME from the environment, else from the site file, through the tools' own loader
    (tools/lib/tools.sh: ownership and mode checks, allowlisted keys, values never evaluated)."""
    script = 'source "$1"; tools::load_site; printf "%s" "${!2:-}"'
    result = subprocess.run(["bash", "-c", script, "site", str(TOOLS_LIB), name],
                            capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or f"STOP: the site loader failed ({result.returncode})")
    return result.stdout or None


def when(stamp):
    """kubectl --timestamps value (RFC 3339, nanoseconds, Z) -> aware datetime."""
    return dt.datetime.fromisoformat(stamp[:26] + "+00:00")


def parse_events(lines, since):
    """Shrinks (dicts), the number of grows, and other non-routine tail lines at or after since."""
    shrinks, grows, other = [], 0, []
    for line in lines:
        if (m := SHRANK.match(line)) and when(m[1]) >= since:
            shrinks.append({"at": when(m[1]), "seconds": float(m[2]), "gib": float(m[3]),
                            "moved": int(m[4]), "evicted": int(m[5])})
        elif (m := GREW.match(line)) and when(m[1]) >= since:
            grows += 1
        elif (m := TAIL.match(line)) and when(m[1]) >= since and not ROUTINE.search(m[2]):
            other.append(f"{m[1][:19]} {m[2][:160]}")
    return shrinks, grows, other


def first_event(lines):
    for line in lines:
        if m := TAIL.match(line):
            return when(m[1])
    return None


def merge(spans):
    out = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def windows(shrinks, since, now, window):
    """Spans within window after a hand-back that dropped cache, and the rest of [since, now]."""
    exposed = merge([[s["at"], min(s["at"] + window, now)] for s in shrinks if s["evicted"] > 0])
    rest, cursor = [], since
    for a, b in exposed:
        if a > cursor:
            rest.append([cursor, a])
        cursor = max(cursor, b)
    if cursor < now:
        rest.append([cursor, now])
    return exposed, rest


def per100(totals):
    return 100 * totals["large"] / totals["requests"] if totals["requests"] >= 1 else None


def prom(base, expr, at):
    url = f"{base.rstrip('/')}/api/v1/query?" + urllib.parse.urlencode({"query": expr, "time": f"{at.timestamp():.3f}"})
    with urllib.request.urlopen(url, timeout=30) as reply:
        data = json.load(reply)
    if data.get("status") != "success":
        raise SystemExit(f"prometheus: {data.get('error', data)}")
    return sum(float(x["value"][1]) for x in data["data"]["result"])


def counts(base, a, b, threshold):
    """Requests, large recomputes, recomputed tokens, prompt tokens and cached tokens between a and b."""
    span = max(int((b - a).total_seconds()), 60)

    def inc(metric):
        return prom(base, f"sum(increase({metric}[{span}s]))", b)

    req = inc("sglang:uncached_prompt_tokens_histogram_count")
    small = inc(f'sglang:uncached_prompt_tokens_histogram_bucket{{le="{threshold}.0"}}')
    return {"requests": req, "large": max(req - small, 0.0),
            "recomputed": inc("sglang:uncached_prompt_tokens_histogram_sum"),
            "prompt": inc("sglang:prompt_tokens_total"), "cached": inc("sglang:cached_tokens_total")}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--since")
    ap.add_argument("--window-min", type=int, default=30)
    ap.add_argument("--threshold", type=int, default=10000, choices=BUCKETS)
    ap.add_argument("--prometheus")
    ap.add_argument("--dir", type=Path, default=Path.home() / "ai-stack" / "kv-trial")
    args = ap.parse_args()
    base = args.prometheus or site_value("PROMETHEUS_URL")
    if not base:
        raise SystemExit("STOP: set PROMETHEUS_URL in the environment or the site file (tools/README.md), "
                         "or pass --prometheus")
    lines = (args.dir / "events.log").read_text().splitlines()
    since = dt.datetime.fromisoformat(args.since) if args.since else first_event(lines)
    if since is None:
        raise SystemExit("STOP: no events in the log; pass --since")
    now = dt.datetime.now(dt.UTC)
    shrinks, grows, other = parse_events(lines, since)
    exposed, rest = windows(shrinks, since, now, dt.timedelta(minutes=args.window_min))

    def total(spans):
        acc = dict.fromkeys(KEYS, 0.0)
        for a, b in spans:
            for k, v in counts(base, a, b, args.threshold).items():
                acc[k] += v
        return acc

    whole = counts(base, since, now, args.threshold)
    after, elsewhere = total(exposed), total(rest)
    report = {
        "since": since.isoformat(), "until": now.isoformat(), "window_min": args.window_min,
        "threshold_tokens": args.threshold, "grows": grows, "hand_backs": len(shrinks),
        "hand_backs_dropping_cache": sum(1 for s in shrinks if s["evicted"] > 0),
        "tokens_moved": sum(s["moved"] for s in shrinks), "tokens_dropped": sum(s["evicted"] for s in shrinks),
        "slowest_shrink_s": max((s["seconds"] for s in shrinks), default=None),
        "cache_hit_pct": round(100 * whole["cached"] / whole["prompt"], 1) if whole["prompt"] else None,
        "after_hand_backs": {k: round(v) for k, v in after.items()} | {"large_per_100": per100(after)},
        "other_times": {k: round(v) for k, v in elsewhere.items()} | {"large_per_100": per100(elsewhere)},
        "other_tail_lines": other[-20:],
    }
    out = args.dir / f"report-{now:%Y%m%dT%H%MZ}.json"
    out.write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report, indent=2, default=str))
    print(f"saved {out}")


if __name__ == "__main__":
    main()
