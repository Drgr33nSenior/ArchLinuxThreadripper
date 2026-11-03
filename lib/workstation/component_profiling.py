"""Opt-in profiler arguments for an already approved component sandbox.

This module only builds arguments. The packet owner retains admission, limits,
runtime hashes, process supervision and release. Importing it launches nothing.
"""
from pathlib import PurePosixPath


def absolute(value):
    path = PurePosixPath(value)
    if not path.is_absolute() or str(path) != value or '..' in path.parts or '\x00' in value:
        raise ValueError('canonical absolute sandbox path required')
    return value


def configure(payload, *, route='off', python=None, rocm_root=None, trace_directory=None,
              twin_sitepackages=None, runtime_sitepackages='/usr/local/lib/python3.14/dist-packages'):
    """Return command, extra bwrap mounts and environment; default is unchanged.

For rocprofv3, the payload must terminate naturally to flush its trace. Avoid
compiler subprocesses under its preload. For Kineto, warm/compile sentinels
before starting the Torch profiler. Profiling results remain separate from
the component's correctness and timing decision.
    """
    if not isinstance(payload, (list, tuple)) or not payload or not all(isinstance(arg, str) and '\x00' not in arg for arg in payload):
        raise ValueError('explicit payload argv required')
    if route not in {'off', 'rocprofv3', 'kineto'}:
        raise ValueError('unknown component profiling route')
    result = {'argv': list(payload), 'mounts': [], 'environment': {}}
    if route == 'off':
        if any(v is not None for v in (python, rocm_root, trace_directory, twin_sitepackages)):
            raise ValueError('profiling options require explicit opt-in')
        return result
    if route == 'rocprofv3':
        if twin_sitepackages is not None or not all((python, rocm_root, trace_directory)):
            raise ValueError('rocprofv3 requires interpreter, SDK and output paths only')
        executable, sdk, output = map(absolute, (python, rocm_root, trace_directory))
        result['argv'] = [executable, '-I', '-B', '-S', sdk + '/bin/rocprofv3', '--rocm-root', sdk,
                          '--kernel-trace', '--hip-runtime-trace', '--output-format', 'json',
                          '--output-directory', output, '--output-file', '%pid%', '--process-sync', 'false',
                          '--', *payload]
        result['environment'] = {'ROCPROF_TMPDIR': '/tmp'}
    else:
        if not twin_sitepackages or any(v is not None for v in (python, rocm_root, trace_directory)):
            raise ValueError('Kineto requires the isolated Torch site-packages path only')
        source, target = map(absolute, (twin_sitepackages, runtime_sitepackages))
        if source == target:
            raise ValueError('profiling twin must be separate from serving')
        result['mounts'] = ['--ro-bind', source + '/torch', target + '/torch']
        result['environment'] = {'WORKSTATION_COMPONENT_PROFILER': 'kineto', 'KINETO_LOG_LEVEL': '0'}
    return result
