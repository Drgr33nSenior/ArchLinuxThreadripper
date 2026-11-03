"""Closed, source-pinned serving experiment for the operation-211 winner.

The existing owner and workload are reused. Only the BF16 final call inside the
already guarded verification adapter selects the candidate; native globals and
all fallback paths remain unchanged. This module performs inert transformations.
"""

import hashlib
import json
import textwrap

FAMILY = "verify_prefetch"
FILE = "decode_attention_fwd_candidate.py"
BASELINE_SHA = "34ca0066e2716e1ffad048ec7cbb3a25af13eeb61e99034a663fed6277bf691a"
CANDIDATE_SHA = "ed26c67b69da358944999fdf9f769fafc4c2aef821335fbb777679b966e9e4e3"
ADAPTER_SHA = "731fdc22429376633785c9bd659128eedf0668575bdddbdb29c0e4816703223b"


def require(value, message):
    if not value:
        raise ValueError(message)


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def apply(files, candidate):
    require(hashlib.sha256(candidate).hexdigest() == CANDIDATE_SHA,
            "only the operation-211 verification candidate is supported")
    require(hashlib.sha256(files["verify_decode_adapter.py"]).hexdigest() == ADAPTER_SHA,
            "selected verification adapter differs")

    def edit(name, old, new):
        text = files[name].decode()
        require(text.count(old) == 1, "verification serving source drift: " + name)
        files[name] = text.replace(old, new).encode()

    files[FILE] = candidate
    overlay = json.loads(files["ALLOCATION-OVERLAY.json"])
    row = next(r for r in overlay["files"] if r["key"] == "decode_attention_fwd")
    require(row["file"] == FILE and row["original_sha256"] == BASELINE_SHA,
            "native decode provenance differs")
    # runtime_overlays continues to strip this row in BOTH arms. The existing
    # package asset is imported privately by the adapter, never over native decode.
    row["sha256"] = CANDIDATE_SHA
    files["ALLOCATION-OVERLAY.json"] = canonical(overlay)

    edit("verify_decode_adapter.py", "        self.buffers = {}\n",
         "        self.prefetch_decode = None\n"
         "        self.prefetch_calls = {8: 0, 16: 0}\n"
         "        if manifest['arm'] == 'mtp':\n"
         "            candidate = source_module(dict(\n"
         "                path=str(Path(__file__).with_name('" + FILE + "')),\n"
         "                module='decode_attention_fwd_candidate', sha256='" + CANDIDATE_SHA + "'))\n"
         "            self.prefetch_decode = candidate.decode_attention_fwd\n"
         "        self.buffers = {}\n")
    edit("verify_decode_adapter.py", "    def decode(self, backend, batch, args, layer):\n",
         "    def bf16_attention(self, backend, q, *args, **kwargs):\n"
         "        # Called only after eligible() and only from the BF16 branch.\n"
         "        if self.manifest['arm'] == 'mtp':\n"
         "            need(self.prefetch_decode is not None, 'prefetch function absent')\n"
         "            rows = int(q.shape[0])\n"
         "            need(rows in (8, 16), 'prefetch row geometry')\n"
         "            self.prefetch_calls[rows] += 1\n"
         "            return self.prefetch_decode(q, *args, **kwargs)\n"
         "        need(self.manifest['arm'] == 'control' and self.prefetch_decode is None,\n"
         "             'prefetch arm binding')\n"
         "        return backend.decode_attention_fwd(q, *args, **kwargs)\n\n"
         "    def decode(self, backend, batch, args, layer):\n")
    # Exact BF16 call; the following FP8 branch and all metadata code stay intact.
    edit("verify_decode_adapter.py",
         "        else:\n            backend.decode_attention_fwd(q, kb, vb, out, b['indptr'], b['indices'],\n",
         "        else:\n            self.bf16_attention(backend, q, kb, vb, out, b['indptr'], b['indices'],\n")

    edit("worker_entry.py", "        proof['indices_elements'] = sorted",
         "        proof['prefetch_calls'] = {str(k): v for k, v in adapter.prefetch_calls.items()}\n"
         "        proof['verification_kernel_sha256'] = ('" + CANDIDATE_SHA + "'\n"
         "            if manifest['arm'] == 'mtp' else '" + BASELINE_SHA + "')\n"
         "        proof['indices_elements'] = sorted")
    edit("serving_integration.py", "    fields.add('indices_elements')\n",
         "    fields.update(('indices_elements', 'prefetch_calls', 'verification_kernel_sha256'))\n")
    edit("serving_integration.py", "    counts = [record[key] for key in ('metadata_build_host_calls',",
         "    expected_kernel = ('" + CANDIDATE_SHA + "' if binding['arm'] == 'mtp'\n"
         "                       else '" + BASELINE_SHA + "')\n"
         "    _need(record['verification_kernel_sha256'] == expected_kernel, 'verification kernel binding')\n"
         "    calls = record['prefetch_calls']\n"
         "    _need(type(calls) is dict and set(calls) == {'8', '16'} and\n"
         "          all(type(v) is int and v >= 0 for v in calls.values()), 'prefetch counts')\n"
         "    _need((all(v >= 16 for v in calls.values()) and sum(calls.values()) == record['eligible_host_calls'])\n"
         "          if binding['arm'] == 'mtp' else all(v == 0 for v in calls.values()),\n"
         "          'prefetch absent from an eligible verification graph or used in control')\n"
         "    counts = [record[key] for key in ('metadata_build_host_calls',")
    production_witness(files, edit)
    return dict(family=FAMILY, source_file=FILE, baseline_sha256=BASELINE_SHA,
                candidate_sha256=CANDIDATE_SHA, candidate_arm="mtp",
                native_decode_overlay=False, selected_c4_both_arms=True,
                component_operation=211, serving_qualified=False)


