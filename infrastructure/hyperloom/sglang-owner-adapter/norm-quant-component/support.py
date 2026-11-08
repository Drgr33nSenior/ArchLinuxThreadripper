"""Bounded component artifacts (op 164's writer and caps, unchanged) and small helpers."""
import hashlib, json, math, time

FILE_LIMIT = 16 << 20
RESULT_LIMIT = 2 << 20
COMPONENT_LIMIT = 24 << 20
MEMBER_LIMIT = 96
FAILURE_LIMIT = 1 << 20
REPORT = 'MXFP4-RESULT.json'  # the owner's fixed component report name


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def check(deadline):
    if type(deadline) not in (int, float) or not math.isfinite(deadline) or time.monotonic() >= deadline:
        raise TimeoutError('component deadline')


class Artifacts:
    def __init__(self, root, deadline):
        root.mkdir(mode=0o700, exist_ok=False)
        self.root, self.deadline, self.bytes, self.members = root, deadline, 0, []

    def write(self, name, raw):
        check(self.deadline)
        report = name == REPORT; failure = name in ('failure.raw', 'failure.json')
        if type(raw) is not bytes or '/' in name or name in ('', '.', '..'): raise ValueError('exact artifact bytes/name')
        limit = RESULT_LIMIT if report else FAILURE_LIMIT // 2 if failure else FILE_LIMIT
        if len(raw) > limit: raise ValueError('individual artifact byte cap')
        reserve = 0 if report else RESULT_LIMIT if failure else RESULT_LIMIT + FAILURE_LIMIT
        members_reserved = 0 if report else 1 if failure else 3
        if self.bytes + len(raw) > COMPONENT_LIMIT - reserve or len(self.members) >= MEMBER_LIMIT - members_reserved:
            raise ValueError('component aggregate/member cap including failure/report reserve')
        with (self.root / name).open('xb') as stream: stream.write(raw)
        row = dict(path=name, bytes=len(raw), sha256=sha(raw)); self.bytes += len(raw); self.members.append(row)
        return row


def kernel_resources(compiled):
    """Compiler resource facts of one launched Triton kernel (no ISA bytes are stored)."""
    metadata = getattr(compiled, 'metadata', None)
    value = dict(name=str(getattr(compiled, 'name', '')), n_regs=getattr(compiled, 'n_regs', None),
                 n_spills=getattr(compiled, 'n_spills', None),
                 shared=getattr(metadata, 'shared', None) if metadata is not None else None,
                 num_warps=getattr(metadata, 'num_warps', None) if metadata is not None else None,
                 num_stages=getattr(metadata, 'num_stages', None) if metadata is not None else None)
    return {k: (v if type(v) in (int, str) or v is None else int(v)) for k, v in value.items()}
