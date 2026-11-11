#!/usr/bin/env python3
"""Open WebUI model presets for the serving contract: a reasoning choice in the model menu.

`service render` writes webui-presets.json from the contract. The base model keeps the
contract's request defaults (thinking at its default effort); the presets add:
  Think  thinking on at the template's highest accepted effort
  Fast   thinking off
Open WebUI stores presets in its database, so `apply` upserts them through the admin
import endpoint (/api/v1/models/import, Open WebUI 0.12.5) and grants read access to the
named groups only. The admin API key is read from a file and never printed.

Usage: webui_presets.py apply PRESETS.json --url https://chat.example --key-file FILE --group NAME...
"""
import argparse
import json
import re
import ssl
import sys
import urllib.request
from pathlib import Path

PRESET_ID = re.compile(r"[a-z0-9][a-z0-9.-]{0,63}\Z")


def presets(contract):
    """The presets for one contract, or [] when its reasoning values are unverified."""
    defaults = contract["reasoning"]["request_defaults"]
    if not defaults:
        return []
    alias = contract["model"]["alias"]
    efforts = contract["chat_template"]["reasoning_efforts"]
    deepest = "xhigh" if "xhigh" in efforts else efforts[-1]
    stem = re.sub(r"[^a-z0-9.]+", "-", alias.lower()).strip("-")
    rows = [
        (f"{stem}-think", "Think", f"Thinks carefully before answering (reasoning effort {deepest}). Slower; "
         "for hard questions, planning and code.",
         {"reasoning_effort": deepest, "custom_params": {"chat_template_kwargs": {"enable_thinking": True}}}),
        (f"{stem}-fast", "Fast", "Answers straight away without a thinking phase. Quickest; for simple questions.",
         {"custom_params": {"chat_template_kwargs": {"enable_thinking": False}}}),
    ]
    out = []
    for preset_id, label, description, params in rows:
        if not PRESET_ID.fullmatch(preset_id):
            raise ValueError(f"preset id {preset_id!r} is not a bounded identifier")
        out.append({"id": preset_id, "base_model_id": alias, "name": f"{alias} · {label}",
                    "meta": {"description": description, "tags": [{"name": "reasoning"}]},
                    "params": params, "is_active": True})
    return out


def _request(method, url, key, body=None, context=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method=method,
                                     headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30, context=context) as response:
        return json.loads(response.read(4 << 20) or b"null")


def apply(rows, url, key, groups, context=None):
    """Upsert the presets with read access for the named groups only; returns the ids applied."""
    if not rows:
        raise ValueError("no presets: the contract's reasoning values are unverified")
    if not groups:
        raise ValueError("name at least one --group; presets are never public")
    base = url.rstrip("/")
    listed = _request("GET", base + "/api/v1/groups/", key, context=context)
    by_name = {g.get("name"): g.get("id") for g in (listed if isinstance(listed, list) else [])}
    missing = [name for name in groups if name not in by_name]
    if missing:
        raise ValueError(f"unknown Open WebUI group(s): {missing}")
    grants = [{"principal_type": "group", "principal_id": by_name[name], "permission": "read"} for name in groups]
    models = [dict(row, access_grants=grants) for row in rows]
    if _request("POST", base + "/api/v1/models/import", key, {"models": models}, context=context) is not True:
        raise ValueError("Open WebUI refused the preset import")
    return [row["id"] for row in rows]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("apply", help="upsert rendered presets into Open WebUI")
    run.add_argument("presets", type=Path)
    run.add_argument("--url", required=True, help="Open WebUI base URL, e.g. https://chat.lan.example")
    run.add_argument("--key-file", required=True, type=Path, help="file holding an admin API key (0600)")
    run.add_argument("--group", action="append", default=[], help="group granted read access (repeatable)")
    args = parser.parse_args(argv)
    if not args.url.startswith("https://"):
        parser.error("--url must be https")
    if args.key_file.stat().st_mode & 0o077:
        parser.error("--key-file must not be readable by group or others (chmod 600)")
    key = args.key_file.read_text().strip()
    if not key or any(c.isspace() for c in key):
        parser.error("--key-file must hold one API key")
    rows = json.loads(args.presets.read_text())
    applied = apply(rows, args.url, key, args.group, context=ssl.create_default_context())
    print("applied presets: " + ", ".join(applied) + "; read access: " + ", ".join(args.group))


if __name__ == "__main__":
    sys.exit(main())