def production_witness(files, edit):
    """Require a metadata-only handshake witness on the actual production path."""
    edit("verify_decode_adapter.py", "        self.prefetch_calls = {8: 0, 16: 0}\n",
         "        self.prefetch_calls = {8: 0, 16: 0}\n"
         "        self.bf16_capture_calls = {8: 0, 16: 0}\n"
         "        self.prefetch_capture_calls = {8: 0, 16: 0}\n")
    edit("verify_decode_adapter.py",
         "        # Called only after eligible() and only from the BF16 branch.\n",
         "        # Host metadata only; no tensor arithmetic or extra launches.\n"
         "        rows = int(q.shape[0])\n"
         "        need(rows in (8, 16), 'BF16 route row geometry')\n"
         "        capturing = self.torch.cuda.is_current_stream_capturing()\n"
         "        if capturing:\n"
         "            self.bf16_capture_calls[rows] += 1\n")
    edit("verify_decode_adapter.py", "            self.prefetch_calls[rows] += 1\n",
         "            self.prefetch_calls[rows] += 1\n"
         "            if capturing:\n"
         "                self.prefetch_capture_calls[rows] += 1\n")

    # Move the existing proof producer intact out of the precision-only path.
    worker = files["worker_entry.py"].decode()
    start = worker.index("    if contract.full_enabled_benchmark():\n", worker.index("def _precision_weights("))
    end = worker.index("    return record\n", start)
    block = worker[start:end]
    proof = textwrap.dedent(block.split("\n", 1)[1])
    proof = proof.replace("proof['prefetch_calls'] =", "proof['bf16_capture_calls'] = {str(k): v for k, v in adapter.bf16_capture_calls.items()}\n"
                         "proof['prefetch_capture_calls'] = {str(k): v for k, v in adapter.prefetch_capture_calls.items()}\n"
                         "proof['prefetch_calls'] =")
    proof = proof.replace("_atomic_json(Path(manifest['evidence_dir'])",
                          "proof.update(start_ticks=worker['start_ticks'], boot_id=worker['boot_id'])\n"
                          "_atomic_json(Path(manifest['evidence_dir'])")
    helper = "def _full_enabled_witness(manifest, worker, contract):\n" + textwrap.indent(proof, "    ") + "\n\n"
    edit("worker_entry.py", block,
         "    if contract.full_enabled_benchmark():\n"
         "        _full_enabled_witness(manifest, worker, contract)\n")
    # Reuse the exact native source/code-object authentication performed by the
    # allocator hook, but none of its allocator, probe or loaded-weight actions.
    auth_start = worker.index("    source = manifest['sources']['scheduler']", worker.index("def _allocator_startup_hook("))
    auth_end = worker.index("    observer = _source_module", auth_start)
    authentication = worker[auth_start:auth_end]
    hook = "def _verification_startup_hook(manifest, rank, gpu_id, worker, target):\n" + authentication + '''    _require(type(gpu_id) is int and gpu_id == rank == worker['rank'], 'verification GPU/TP mapping')
    called = False

    def restore():
        current = cls.__dict__.get('get_init_info')
        _require(current is wrapper or current is original, 'verification handshake replaced')
        if current is wrapper:
            cls.get_init_info = original

    def wrapper(self, *args, **kwargs):
        nonlocal called
        restore()
        _require(not called and type(self) is cls and 'get_init_info' not in self.__dict__,
                 'verification duplicate/substituted handshake')
        called = True
        result = original(self, *args, **kwargs)
        cuda = getattr(sys.modules.get('torch'), 'cuda', None)
        _require(cuda is not None and cuda.current_device() == gpu_id and
                 not cuda.is_current_stream_capturing(), 'verification startup capture/device boundary')
        _require(os.getpid() == worker['pid'] and _process_start_ticks() == worker['start_ticks'] and
                 _boot_id() == worker['boot_id'], 'verification startup worker lifecycle')
        contract = _source_module(dict(manifest['sources']['mtp_contract'], module='mtp_contract'))
        _require(not contract.instrumentation_enabled() and contract.full_enabled_benchmark(),
                 'verification production handshake contract')
        _full_enabled_witness(manifest, worker, contract)
        return result

    cls.get_init_info = wrapper
    return restore


'''
    edit("worker_entry.py", "def _allocator_startup_hook(", helper + hook + "def _allocator_startup_hook(")
    edit("worker_entry.py", "        if contract.verify_attention_observed(manifest['arm']):\n",
         "        if not instrumented:\n"
         "            restore = _verification_startup_hook(manifest, tp_rank, gpu_id, record, target)\n"
         "        if contract.verify_attention_observed(manifest['arm']):\n")

    edit("serving_integration.py",
         "    fields.update(('indices_elements', 'prefetch_calls', 'verification_kernel_sha256'))\n",
         "    fields.update(('indices_elements', 'prefetch_calls', 'verification_kernel_sha256',\n"
         "                   'bf16_capture_calls', 'prefetch_capture_calls', 'start_ticks', 'boot_id'))\n")
    edit("serving_integration.py", "    expected_kernel = (",
         "    _need(all(record[k] == worker[k] for k in ('start_ticks', 'boot_id')), 'route worker lifecycle')\n"
         "    for name in ('bf16_capture_calls', 'prefetch_capture_calls'):\n"
         "        values = record[name]\n"
         "        _need(type(values) is dict and set(values) == {'8', '16'} and\n"
         "              all(type(v) is int and v >= 0 for v in values.values()), 'captured route counts')\n"
         "    _need(record['bf16_capture_calls'] == {'8': 16, '16': 16}, 'both BF16 graph routes required')\n"
         "    _need(record['prefetch_capture_calls'] ==\n"
         "          ({'8': 16, '16': 16} if binding['arm'] == 'mtp' else {'8': 0, '16': 0}),\n"
         "          'candidate graph route differs or used in control')\n"
         "    expected_kernel = (")
    edit("serving_integration.py",
         "PRODUCTION_OBSERVATIONS = frozenset({'NATIVE-READINESS-CALLBACK.json', 'SERVING-BINDING.json', 'WORKER-MANIFEST.json'})",
         "PRODUCTION_OBSERVATIONS = frozenset({'NATIVE-READINESS-CALLBACK.json', 'SERVING-BINDING.json',\n"
         "    'WORKER-MANIFEST.json', 'FULL-ENABLED-rank-0.json', 'FULL-ENABLED-rank-1.json'})")
    edit("serving_integration.py", "    wire = reader.read_client(client_root, expected=expected, native_gate_raw=gate_raw)\n    frozen = reader.contract.requests()",
         "    for worker in full_workers:\n"
         "        name = 'FULL-ENABLED-rank-%d.json' % worker['rank']\n"
         "        _need(len(observations[name]) <= 16 << 10, 'startup route record size')\n"
         "        validate_full_enabled_record(_observation_json(observations, name), binding, worker)\n"
         "    wire = reader.read_client(client_root, expected=expected, native_gate_raw=gate_raw)\n    frozen = reader.contract.requests()")
    edit("serving_integration.py",
         "'production native observations are the readiness callback and startup bindings only'",
         "'production native observations require readiness, bindings and both startup routes'")
    # Fail before the first request, and validate again from the collected bytes.
    edit("serving_runtime.py", "        _hip_sampling_ready(material, gate)\n",
         "        if not mtp_contract.instrumentation_enabled():\n"
         "            import serving_integration\n"
         "            for worker in serving_client.validate_gate(gate, expected):\n"
         "                path = material['native_dir'] / ('FULL-ENABLED-rank-%d.json' % worker['rank'])\n"
         "                proof = serving_integration._json(serving_integration._read(path, 16 << 10))\n"
         "                serving_integration.validate_full_enabled_record(proof, binding, worker)\n"
         "        _hip_sampling_ready(material, gate)\n")
