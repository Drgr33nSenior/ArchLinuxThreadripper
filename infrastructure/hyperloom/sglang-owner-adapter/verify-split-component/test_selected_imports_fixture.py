"""Exact selected v5 source/import functions used by the state entry."""
import hashlib,importlib,importlib.util,importlib.machinery,json,math,os,stat,sys,tempfile,time
from pathlib import Path
ROOT=Path(__file__).resolve().parent
SOURCE_ROOT=Path('/mnt/uq-build/image-source/python/sglang')
RUNTIME_PARENT=Path('/usr/local/lib/python3.14/dist-packages')
APPLICATION_PARENT=SOURCE_ROOT.parent
COMPATIBILITY_PARENT=Path('/mnt/compat')
FILE_LIMIT,TOTAL_LIMIT,REPORT_LIMIT,MEMBER_LIMIT=16<<20,32<<20,1<<20,128
SOURCE_FILE_LIMIT,INVENTORY_MEMBERS=512<<20,5182

class ProbeFailure(ValueError):
    pass

def need(condition, reason):
    if not condition:
        raise ProbeFailure(reason)

def sha(raw):
    return hashlib.sha256(raw).hexdigest()

def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)+'\n').encode()

def check_deadline(deadline, clock=time.monotonic):
    need(type(deadline) in (int, float) and math.isfinite(deadline) and clock() < deadline,
         'finite work deadline expired or invalid')

def held_source(path, pin, maximum):
    path = Path(path)
    need(path.is_absolute() and path.resolve() == path, 'canonical source required')
    fields = ('st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_gid', 'st_nlink',
              'st_size', 'st_mtime_ns', 'st_ctime_ns')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
        before = os.fstat(stream.fileno())
        need(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_size <= maximum,
             'bounded regular source required')
        raw = stream.read(maximum+1)
        need(len(raw) == before.st_size and all(getattr(before, key) == getattr(os.fstat(stream.fileno()), key)
             == getattr(path.lstat(), key) for key in fields) and sha(raw) == pin, 'source identity differs')
    return raw

class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        need(self.directory.is_absolute() and self.directory.parent.resolve() == self.directory.parent,
             'canonical output parent required')
        self.directory.mkdir(mode=0o700)
        self.files, self.total = {}, 0

    def save(self, name, raw, *, report=False):
        need(type(raw) is bytes and len(raw) <= (REPORT_LIMIT if report else FILE_LIMIT), 'output file bound')
        need(Path(name).name == name and name not in ('', '.', '..'), 'fixed output basename required')
        if name in self.files:
            need(self.files[name]['sha256'] == sha(raw), 'output identity collision')
            return self.files[name]
        need(len(self.files)+1 <= MEMBER_LIMIT-(0 if report else 1), 'output member bound')
        need(self.total+len(raw) <= TOTAL_LIMIT-(0 if report else REPORT_LIMIT), 'aggregate output bound')
        path = self.directory/name
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        row = dict(name=name, bytes=len(raw), sha256=sha(raw))
        self.files[name], self.total = row, self.total+len(raw)
        return row

def load_source(path,name,pin):
    raw=held_source(path,pin,2<<20)
    module=importlib.util.module_from_spec(importlib.util.spec_from_file_location(name,path))
    sys.modules[name]=module
    exec(compile(raw,str(path),'exec'),module.__dict__)
    return module

def consumed_sources(inventory, *, modules=None, source_root=SOURCE_ROOT):
    # Namespace packages carry no executable source. Every executable SGLang
    # module must originate in the admitted read-only tree and match its pin.
    rows={}
    modules=sys.modules if modules is None else modules
    for name,module in sorted(list(modules.items())):
        if not (name=='sglang' or name.startswith('sglang.')):continue
        path=getattr(module,'__file__',None)
        if path is None:
            spec=getattr(module,'__spec__',None);paths=list(getattr(module,'__path__',[]))
            need(spec is not None and spec.origin is None and type(spec.loader).__name__=='NamespaceLoader' and paths,'source namespace identity')
            need(all(Path(path).resolve().is_relative_to(source_root) for path in paths),'source namespace origin')
            rows[name]=dict(namespace_paths=paths);continue
        path=Path(path)
        need(path.is_absolute() and path.is_relative_to(source_root),'consumed source origin: '+name)
        relative=path.relative_to(source_root).as_posix()
        need(relative in inventory['files'],'consumed source absent from inventory: '+name)
        pin=inventory['files'][relative];raw=held_source(path,pin,2<<20)
        rows[name]=dict(path=str(path),sha256=pin,bytes=len(raw))
    need(rows,'empty consumed SGLang source closure')
    return rows

