"""Authenticate the two installed decorated scheduler methods without imports.

The owner supplies source descriptors from its pinned runtime manifest. Compiling
whole source files checks callable code; it does not execute those modules.
The existing stack guard remains responsible for other globals and modules.
"""
from pathlib import Path
from types import CodeType, FunctionType
import sys

from bundle_contract import checked_file, need

def _file(root, name, limit):
    return checked_file(root / name, limit)

def hex64(value):
    need(type(value) is str and len(value) == 64 and all(x in "0123456789abcdef" for x in value), "SHA256")
    return value

from bundle_contract import sha


def _codes(code):
    yield code
    for child in code.co_consts:
        if isinstance(child, CodeType):
            yield from _codes(child)


def _source(item):
    need(set(item) == {"path", "sha256"}, "callable source descriptor")
    path = Path(item["path"])
    need(path.is_absolute() and path.resolve() == path and path.is_file()
         and not path.is_symlink() and path.stat().st_size <= 2 << 20,
         "bounded canonical callable source")
    raw = _file(path.parent, path.name, 2 << 20)
    need(sha(raw) == hex64(item["sha256"]), "callable source digest")
    return list(_codes(compile(raw, str(path), "exec", dont_inherit=True, optimize=0)))


def _match(function, codes, qualified_name):
    need(type(function) is FunctionType, "native Python callable required")
    expected = [c for c in codes if c.co_qualname == qualified_name]
    need(len(expected) == 1 and function.__code__ == expected[0]
         and function.__code__.co_filename == expected[0].co_filename,
         "native callable code differs: " + qualified_name)


def _closure(function, names):
    cells = dict(zip(function.__code__.co_freevars, function.__closure__ or (), strict=True))
    need(set(cells) == set(names), "native closure fields")
    return {key: cell.cell_contents for key, cell in cells.items()}


def authenticate(scheduler_type, sources):
    """Return the exact original objects only after checking both wrapper layers."""
    need(set(sources) == {"scheduler", "stage", "nvtx"}, "complete scheduler wrapper closure")
    codes = {key: _source(item) for key, item in sources.items()}
    originals = {}
    for name in ("run_batch", "process_batch_result"):
        method = scheduler_type.__dict__.get(name)
        _match(method, codes["stage"], "scheduler_stage_method.<locals>.decorator.<locals>.wrapper")
        stage = _closure(method, ("profiled_func", "stage"))
        need(stage["stage"] == name, "scheduler stage identity")
        inner = stage["profiled_func"]
        _match(inner, codes["nvtx"], "profile_method.<locals>.decorator.<locals>.wrapper")
        profile = _closure(inner, ("color", "debug_name", "func", "nvtx_enabled"))
        need(profile["color"] is None and profile["debug_name"] == "scheduler." + name
             and profile["nvtx_enabled"] is False, "selected scheduler annotation configuration")
        native = profile["func"]
        _match(native, codes["scheduler"], "Scheduler." + name)
        need(getattr(method, "__wrapped__", None) is native
             and getattr(inner, "__wrapped__", None) is native,
             "scheduler wrapper/native identity")
        originals[name] = method
    return originals


def authenticate_target(target, sources):
    """Bind the actual worker target, its module, constructor and Scheduler class."""
    need(type(target) is FunctionType, "native scheduler target")
    module = sys.modules.get(target.__module__)
    need(module is not None and target.__globals__ is module.__dict__
         and getattr(module, "run_scheduler_process", None) is target,
         "worker target/module identity")
    codes = _source(sources["scheduler"])
    _match(target, codes, "run_scheduler_process")
    cls = getattr(module, "Scheduler", None)
    need(type(cls) is type and cls.__module__ == module.__name__ and cls.__qualname__ == "Scheduler"
         and target.__globals__.get("Scheduler") is cls, "worker Scheduler class identity")
    constructor = cls.__dict__.get("__init__")
    _match(constructor, codes, "Scheduler.__init__")
    need(constructor.__globals__ is module.__dict__, "Scheduler constructor globals")
    originals = authenticate(cls, sources)
    for method in originals.values():
        need(method.__wrapped__.__globals__ is module.__dict__, "Scheduler method globals")
    return cls, originals
