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
Prints one JSON line: counts and setting names, no values.
"""
import ast
import asyncio
import json
import os
import sys

APP = "/app/backend"


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
    print(json.dumps({"pinned": len(keys), "settings": keys,
                      "env_only": sorted(names - used)}))  # read by Open WebUI directly, never stored
    return 0


if __name__ == "__main__":
    sys.exit(main())
