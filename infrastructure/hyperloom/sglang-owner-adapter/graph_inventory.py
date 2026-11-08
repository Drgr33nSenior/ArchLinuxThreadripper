"""Capture-time HIP graph node inventory for the matched profile (host metadata only).

The matched worker entry installs this producer before the target worker builds any
model graph and restores it in reverse order after the shared scheduler scope. It uses
only the profiling twin's public lifecycle hooks (torch.cuda.graphs, pin 2ce9b12b):

* register_graph_capture_end_hook: fires inside CUDAGraph.capture_end after
  capture_end_pre, while the captured template is live for keep_graph=False.
  raw_cuda_graph() is read there, used immediately and never retained.
* register_graph_replay_start_hook: counts replays of inventoried graph objects
  while a fixed profiling window is open; nothing is recorded outside windows.

It never sets keep_graph, calls instantiate/reset/debug_dump/get_graph_data, reads
tensor data, issues device work or retains graph handles. The twin swallows
exceptions raised by global hooks, so every failure is recorded explicitly and the
exported inventory is INCOMPLETE; nothing falls back to trace-derived membership.
Installation checks run before any model exists and raise on mismatch.

HIP entry points are called through a frozen ABI manifest (enum values, the
hipKernelNodeParams size and func offset, the kernel-name route and the symbol
mapping). The packet builder refuses a manifest that is not QUALIFIED by its native
CPU source/ABI step; this module refuses one at install time as well.
"""
import abc
import hashlib
import json
import os
import re
import sys
import threading
import weakref

SCHEMA = 1
INVENTORY_BYTES = 192 << 10
MAX_CAPTURES = 32
MAX_NODES = 16384          # per top-level graph, children included
MAX_DEPTH = 3              # child-graph nesting
MAX_SYMBOLS = 2048         # distinct mapped symbols per graph
MAX_SYMBOL_BYTES = 8192
MAX_ERRORS = 32
ROLES = {False: "VERIFY", True: "DRAFT"}
SIZES = (1, 2)
# Closed node-type vocabulary; the ABI manifest maps each name to the header value.
NODE_TYPES = ("kernel", "memcpy", "memset", "host", "child_graph", "empty", "event_wait",
              "event_record", "ext_semaphore_signal", "ext_semaphore_wait", "mem_alloc",
              "mem_free", "memcpy_from_symbol", "memcpy_to_symbol", "batch_mem_op", "conditional")
ABI_FIELDS = {"schema", "status", "library", "header_sources", "hip_success", "node_type_values",
              "kernel_node_params", "kernel_name_route", "symbol_mapping", "evidence"}
NAME_ROUTES = ("host_function_then_module_function",)
SYMBOL_MAPPINGS = ("cxa_demangle_of_name_plus_kd",)
TWIN_HOOKS = ("register_graph_capture_end_hook", "register_graph_replay_start_hook")


class InventoryError(ValueError):
    pass


