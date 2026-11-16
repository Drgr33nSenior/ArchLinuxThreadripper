"""Resource and SGLang checks for rollout-main.sh: a plan-only render against the live Deployments.

Usage: rollout_check.py WORKLOADS_YAML LIVE_DEPLOYMENTS_JSON LIVE_SGLANG_IMAGE

Reads local files only. Exits 1 when
  - a Deployment's container or init container sizing (requests, limits) would change, unless
    EXPECT_CHANGED allows exactly that change: one container's memory request and limit set to the
    reviewed value, everything else (CPU, other resources, other containers) as live; or
    EXPECT_CHANGED lists a change the render does not make;
  - the rendered SGLang image differs from LIVE_SGLANG_IMAGE;
  - the rendered SGLang environment differs from the live one, unless EXPECT_SGLANG_ENV names every
    changed variable, or EXPECT_SGLANG_ENV names a variable the render does not change.

EXPECT_CHANGED="deployment=container:memory ..."   reviewed memory changes (init containers: init:NAME)
EXPECT_SGLANG_ENV="NAME ..."                       reviewed SGLang environment changes
"""
import copy
import json
import os
import sys

import yaml


def sizing(dep):
    """{container: {requests, limits}} for the containers and (prefixed init:) the init containers."""
    spec = dep["spec"]["template"]["spec"]
    out = {}
    for prefix, key in (("", "containers"), ("init:", "initContainers")):
        for c in spec.get(key) or []:
            out[prefix + c["name"]] = {k: (c.get("resources") or {}).get(k) for k in ("requests", "limits")}
    return out


def parse_expected(text):
    """EXPECT_CHANGED text -> {deployment: (container, memory)}. The memory follows the last ":",
    so an init container is named init:NAME (sglang=init:prepare:5Gi)."""
    expected = {}
    for item in text.split():
        name, sep, rest = item.partition("=")
        container, colon, memory = rest.rpartition(":")
        if not (name and sep and container and colon and memory):
            raise ValueError(f"EXPECT_CHANGED item {item!r} is not deployment=container:memory")
        expected[name] = (container, memory)
    return expected


def env_value(entry):
    """One env entry with the API server's defaults filled, so a live object compares equal to its render."""
    if "valueFrom" not in entry:
        return entry.get("value") or ""
    source = copy.deepcopy(entry["valueFrom"])
    if "fieldRef" in source:
        source["fieldRef"].setdefault("apiVersion", "v1")
    if "resourceFieldRef" in source:
        source["resourceFieldRef"].setdefault("divisor", "0")
    return json.dumps(source, sort_keys=True)


def environment(container):
    return {e["name"]: env_value(e) for e in container.get("env") or []}


def check(rendered, live, live_image, expected, expected_env):
    """rendered: Deployments from the render; live: the live Deployment list. Returns (lines, ok)."""
    docs = {d["metadata"]["name"]: d for d in rendered}
    live_by_name = {d["metadata"]["name"]: d for d in live}
    lines = []
    live_sizing = {n: sizing(d) for n, d in live_by_name.items()}
    changed = [n for n, d in docs.items() if n in live_sizing and sizing(d) != live_sizing[n]]
    for n in changed:
        lines.append(f"  {n}: live {live_sizing[n]} -> rendered {sizing(docs[n])}")

    def as_expected(n):
        # Live sizing with only the reviewed container's memory request and limit replaced.
        container, memory = expected[n]
        want = copy.deepcopy(live_sizing[n])
        if container not in want:
            return False
        for k in ("requests", "limits"):
            want[container][k] = {**(want[container][k] or {}), "memory": memory}
        return sizing(docs[n]) == want

    bad = [n for n in changed if n not in expected or not as_expected(n)]
    missing = [n for n in expected if n not in changed]
    lines.append(f"resource check: {'unchanged' if not changed else f'changed {changed}'} | unexpected: "
                 f"{bad or 'none'} | expected but not changed: {missing or 'none'}")

    if "sglang" not in docs:
        lines.append("sglang: not in the render")
        return lines, False
    rendered_container = docs["sglang"]["spec"]["template"]["spec"]["containers"][0]
    image_ok = rendered_container["image"] == live_image
    env_ok, env_note = True, "no live sglang Deployment"
    if "sglang" in live_by_name:
        old = environment(live_by_name["sglang"]["spec"]["template"]["spec"]["containers"][0])
        new = environment(rendered_container)
        env_changed = sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))
        env_bad = [k for k in env_changed if k not in expected_env]
        env_missing = [k for k in expected_env if k not in env_changed]
        for k in env_changed:
            lines.append(f"  sglang env {k}: {old.get(k)!r} -> {new.get(k)!r}")
        env_ok = not env_bad and not env_missing
        env_note = (f"changed {env_changed or 'none'} | unexpected: {env_bad or 'none'}"
                    f" | expected but not changed: {env_missing or 'none'}")
    lines.append(f"sglang image unchanged: {image_ok} | sglang env: {env_note}")
    return lines, not bad and not missing and image_ok and env_ok


def main(argv):
    if len(argv) != 4 or argv[1] in ("-h", "--help"):
        print(__doc__.strip())
        return 0 if len(argv) > 1 and argv[1] in ("-h", "--help") else 2
    with open(argv[1]) as handle:
        rendered = [d for d in yaml.safe_load_all(handle) if d and d.get("kind") == "Deployment"]
    with open(argv[2]) as handle:
        live = json.load(handle)["items"]
    try:
        expected = parse_expected(os.environ.get("EXPECT_CHANGED", ""))
    except ValueError as error:
        print(f"STOP: {error}", file=sys.stderr)
        return 1
    lines, ok = check(rendered, live, argv[3], expected, os.environ.get("EXPECT_SGLANG_ENV", "").split())
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
