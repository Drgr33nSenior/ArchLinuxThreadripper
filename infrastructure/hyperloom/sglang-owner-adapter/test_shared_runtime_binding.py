"""Use whole installed source files as inert callable-code fixtures."""
from contextlib import nullcontext
from functools import partial, wraps
import os
from pathlib import Path
import sys
from types import FunctionType, ModuleType
from unittest.mock import patch
from typing import cast
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import shared_runtime_binding as binding
from shared_diagnostic import sha

SOURCE = Path(os.environ["SHARED_SOURCE_FIXTURES"])


def fixture(scope=None):
    scope = {} if scope is None else scope
    sources = {key: {"path": str(SOURCE / name), "sha256": sha((SOURCE / name).read_bytes())}
               for key, name in (("scheduler", "scheduler.py"), ("stage", "scheduler_stage_metrics.py"),
                                 ("nvtx", "nvtx_utils.py"))}
    codes = {key: binding._source(item) for key, item in sources.items()}
    def fn(key, name, scope):
        found = [c for c in codes[key] if c.co_qualname == name]
        assert len(found) == 1
        return FunctionType(found[0], scope)
    profile = fn("nvtx", "profile_method", {"wraps": wraps, "profile_range": lambda *a, **kw: nullcontext()})
    profile.__kwdefaults__ = {"color": None, "nvtx_enabled": False}
    stage = fn("stage", "scheduler_stage_method", {
        "_SCHEDULER_STAGE_CATEGORY_SET": {"run_batch", "process_batch_result"}, "wraps": wraps,
        "scheduler_nvtx_method": partial(profile, nvtx_enabled=False), "cast": cast, "_F": object})
    methods = {}
    for name in ("run_batch", "process_batch_result"):
        native = fn("scheduler", "Scheduler." + name, scope)
        methods[name] = stage(name)(native)
    return type("Scheduler", (), methods), sources


def target_fixture():
    module = ModuleType("shared_inert_scheduler")
    cls, sources = fixture(module.__dict__)
    cls.__module__ = module.__name__
    codes = binding._source(sources["scheduler"])
    for qualname, container, name in (("Scheduler.__init__", cls, "__init__"),
                                      ("run_scheduler_process", module, "run_scheduler_process")):
        code = next(c for c in codes if c.co_qualname == qualname)
        setattr(container, name, FunctionType(code, module.__dict__))
    module.Scheduler = cls
    return module, sources


class TestBinding(unittest.TestCase):
    def test_actual_target_module_constructor_and_class_are_bound(self):
        module, sources = target_fixture()
        with patch.dict(sys.modules, {module.__name__: module}):
            cls, originals = binding.authenticate_target(module.run_scheduler_process, sources)
            self.assertIs(cls, module.Scheduler)
            self.assertIs(originals["run_batch"], cls.run_batch)

    def test_target_class_constructor_and_globals_substitutions_refuse(self):
        for mutation in ("module", "class", "constructor", "method_globals"):
            with self.subTest(mutation=mutation):
                module, sources = target_fixture(); target = module.run_scheduler_process
                with patch.dict(sys.modules, {module.__name__: module}):
                    if mutation == "module": module.run_scheduler_process = lambda: None
                    if mutation == "class": module.Scheduler = type("Other", (), {})
                    if mutation == "constructor": module.Scheduler.__init__ = lambda self: None
                    if mutation == "method_globals":
                        native = module.Scheduler.run_batch.__wrapped__
                        module.Scheduler.run_batch.__wrapped__ = FunctionType(native.__code__, {})
                    with self.assertRaises(ValueError): binding.authenticate_target(target, sources)

    def test_two_real_decorator_layers_match_whole_source_compile(self):
        cls, sources = fixture()
        originals = binding.authenticate(cls, sources)
        self.assertIs(originals["run_batch"], cls.run_batch)
        self.assertIs(originals["process_batch_result"], cls.process_batch_result)

    def test_changed_native_body_is_refused(self):
        cls, sources = fixture()
        cls.run_batch.__wrapped__.__code__ = (lambda *a: None).__code__
        with self.assertRaisesRegex(ValueError, "callable code differs"):
            binding.authenticate(cls, sources)

    def test_hidden_foreign_profiled_function_is_refused(self):
        cls, sources = fixture()
        cells = dict(zip(cls.run_batch.__code__.co_freevars, cls.run_batch.__closure__))
        cells["profiled_func"].cell_contents = lambda *a: None
        with self.assertRaisesRegex(ValueError, "callable code differs"):
            binding.authenticate(cls, sources)

    def test_stage_label_and_nvtx_change_are_refused(self):
        for which in ("stage", "nvtx_enabled"):
            with self.subTest(which=which):
                cls, sources = fixture()
                cells = dict(zip(cls.run_batch.__code__.co_freevars, cls.run_batch.__closure__))
                if which == "stage": cells[which].cell_contents = "idle"
                else:
                    inner = cells["profiled_func"].cell_contents
                    dict(zip(inner.__code__.co_freevars, inner.__closure__))[which].cell_contents = True
                with self.assertRaises(ValueError): binding.authenticate(cls, sources)

    def test_forged_wrapped_link_and_filename_are_refused(self):
        cls, sources = fixture(); cls.run_batch.__wrapped__ = lambda: None
        with self.assertRaisesRegex(ValueError, "wrapper/native identity"):
            binding.authenticate(cls, sources)
        cls, sources = fixture(); cls.run_batch.__code__ = cls.run_batch.__code__.replace(co_filename="foreign.py")
        with self.assertRaisesRegex(ValueError, "callable code differs"):
            binding.authenticate(cls, sources)

    def test_wrong_source_pin_is_refused(self):
        cls, sources = fixture(); sources["stage"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "source digest"):
            binding.authenticate(cls, sources)


if __name__ == "__main__": unittest.main()