def need(ok, message):
    if not ok:
        raise InventoryError("graph inventory: " + message)


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def file_sha(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def abi_check(abi):
    """Closed manifest shape; only a QUALIFIED manifest can drive HIP calls."""
    need(type(abi) is dict and set(abi) == ABI_FIELDS and abi["schema"] == 1, "ABI manifest fields")
    need(abi["status"] == "QUALIFIED", "ABI manifest not qualified by the native source/ABI step")
    lib = abi["library"]
    need(type(lib) is dict and set(lib) == {"soname", "sha256"} and lib["soname"] == "libamdhip64.so.7"
         and type(lib["sha256"]) is str and len(lib["sha256"]) == 64, "pinned HIP library")
    need(type(abi["hip_success"]) is int, "hipSuccess value")
    values = abi["node_type_values"]
    need(type(values) is dict and set(values) <= set(NODE_TYPES) and {"kernel", "memcpy", "memset",
         "child_graph", "empty", "event_wait", "event_record"} <= set(values)
         and all(type(v) is int and v >= 0 for v in values.values())
         and len(set(values.values())) == len(values), "closed hipGraphNodeType values")
    params = abi["kernel_node_params"]
    need(type(params) is dict and set(params) == {"size", "func_offset", "pointer_size"}
         and params["pointer_size"] == 8 and type(params["size"]) is int and 8 <= params["size"] <= 256
         and type(params["func_offset"]) is int and 0 <= params["func_offset"] <= params["size"] - 8
         and params["func_offset"] % 8 == 0, "hipKernelNodeParams layout")
    need(abi["kernel_name_route"] in NAME_ROUTES and abi["symbol_mapping"] in SYMBOL_MAPPINGS,
         "qualified kernel-name route and symbol mapping")
    return abi


class HipGraphApi:
    """ctypes view of the already-loaded HIP runtime; prototypes from the pinned headers.

    hipError_t hipGraphGetNodes(hipGraph_t, hipGraphNode_t*, size_t*);
    hipError_t hipGraphNodeGetType(hipGraphNode_t, hipGraphNodeType*);
    hipError_t hipGraphKernelNodeGetParams(hipGraphNode_t, hipKernelNodeParams*);
    hipError_t hipGraphChildGraphNodeGetGraph(hipGraphNode_t, hipGraph_t*);
    const char* hipKernelNameRefByPtr(const void* hostFunction, hipStream_t stream);
    const char* hipKernelNameRef(const hipFunction_t f);
    """
    def __init__(self, ctypes, library, demangler, abi):
        c = ctypes; self.c = c; self.abi = abi; self.demangler = demangler
        p, s = c.c_void_p, c.c_size_t
        err = c.c_int
        self.get_nodes = self._fn(library, "hipGraphGetNodes", err, [p, c.POINTER(p), c.POINTER(s)])
        self.get_type = self._fn(library, "hipGraphNodeGetType", err, [p, c.POINTER(c.c_int)])
        self.kernel_params = self._fn(library, "hipGraphKernelNodeGetParams", err, [p, p])
        self.child_graph = self._fn(library, "hipGraphChildGraphNodeGetGraph", err, [p, c.POINTER(p)])
        self.name_by_ptr = self._fn(library, "hipKernelNameRefByPtr", c.c_char_p, [p, p])
        self.name_ref = self._fn(library, "hipKernelNameRef", c.c_char_p, [p])
        self.type_names = {v: k for k, v in abi["node_type_values"].items()}

    @staticmethod
    def _fn(library, name, restype, argtypes):
        function = getattr(library, name)
        function.restype = restype; function.argtypes = argtypes
        return function

    def _ok(self, code, api):
        need(code == self.abi["hip_success"], "%s returned %r" % (api, code))

    def nodes(self, graph):
        c = self.c; count = c.c_size_t(0)
        self._ok(self.get_nodes(c.c_void_p(graph), None, c.byref(count)), "hipGraphGetNodes(count)")
        need(0 < count.value <= MAX_NODES, "bounded graph node count")
        array = (c.c_void_p * count.value)(); again = c.c_size_t(count.value)
        self._ok(self.get_nodes(c.c_void_p(graph), array, c.byref(again)), "hipGraphGetNodes(nodes)")
        need(again.value == count.value, "stable graph node count")
        return [array[i] for i in range(count.value)]

    def node_type(self, node):
        value = self.c.c_int(-1)
        self._ok(self.get_type(self.c.c_void_p(node), self.c.byref(value)), "hipGraphNodeGetType")
        return self.type_names.get(value.value, "unknown:%d" % value.value)

    def kernel_symbol(self, node, stream):
        c = self.c; layout = self.abi["kernel_node_params"]
        buffer = (c.c_char * layout["size"])()
        self._ok(self.kernel_params(c.c_void_p(node), c.cast(buffer, c.c_void_p)), "hipGraphKernelNodeGetParams")
        func = c.c_void_p.from_buffer(buffer, layout["func_offset"]).value
        need(func, "kernel node function handle")
        raw = self.name_by_ptr(c.c_void_p(func), c.c_void_p(stream))
        source = "host_function"
        if raw is None:
            raw = self.name_ref(c.c_void_p(func)); source = "module_function"
        need(raw is not None and 0 < len(raw) <= MAX_SYMBOL_BYTES, "resolved kernel name")
        name = raw.decode("utf-8", "strict")
        return self.demangler(name + ".kd"), source

    def child(self, node):
        value = self.c.c_void_p(0)
        self._ok(self.child_graph(self.c.c_void_p(node), self.c.byref(value)), "hipGraphChildGraphNodeGetGraph")
        need(value.value, "child graph handle")
        return value.value


def cxa_demangler(ctypes, cxx, libc):
    """Kineto's demangling rule: __cxa_demangle, keeping the raw name when it fails.

    cxx is the libstdc++ handle already loaded by Torch (RTLD_NOLOAD); libc supplies free.
    """
    demangle = cxx.__cxa_demangle
    demangle.restype = ctypes.c_void_p
    demangle.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
    free = libc.free; free.restype = None; free.argtypes = [ctypes.c_void_p]

    def run(name):
        status = ctypes.c_int(-4)
        pointer = demangle(name.encode(), None, None, ctypes.byref(status))
        if not pointer or status.value != 0:
            if pointer: free(pointer)
            return name
        try:
            return ctypes.string_at(pointer).decode("utf-8", "strict")
        finally:
            free(pointer)
    return run


def loaded_library(maps_text, soname):
    """Return one already loaded library, allowing resolved numeric version suffixes."""
    name = re.compile(re.escape(soname) + r"(?:\.[0-9]+)*")
    paths = sorted({line.split(None, 5)[5] for line in maps_text.splitlines()
                    if len(line.split(None, 5)) == 6 and
                    name.fullmatch(line.split(None, 5)[5].rsplit("/", 1)[-1])})
    need(len(paths) == 1 and paths[0].startswith("/"), "exactly one loaded " + soname)
    return paths[0]


def capture_context(frame, code, graph, limit=8):
    """Join a capture-end callback to FullCudaGraphBackend.capture_one by code identity.

    The frame's own local `graph` must be the hooked object; locals are read only.
    """
    for _ in range(limit):
        if frame is None:
            break
        if frame.f_code is code:
            local = frame.f_locals
            need(local.get("graph") is graph, "capture frame owns the hooked graph")
            backend = local.get("self"); shape = local.get("shape_key")
            draft = getattr(getattr(getattr(backend, "_cuda_graph_runner", None), "model_runner", None),
                            "is_draft_worker", None)
            need(type(draft) is bool, "model runner draft flag")
            size = getattr(shape, "size", None)
            need(type(size) is int and size in SIZES, "graph shape size")
            fields = {k: getattr(shape, k, None) for k in ("stream_idx", "variant_label", "attention_variant")}
            need(all(v is None or type(v) in (int, str) for v in fields.values()), "plain shape key fields")
            stream = getattr(getattr(backend, "_capture_stream", None), "cuda_stream", None)
            need(type(stream) is int and stream != 0, "capture stream handle")
            return {"graph_role": ROLES[draft], "bs": size, "shape": dict(fields, size=size)}, stream
        frame = frame.f_back
    return None, None


class Inventory:
    """Per-process producer state; one instance per worker start."""
    def __init__(self, identity, abi_sha256, producer_sha256, torch_version, hip_version,
                 library, api, capture_code, window, keep_graph_flag):
        self.identity = identity; self.lock = threading.Lock()
        self.producer = {"source_sha256": producer_sha256, "abi_sha256": abi_sha256,
                         "torch_version": torch_version, "torch_hip_version": hip_version,
                         "torch_graphs_sha256": None, "library": library}
        self.api = api; self.code = capture_code; self.window = window
        self.keep_graph_flag = keep_graph_flag
        self.captures = []; self.by_object = {}; self.errors = []
        self.replays = {}; self.foreign_replays = {}; self.replay_threads = set()
        self.handles = []; self.restored = False; self.exported = None

    def _error(self, where, error):
        if len(self.errors) < MAX_ERRORS:
            self.errors.append({"where": where, "type": type(error).__name__, "message": str(error)[:200]})

    def _walk(self, graph, stream, depth, nodes, symbols, index):
        need(depth <= MAX_DEPTH, "child graph depth")
        for node in self.api.nodes(graph):
            need(len(nodes) < MAX_NODES, "bounded node inventory")
            kind = self.api.node_type(node)
            row = [kind, None, None, depth]
            if kind == "kernel":
                symbol, source = self.api.kernel_symbol(node, stream)
                if symbol not in index:
                    need(len(index) < MAX_SYMBOLS, "bounded symbol table")
                    index[symbol] = len(symbols); symbols.append(symbol)
                row[1] = index[symbol]; row[2] = source
            nodes.append(row)
            if kind == "child_graph":
                self._walk(self.api.child(node), stream, depth + 1, nodes, symbols, index)

    def on_capture_end(self, graph):
        """Global capture-end hook body; never raises (the twin would swallow it)."""
        try:
            frame = sys._getframe(1)
            context, stream = capture_context(frame, self.code, graph)
            with self.lock:
                need(len(self.captures) < MAX_CAPTURES, "bounded capture count")
                index = len(self.captures)
                row = {"capture_index": index, "attributed": context is not None,
                       "graph_role": None if context is None else context["graph_role"],
                       "bs": None if context is None else context["bs"],
                       "shape": None if context is None else context["shape"],
                       "keep_graph": self.keep_graph_flag(graph), "status": "PENDING",
                       "thread": threading.get_ident()}
                self.captures.append(row)
                # Weak: the producer must not extend any graph's lifetime.
                self.by_object[id(graph)] = (index, weakref.ref(graph), context is not None)
            if context is None:
                row["status"] = "UNATTRIBUTED_NOT_INVENTORIED"
                return
            need(row["keep_graph"] is False, "selected capture keeps no template")
            nodes, symbols = [], []
            # raw_cuda_graph() is valid only inside this hook for keep_graph=False.
            self._walk(graph.raw_cuda_graph(), stream, 0, nodes, symbols, {})
            counts = {}
            for kind, _, _, _ in nodes:
                counts[kind] = counts.get(kind, 0) + 1
            row.update(status="INVENTORIED", node_count=len(nodes), type_counts=counts, symbols=symbols,
                       nodes=[[kind, symbol] for kind, symbol, _, _ in nodes],
                       name_sources=sorted({source for _, _, source, _ in nodes if source}),
                       max_depth=max(depth for _, _, _, depth in nodes))
            row["nodes_sha256"] = sha(canonical({"symbols": symbols, "nodes": row["nodes"]}))
        except BaseException as error:
            self._error("capture_end", error)
            try:
                if self.captures and self.captures[-1]["status"] == "PENDING":
                    self.captures[-1]["status"] = "FAILED"
            except BaseException:
                pass

    def on_replay_start(self, graph):
        """Global replay-start hook body: window-scoped counts only; never raises."""
        try:
            window = self.window()
            if window is None:
                return
            entry = self.by_object.get(id(graph))
            # Only attributed (VERIFY/DRAFT) captures may replay inside a window.
            key = str(entry[0]) if entry is not None and entry[1]() is graph and entry[2] else None
            with self.lock:
                self.replay_threads.add(threading.get_ident())
                target = self.replays if key is not None else self.foreign_replays
                counts = target.setdefault(window, {})
                counts[key or "foreign"] = counts.get(key or "foreign", 0) + 1
        except BaseException as error:
            self._error("replay_start", error)

    def restore(self):
        """Remove both hooks in reverse registration order; idempotent."""
        if self.restored:
            return
        self.restored = True
        failures = []
        for handle in reversed(self.handles):
            try:
                handle.remove()
            except BaseException as error:
                failures.append(error)
        self.handles = []
        need(not failures, "hook removal")

    def status(self):
        selected = [c for c in self.captures if c["attributed"]]
        keys = sorted((c["graph_role"], c["bs"]) for c in selected)
        complete = (not self.errors and all(c["status"] == "INVENTORIED" for c in selected)
                    and keys == sorted((r, b) for r in ("DRAFT", "VERIFY") for b in SIZES)
                    and not self.foreign_replays and len(self.replay_threads) <= 1)
        return "COMPLETE" if complete else "INCOMPLETE"

    def document(self):
        ident = self.identity
        return {"schema": SCHEMA, "rank": ident["rank"], "pid": ident["pid"], "nonce": ident["nonce"],
                "start": ident["start"], "role": ident["role"], "status": self.status(),
                "producer": self.producer, "captures": self.captures, "replays": self.replays,
                "foreign_replays": self.foreign_replays, "replay_thread_count": len(self.replay_threads),
                "errors": self.errors, "host_metadata_only": True, "gpu_tensor_reads": 0,
                "keep_graph_changed": False, "completion_claimed": False}

    def export(self, directory):
        """Write GRAPH-INVENTORY-rank-N.json exclusively; oversize keeps a bounded INCOMPLETE stub."""
        need(self.exported is None, "one inventory export")
        value = self.document(); raw = canonical(value)
        if len(raw) > INVENTORY_BYTES:
            stub = {k: value[k] for k in ("schema", "rank", "pid", "nonce", "start", "role", "producer",
                                          "host_metadata_only", "gpu_tensor_reads", "keep_graph_changed",
                                          "completion_claimed")}
            stub.update(status="INCOMPLETE", captures=[], replays={}, foreign_replays={},
                        replay_thread_count=value["replay_thread_count"],
                        errors=[{"where": "export", "type": "OversizeInventory", "message": str(len(raw))}])
            raw = canonical(stub)
        name = "GRAPH-INVENTORY-rank-%d.json" % self.identity["rank"]
        path = os.path.join(directory, name)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
        self.exported = {"file": name, "sha256": sha(raw), "bytes": len(raw)}
        return self.exported


def install(torch, identity, config, sources, window, *, binding, ctypes_module=None,
            maps_text=None, library_loader=None, demangler=None):
    """Verify the live twin, backend and HIP runtime, then register both global hooks.

    binding: shared_runtime_binding (its _source/_match authenticate source code).
    library_loader/demangler are CPU test seams only; the worker passes neither.
    Raises before any model exists if an identity or ABI premise fails.
    """
    need(identity.get("schema") == 2 and identity.get("profiled") is True, "matched profiled identity")
    need(set(config) == {"abi", "abi_sha256", "producer_sha256", "twin_graphs_sha256", "torch_version"},
         "inventory config")
    abi = abi_check(config["abi"])
    need(sha(canonical(abi)) == config["abi_sha256"], "ABI manifest identity")
    graphs = sys.modules.get("torch.cuda.graphs")
    need(graphs is not None and getattr(torch.cuda, "graphs", None) is graphs, "loaded twin graph module")
    need(file_sha(graphs.__file__) == config["twin_graphs_sha256"], "profiling twin graphs.py pin")
    need(torch.__version__ == config["torch_version"], "profiling twin Torch version")
    for name in TWIN_HOOKS:
        fn = getattr(graphs, name, None)
        need(fn is not None and fn.__code__.co_filename == graphs.__file__, "twin hook " + name)
    cuda_graph = graphs.CUDAGraph
    need(getattr(cuda_graph, "capture_end", None) is not None
         and cuda_graph.capture_end.__code__.co_filename == graphs.__file__, "twin capture_end")
    producer = sys.modules.get("graph_inventory")
    need(producer is not None and file_sha(producer.__file__) == config["producer_sha256"], "producer pin")
    descriptor = sources["backend"]
    module = sys.modules.get("sglang.srt.model_executor.runner_backend.full_cuda_graph_backend")
    need(module is not None and module.__file__ == descriptor["path"], "loaded graph backend module")
    backend_cls = getattr(module, "FullCudaGraphBackend", None)
    # The selected class subclasses BaseCudaGraphBackend(ABC), so its metaclass is exactly
    # abc.ABCMeta (op228 refused it as a plain-type check). Any other metaclass, a non-class
    # or a class bound from another module is refused before capture_one is authenticated.
    need(type(backend_cls) is abc.ABCMeta and backend_cls.__module__ == module.__name__
         and backend_cls.__qualname__ == "FullCudaGraphBackend", "graph backend class")
    method = backend_cls.__dict__.get("capture_one")
    binding._match(method, binding._source(descriptor), "FullCudaGraphBackend.capture_one")
    if ctypes_module is None:
        import ctypes as ctypes_module
    if maps_text is None:
        with open("/proc/self/maps", "r", encoding="utf-8", errors="strict") as stream:
            maps_text = stream.read(64 << 20)
    path = loaded_library(maps_text, abi["library"]["soname"])
    need(file_sha(path) == abi["library"]["sha256"], "loaded HIP runtime library pin")
    if library_loader is None:
        library_loader = lambda name: ctypes_module.CDLL(name, mode=os.RTLD_NOLOAD | os.RTLD_LAZY)
    library = library_loader(path)
    if demangler is None:
        # Never loads a new library: both are resolved from the live process maps.
        demangler = cxa_demangler(ctypes_module, library_loader(loaded_library(maps_text, "libstdc++.so.6")),
                                  ctypes_module.CDLL(None))
    api = HipGraphApi(ctypes_module, library, demangler, abi)
    inventory = Inventory(identity, config["abi_sha256"], config["producer_sha256"], torch.__version__,
                          getattr(torch.version, "hip", None), {"path": path, "sha256": abi["library"]["sha256"]},
                          api, method.__code__, window, lambda graph: graph._keep_graph)
    inventory.producer["torch_graphs_sha256"] = config["twin_graphs_sha256"]
    try:
        inventory.handles.append(graphs.register_graph_capture_end_hook(inventory.on_capture_end))
        inventory.handles.append(graphs.register_graph_replay_start_hook(inventory.on_replay_start))
    except BaseException:
        inventory.restore()
        raise
    return inventory
