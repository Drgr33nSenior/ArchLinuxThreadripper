"""Re-apply the Open WebUI settings this deployment sets, before Open WebUI starts.

Open WebUI runs with persistent config: settings changed in the Admin Panel are kept in its
database across restarts, and the environment only seeds a fresh database. The settings our
deployment sets explicitly (connections and keys, sign-up, default role and permissions, the
reviewed RAG profile, model defaults) stay owned by the deployment: on every start this writes
their environment values over the database, so an Admin Panel change to one of them lasts
until the next deployment, and a rotated key or changed default reaches Open WebUI.

WEBUI_PINNED_ENV lists the names (never values) of the environment variables the deployment
sets; lib/workstation/service.py renders it. Which settings those variables feed is read from
the installed Open WebUI's own config.py, so the mapping follows Open WebUI upgrades.

The tool-use system prompt (system-prompt.md beside this script) is owned the same way. Open
WebUI has no global system prompt: a `system` entry in DEFAULT_MODEL_PARAMS is dropped before
the request is built, and only a stored model record's `params.system` is applied on the
server to every request for that model (browser chats, API clients, background tasks). So the
prompt is written to the records of the models named in DEFAULT_MODELS. Only that one field of
an existing record changes; a missing record is reported, never created, because creating one
would decide who may use the model.

Two more fields are owned the same way, from assistant.env:

- MODEL_BUILTIN_TOOLS_OFF names the built-in tool categories a chat with those models does
  not carry (the record's `meta.builtinTools`; Open WebUI offers every category that is not
  switched off there). The features themselves stay available in the interface.
- WEB_SEARCH_WITHOUT_CONFIRMATION_GROUPS names the groups whose members search the web
  without the confirmation dialog (the fork's `features.web_search_without_confirmation`
  group permission). Every other group has the permission removed, so the grant follows
  this list. Unlike the prompt this is a permission: a failure to write it stops the start.

Prints one JSON line: counts and setting, model, category and group names, no values.
"""
import ast
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

APP = "/app/backend"
SYSTEM_PROMPT = Path(__file__).with_name("system-prompt.md")
CONFIRMATION_PERMISSION = "web_search_without_confirmation"


def listed(name):
    """The comma-separated names in an environment variable, in order, without repeats."""
    return list(dict.fromkeys(item.strip() for item in os.environ.get(name, "").split(",") if item.strip()))


def setting_sources(source):
    """Map each DEFAULT_CONFIG setting to the environment variables its value is computed from.

    Follows module-level assignments in config.py, including reassignment chains
    (A = getenv('A'); B = A or ...), so a setting depends on every variable that can reach it.
    """
    tree = ast.parse(source)
    reads, uses = {}, {}

    def env_reads(node):
        found = set()
        for n in ast.walk(node):
            if isinstance(n, ast.Call) and n.args and isinstance(n.args[0], ast.Constant) \
                    and isinstance(n.args[0].value, str):
                func = n.func
                if (isinstance(func, ast.Attribute) and func.attr == "getenv") or \
                        (isinstance(func, ast.Attribute) and func.attr == "get"
                         and isinstance(func.value, ast.Attribute) and func.value.attr == "environ"):
                    found.add(n.args[0].value)
            elif isinstance(n, ast.Subscript) and isinstance(n.value, ast.Attribute) \
                    and n.value.attr == "environ" and isinstance(n.slice, ast.Constant):
                found.add(n.slice.value)
        return found

    def names_in(node):
        return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for name in names_in(target):
                    reads.setdefault(name, set()).update(env_reads(node.value))
                    uses.setdefault(name, set()).update(names_in(node.value) - {name})

    def closure(name, seen):
        if name in seen:
            return set()
        seen.add(name)
        found = set(reads.get(name, ()))
        for used in uses.get(name, ()):
            found |= closure(used, seen)
        return found

    default = next(n.value for n in tree.body if isinstance(n, ast.Assign)
                   and any(getattr(t, "id", None) == "DEFAULT_CONFIG" for t in n.targets))
    sources = {}
    for key, value in zip(default.keys, default.values):
        found = set()
        for name in names_in(value):
            found |= closure(name, set())
        found |= env_reads(value)
        sources[key.value] = found
    return sources


def system_prompt_change(params, prompt):
    """The record's params with the deployment's prompt, or None when it is already there."""
    params = dict(params or {})
    if (params.get("system") or "") == prompt:
        return None
    return {**params, "system": prompt}


def builtin_tools_change(meta, off):
    """The record's meta with exactly these built-in tool categories switched off, or None
    when it already says so. A category that is not listed is on (Open WebUI's default)."""
    meta = dict(meta or {})
    wanted = {category: False for category in off}
    if (meta.get("builtinTools") or {}) == wanted:
        return None
    if wanted:
        return {**meta, "builtinTools": wanted}
    meta.pop("builtinTools", None)
    return meta


def builtin_tool_categories(source):
    """The built-in tool categories the installed Open WebUI checks on a model record."""
    return set(re.findall(r"is_builtin_tool_enabled\(\s*'([a-z_]+)'", source))


