"""Private parent/evaluator protocol. Start with python3 -I -B before the agent.

The parent pins this bootstrap and the checker manifest before launch. Every
checker import then compiles the verified bytes captured below, never a later
filesystem version. The parent owns stdin/stdout and a fixed capability lease;
these channels must never be exposed to the agent.
"""
import hashlib
import importlib.abc
import importlib.util
import json
from pathlib import Path
import sys

MAX_MESSAGE = 4 * 1024 * 1024


def receive():
    line = sys.stdin.buffer.readline(MAX_MESSAGE + 1)
    if len(line) > MAX_MESSAGE or not line.endswith(b'\n'):
        raise ValueError('missing or oversized parent message')
    value = json.loads(line)
    if not isinstance(value, dict):
        raise ValueError('parent message must be an object')
    return value


def send(value):
    print(json.dumps(value, sort_keys=True), flush=True)


class PinnedModules(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, root, contents):
        self.root = root
        self.contents = {name[:-3].replace('/', '.'): data for name, data in contents.items()}

    def find_spec(self, fullname, path=None, target=None):
        if fullname in self.contents:
            return importlib.util.spec_from_loader(fullname, self)
        return None

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        path = str(self.root / (module.__name__.replace('.', '/') + '.py'))
        module.__file__ = path
        exec(compile(self.contents[module.__name__], path, 'exec'), module.__dict__)


def main():
    start = receive()
    root = Path(__file__).resolve().parent
    paths = [path for path in root.rglob('*.py') if '__pycache__' not in path.parts]
    if any(path.is_symlink() for path in paths):
        raise ValueError('checker source links are not trusted')
    contents = {path.relative_to(root).as_posix(): path.read_bytes() for path in paths}
    identities = {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}
    if start.get('checker') != identities:
        raise ValueError('checker tree does not match the parent-held immutable pin')
    sys.meta_path.insert(0, PinnedModules(root, contents))
    import task_acceptance as acceptance
    lease = start.get('capability_lease')
    names = start.get('routes')
    if not isinstance(lease, str) or not lease or not isinstance(names, list) or not names:
        raise ValueError('a fixed parent capability lease and actual route names are required')

    def probe(route):
        def invoke(operation, path):
            send({'probe': operation, 'path': path, 'route': route, 'capability_lease': lease})
            response = receive()
            if response.get('capability_lease') != lease:
                return 'unavailable'
            return response.get('outcome', 'unavailable')
        return invoke

    attempt = acceptance.begin_attempt(start['source'], start['workspace'], start['seal'],
                                      agent_routes={name: probe(name) for name in names},
                                      protected_configuration=start.get('configuration', []),
                                      state=start.get('state_path'), expected_checker=identities)
    send({'ready': attempt.before['status'] == acceptance.PASSED,
          'protection': attempt.before, 'capability_lease': lease})
    final = receive()
    if final.get('capability_lease') != lease:
        raise ValueError('capability lease changed during the attempt')
    if 'observation' in final:
        attempt.observe(final['observation'])
    result = acceptance.check_task(start['source'], start['workspace'], start['seal'], attempt=attempt,
                                   answer=final.get('answer'), origins=final.get('origins', []),
                                   claim=final.get('claim'), run_id=start.get('run_id'),
                                   attempt_id=start.get('attempt_id'))
    acceptance.validate_result(result)
    send({'result': result})
    return acceptance.EXIT_STATUS[result['verdict']]


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError) as error:
        print('session unavailable: ' + str(error), file=sys.stderr)
        raise SystemExit(3)