def authenticate_inventory(inventory, checked_file, deadline, *, source_root=SOURCE_ROOT, expected_members=INVENTORY_MEMBERS):
    """Check the complete immutable application tree before executable import."""
    need(not any(name=='sglang' or name.startswith('sglang.') for name in sys.modules),
         'SGLang imported before complete source authentication')
    files=inventory['files']
    need(type(files) is dict and len(files)==expected_members, 'exact complete source inventory required')
    rows={}
    for relative,pin in sorted(files.items()):
        check_deadline(deadline)
        path=Path(relative)
        need(type(relative) is str and not path.is_absolute() and str(path)==relative and
             all(part not in ('', '.', '..') for part in relative.split('/')),
             'canonical inventory relative path required')
        try:
            rows[relative]=checked_file(source_root/relative,pin,maximum=SOURCE_FILE_LIMIT)
        except BaseException as error:
            error.add_note('pre-import SGLang inventory member: '+relative)
            raise
    check_deadline(deadline)
    return dict(complete=True,before_any_sglang_import=True,source_root=str(source_root),
        inventory_members=len(rows),inventory_files_sha256=sha(encoded(files)),
        total_bytes=sum(row['bytes'] for row in rows.values()),files=rows)

def isolate_pycache(*, parent='/tmp'):
    # -B prevents writes but does not prevent loading adjacent stale pyc files.
    # A fresh exclusive prefix redirects all cache reads outside the source tree.
    prefix=Path(tempfile.mkdtemp(prefix='attention-pycache-',dir=parent)).resolve()
    need(prefix.is_dir() and not any(prefix.iterdir()), 'fresh empty pycache prefix required')
    sys.pycache_prefix=str(prefix)
    sys.dont_write_bytecode=True
    importlib.invalidate_caches()
    return dict(prefix=str(prefix),created_exclusive=True,initial_members=0,
                dont_write_bytecode=True,source_adjacent_cache_reads=False)

class AuthenticatedSourceFinder:
    """Refuse executable application modules absent from the prechecked tree."""
    def __init__(self,inventory,source_root=SOURCE_ROOT):
        self.files=inventory['files'];self.source_root=source_root

    def find_spec(self,fullname,path=None,target=None):
        if not (fullname=='sglang' or fullname.startswith('sglang.')):return None
        spec=importlib.machinery.PathFinder.find_spec(fullname,path,target)
        need(spec is not None,'application module absent from authenticated source: '+fullname)
        if spec.origin is None:
            paths=list(spec.submodule_search_locations or [])
            need(paths and all(Path(item).resolve().is_relative_to(self.source_root) for item in paths),
                 'application namespace outside authenticated source: '+fullname)
        else:
            origin=Path(spec.origin)
            need(origin.is_absolute() and origin.resolve()==origin and origin.is_relative_to(self.source_root),
                 'application module outside authenticated source: '+fullname)
            need(origin.relative_to(self.source_root).as_posix() in self.files,
                 'application module not preauthenticated: '+fullname)
            need(type(spec.loader) in (importlib.machinery.SourceFileLoader,importlib.machinery.ExtensionFileLoader),
                 'unvalidated application bytecode/loader: '+fullname)
        return spec

def load_runtime():
    # Only the authenticated SDK is reachable while Torch/Triton initialize.
    need(str(COMPATIBILITY_PARENT) not in sys.path,'compatibility path precedes SDK import')
    need(str(APPLICATION_PARENT) not in sys.path, 'application path precedes SDK import')
    sys.path.insert(0,str(RUNTIME_PARENT))
    torch=importlib.import_module('torch');triton=importlib.import_module('triton')
    need(Path(torch.__file__).resolve()==RUNTIME_PARENT/'torch/__init__.py','current Torch origin differs')
    need(Path(triton.__file__).resolve()==RUNTIME_PARENT/'triton/__init__.py','current Triton origin differs')
    torch.set_num_threads(1)
    # SDK modules are authenticated/loaded. Resolve SGLang from its narrow mount
    # ahead of the SDK's separate SGLang installation; origin guards stay strict.
    sys.path.insert(0,str(COMPATIBILITY_PARENT))
    sys.path.insert(0,str(APPLICATION_PARENT))
    return torch,triton