async def pin_model_records(model_ids, prompt, builtin_off):
    """Write the prompt and the built-in tool categories to each named model record that
    exists. Returns (prompt result, tool result) per model id."""
    from open_webui.internal.db import get_async_db
    from open_webui.models.models import Model

    results = {}
    async with get_async_db() as session:
        for model_id in model_ids:
            record = await session.get(Model, model_id)
            if record is None:
                results[model_id] = ("no model record", "no model record")
                continue
            params = system_prompt_change(record.params, prompt) if prompt else None
            meta = builtin_tools_change(record.meta, builtin_off)
            if params is not None:
                record.params = params
            if meta is not None:
                record.meta = meta
            if params is not None or meta is not None:
                record.updated_at = int(time.time())
            results[model_id] = ("unchanged" if params is None else "updated",
                                 "unchanged" if meta is None else "updated")
        await session.commit()
    return results


def model_record_report():
    """Pin the prompt and the built-in tool categories and describe the outcome as
    (system_prompt, builtin_tools); a failure is reported, never fatal.

    Both are guidance for the model, not permissions, so Open WebUI still starts when
    they cannot be written (unlike the settings above, which fail closed)."""
    try:
        prompt = SYSTEM_PROMPT.read_text().strip()
    except OSError:
        prompt = None
    model_ids, off = listed("DEFAULT_MODELS"), listed("MODEL_BUILTIN_TOOLS_OFF")
    if not model_ids:
        skipped = {"skipped": "DEFAULT_MODELS names no model"}
        return skipped, skipped
    try:
        results = asyncio.run(pin_model_records(model_ids, prompt, off))
    except Exception as error:  # noqa: BLE001 - any database or schema fault is reported, not raised
        return {"error": type(error).__name__}, {"error": type(error).__name__}
    if prompt:
        prompt_report = {"characters": len(prompt), "models": {m: r[0] for m, r in results.items()}}
    else:
        prompt_report = {"error": "system-prompt.md missing"} if prompt is None else {"skipped": "no prompt text"}
    tools_report = {"off": off, "models": {m: r[1] for m, r in results.items()}}
    try:
        known = builtin_tool_categories(Path(APP, "open_webui/utils/tools.py").read_text())
        unknown = sorted(set(off) - known)
        if unknown:  # renamed or removed upstream: the category is no longer switched off
            tools_report["unknown"] = unknown
    except OSError:
        pass
    return prompt_report, tools_report


def confirmation_change(permissions, granted):
    """The group's permissions with the confirmation exemption set, or None when they
    already say so. A group that never had the permission and is not granted it is left alone."""
    permissions = dict(permissions or {})
    features = dict(permissions.get("features") or {})
    if bool(features.get(CONFIRMATION_PERMISSION)) == granted:
        return None
    return {**permissions, "features": {**features, CONFIRMATION_PERMISSION: granted}}


async def pin_confirmation_groups(names):
    """Grant the exemption to the named groups and remove it from every other group."""
    from open_webui.internal.db import get_async_db
    from open_webui.models.groups import Group
    from sqlalchemy import select

    results = {name: "no group" for name in names}
    async with get_async_db() as session:
        for group in (await session.execute(select(Group))).scalars():
            granted = group.name in results
            permissions = confirmation_change(group.permissions, granted)
            if permissions is not None:
                group.permissions, group.updated_at = permissions, int(time.time())
            if granted:
                results[group.name] = "unchanged" if permissions is None else "granted"
            elif permissions is not None:
                results[group.name] = "removed"
        await session.commit()
    return results


def main():
    if os.environ.get("ENABLE_PERSISTENT_CONFIG", "true").lower() != "true":
        print(json.dumps({"pinned": 0, "reason": "persistent config off: the environment is used directly"}))
        return 0
    names = {n for n in os.environ.get("WEBUI_PINNED_ENV", "").split(",") if n}
    if not names:
        # Fail closed: without the list every setting would follow the database.
        print(json.dumps({"error": "WEBUI_PINNED_ENV missing; render with lib/workstation/service.py"}))
        return 1
    sys.path.insert(0, APP)
    from open_webui import config  # runs Open WebUI's migrations; DEFAULT_CONFIG from this environment
    from open_webui.models.config import DICT_CONFIG_KEY_ALIASES, Config

    sources = setting_sources(open(config.__file__).read())
    for key, aliases in DICT_CONFIG_KEY_ALIASES.items():
        sources.setdefault(key, set()).update(aliases)
    keys = sorted(key for key, envs in sources.items() if envs & names and key in config.DEFAULT_CONFIG)
    asyncio.run(Config.upsert({key: config.DEFAULT_CONFIG[key] for key in keys}))
    used = set().union(*(sources[key] for key in keys)) & names if keys else set()
    report = {"pinned": len(keys), "settings": keys,
              "env_only": sorted(names - used)}  # read by Open WebUI or this script directly, never stored
    try:
        report["web_search_without_confirmation"] = asyncio.run(
            pin_confirmation_groups(listed("WEB_SEARCH_WITHOUT_CONFIRMATION_GROUPS")))
    except Exception as error:  # noqa: BLE001 - a permission that cannot be written stops the start
        print(json.dumps({**report, "error": "web search confirmation groups: " + type(error).__name__}))
        return 1
    report["system_prompt"], report["builtin_tools"] = model_record_report()
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
