"""Finite SSH exchange transport through the existing pinned connection helper."""

import base64
import importlib.util
import json
from pathlib import Path
import sys
import time
import zlib

LIB = Path(__file__).resolve().parents[3] / "lib/workstation"
sys.path.insert(0, str(LIB))
import hyperloom_exchange as exchange
from hyperloom_sglang import checked_bytes, decoded_json, require


CONNECTION_SHA = "1d885651f2d93b9f019bcce04c2022726a9c8af5676e475dc92035047b947dde"
COMMANDS_SHA = "71ac3ebfc4eef1c0b373a7a8317c1f72d300a375f307cce1500bbdb0a659711a"
MODULES = ("hyperloom_sglang", "hyperloom_latency", "hyperloom_exchange",
           "hyperloom_owner_backend", "hyperloom_exchange_host")
MAX_WIRE = 48 << 20
REMOTE = r'''
import base64,hashlib,json,os,resource,socket,sys,types,zlib
from pathlib import Path
assert os.getuid()==os.geteuid()==1000 and socket.gethostname()=='threadripper-ai'
resource.setrlimit(resource.RLIMIT_CPU,(10,10))
resource.setrlimit(resource.RLIMIT_AS,(512<<20,512<<20))
resource.setrlimit(resource.RLIMIT_FSIZE,(16<<20,16<<20))
def audit(event,args):
 if event=='import' and args[0].split('.')[0] in {'torch','triton','sglang','numpy'}:
  raise RuntimeError('framework import refused in exchange transport')
 if event in {'subprocess.Popen','os.system','os.fork','os.posix_spawn','socket.__new__','socket.connect'} or event.startswith('os.exec'):
  raise RuntimeError('child/network execution refused in exchange transport')
 if event=='open' and isinstance(args[0],(str,bytes)):
  name=os.fsdecode(args[0])
  if name.startswith(('/dev/kfd','/dev/dri/')):raise RuntimeError('GPU device access refused')
sys.addaudithook(audit)
raw=zlib.decompress(base64.b64decode(PAYLOAD))
assert len(raw)<=48<<20 and hashlib.sha256(raw).hexdigest()==PAYLOAD_SHA
payload=json.loads(raw)
assert Path('/proc/sys/kernel/random/boot_id').read_text().strip()==payload['boot_id']
for row in payload['modules']:
 source=base64.b64decode(row['source'],validate=True)
 assert hashlib.sha256(source).hexdigest()==row['sha256']
 module=types.ModuleType(row['name']);module.__file__='/controller-source/'+row['name']+'.py'
 assert row['name'] not in sys.modules
 sys.modules[row['name']]=module
 exec(compile(source,module.__file__,'exec',dont_inherit=True),module.__dict__)
backend=sys.modules['hyperloom_owner_backend'];host=sys.modules['hyperloom_exchange_host']
bound=backend.read_binding(payload['binding']['path'],payload['binding']['sha256'])
result=host.dispatch(payload['action'],bound,job_id=payload['job_id'],result=payload['result'])
wire=sys.modules['hyperloom_exchange'].canonical(result)
assert len(wire)<=48<<20
print(wire.decode(),end='',flush=True)
'''


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def recipe(config, action, *, job_id=None, result=None):
    require(action in {"list", "take", "cancelled", "publish", "park"}, "bounded exchange RPC required")
    require(set(config["module_pins"]) == set(MODULES), "closed exchange module set required")
    modules = []
    for name in MODULES:
        raw = checked_bytes(LIB / (name + ".py"), config["module_pins"][name], 1 << 20)
        modules.append(dict(name=name, sha256=exchange.digest(raw), source=base64.b64encode(raw).decode()))
    payload = dict(modules=modules, boot_id=config["boot_id"], binding=config["native_binding"],
                   action=action, job_id=job_id, result=result)
    raw = exchange.canonical(payload)
    require(len(raw) <= MAX_WIRE, "bounded exchange payload required")
    source = ("PAYLOAD=" + repr(base64.b64encode(zlib.compress(raw, 9)).decode()) + "\nPAYLOAD_SHA="
              + repr(exchange.digest(raw)) + "\n" + REMOTE).encode()
    compile(source, "bounded-owner-exchange", "exec", dont_inherit=True)
    return source


def call(config, action, destination, *, job_id=None, result=None, deadline_unix_ms=None):
    """Exactly one transport invocation; preserve any failed receipt, no retry."""
    destination = Path(destination)
    require(destination.is_absolute() and destination.parent.resolve() == destination.parent
            and destination.parent.is_dir() and not destination.exists() and not destination.is_symlink(),
            "fresh canonical RPC receipt directory required")
    raw = recipe(config, action, job_id=job_id, result=result)
    connection_raw = checked_bytes(Path(config["connection"]), CONNECTION_SHA, 1 << 20)
    commands_raw = checked_bytes(Path(config["local_commands"]), COMMANDS_SHA, 1 << 20)
    destination.mkdir(mode=0o700)
    (destination / "connection.py").write_bytes(connection_raw)
    (destination / "local_commands.py").write_bytes(commands_raw)
    (destination / "RECIPE.py").write_bytes(raw)
    connection = load(destination / "connection.py", "_hyperloom_exchange_connection")
    commands = load(destination / "local_commands.py", "_hyperloom_exchange_commands")
    connection.check()
    seconds = 20
    if deadline_unix_ms is not None:
        require(type(deadline_unix_ms) is int, "finite exchange deadline required")
        seconds = min(seconds, int(deadline_unix_ms / 1000 - time.time() - 10))
        require(seconds > 0, "exchange transport does not fit the remaining deadline")
    receipt = commands.run("exchange", connection.SSH + ["/usr/bin/python3", "-I", "-B", "-S"],
                           seconds, stdin=raw, cwd=destination, output_limit=MAX_WIRE)
    wire = checked_bytes(destination / "exchange.stdout", receipt["stdout"]["sha256"], MAX_WIRE)
    value = decoded_json(wire)
    require(isinstance(value, dict) and value.get("schema") == 1, "exchange response schema")
    return value
