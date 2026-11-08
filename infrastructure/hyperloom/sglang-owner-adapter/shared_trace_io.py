"""Bounded trace transport and same-process CPU-annotation joins.

Raw profiler production needs the owner's sized tmpfs mounts before use.
These helpers enforce file/stream/export limits; they do not create a GPU owner.
"""
from decimal import Decimal
import hashlib
import math
import os
from pathlib import Path
import zlib

from shared_diagnostic import canonical, exclusive, hex64, integer, need, sha, _json

PREFILL_LIMIT=768 << 10
DECODE_LIMIT=320 << 10
RAW_LIMIT=32 << 20
RANK_TRACE_LIMIT=3584 << 10
WINDOWS=("long-c1-cold-prefill",)+tuple(
    case+"-decode" for case in ("long-c1-cold","long-c1-warm","long-c2-cold",
                                "long-c2-warm","short-c1-cold","short-c2-cold"))


def raw_mount_plan():
    """Inert owner argv addition. Must be reviewed and consumed before capture."""
    return [part for rank in (0,1) for part in
            ("--size",str(RAW_LIMIT),"--tmpfs",f"/mnt/shared-trace-raw-rank-{rank}")]


class TraceStore:
    def __init__(self,directory,rank):
        self.directory=Path(directory);integer(rank,0,1);self.rank=rank
        need(self.directory.is_dir() and self.directory.resolve()==self.directory,"canonical trace directory")
        self.used=0;self.attempted=set()

    def pack(self,raw_path,window):
        need(window in WINDOWS and window not in self.attempted,"one capture per fixed window")
        self.attempted.add(window)
        stem=f"TRACE-rank-{self.rank}-{window}"
        exclusive(self.directory/(stem+".attempt.json"),canonical({"window":window,"rank":self.rank,"retry":False}))
        phase="prefill" if window.endswith("-prefill") else "decode"
        limit=PREFILL_LIMIT if phase=="prefill" else DECODE_LIMIT
        row={"window":window,"rank":self.rank,"phase":phase,"status":"INCOMPLETE",
             "file":stem+".json.gz","compressed_limit":limit,"raw_limit":RAW_LIMIT,
             "producer_hard_bound_verified":False}
        target=self.directory/row["file"]
        raw_path=Path(raw_path);out=None;used=0
        packed_digest=hashlib.sha256()
        try:
            before=raw_path.lstat()
            need(raw_path.resolve()==raw_path and raw_path.is_file() and
                 before.st_nlink==1 and before.st_size<=RAW_LIMIT,"bounded raw trace file")
            fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            out=os.fdopen(fd,"wb")
            compressor=zlib.compressobj(9,zlib.DEFLATED,31)
            raw_bytes=0;raw_digest=hashlib.sha256()
            overflow=False
            def write(packed):
                nonlocal used,overflow
                remaining=min(limit-used,RANK_TRACE_LIMIT-self.used-used)
                data=packed[:max(0,remaining)]
                if data:out.write(data);used+=len(data);packed_digest.update(data)
                if len(data)!=len(packed) or used>=limit:overflow=True
            raw_fd=os.open(raw_path,os.O_RDONLY|os.O_NOFOLLOW)
            with os.fdopen(raw_fd,"rb") as src:
                opened=os.fstat(src.fileno())
                need((opened.st_dev,opened.st_ino,opened.st_size,opened.st_mtime_ns)==
                     (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns),"raw descriptor identity")
                while True:
                    block=src.read(65536)
                    if not block:break
                    raw_bytes+=len(block);need(raw_bytes<=RAW_LIMIT,"raw trace grew past bound")
                    raw_digest.update(block);write(compressor.compress(block))
                    if overflow:break
                if not overflow:write(compressor.flush())
                ended=os.fstat(src.fileno())
                need((opened.st_dev,opened.st_ino,opened.st_size,opened.st_mtime_ns)==
                     (ended.st_dev,ended.st_ino,ended.st_size,ended.st_mtime_ns),"raw descriptor changed")
            after=raw_path.stat()
            need((before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)==
                 (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns),"raw trace changed during export")
            out.flush();os.fsync(out.fileno());out.close();out=None
            row.update(bytes=used,sha256=packed_digest.hexdigest(),raw_bytes=raw_bytes,
                       raw_sha256=raw_digest.hexdigest())
            need(not overflow and used<limit and self.used+used<=RANK_TRACE_LIMIT,"compressed trace overflow")
            need(raw_bytes==before.st_size,"raw extent")
            row["status"]="COMPLETE"
        except Exception as error:
            row["error_type"]=type(error).__name__
        finally:
            if out is not None:out.close()
            self.used+=used
            row.update(bytes=used,sha256=packed_digest.hexdigest())
        # An export COMPLETE is only byte retention, not a usable profile.
        exclusive(self.directory/(stem+".export.json"),canonical(row))
        return row


def unpack(directory,row):
    need(row["status"]=="COMPLETE" and row["window"] in WINDOWS,"complete export required")
    integer(row["rank"],0,1)
    phase="prefill" if row["window"].endswith("-prefill") else "decode"
    need(row["phase"]==phase,"phase")
    limit=PREFILL_LIMIT if phase=="prefill" else DECODE_LIMIT
    name=f"TRACE-rank-{row['rank']}-{row['window']}.json.gz"
    need(row["file"]==name,"trace filename")
    p=Path(directory)/name
    need(p.resolve()==p and p.is_file() and p.stat().st_nlink==1 and
         p.stat().st_size<limit,"compressed file bound")
    packed=p.read_bytes()
    need(len(packed)==row["bytes"] and sha(packed)==hex64(row["sha256"]),"compressed identity")
    decoder=zlib.decompressobj(31);raw=decoder.decompress(packed,RAW_LIMIT+1)
    need(len(raw)<=RAW_LIMIT and decoder.eof and not decoder.unused_data and
         not decoder.unconsumed_tail,"single bounded complete gzip")
    need(len(raw)==row["raw_bytes"] and sha(raw)==hex64(row["raw_sha256"]),"expanded identity")
    return raw


def number(value):
    need(type(value) in (int,float,Decimal) and math.isfinite(value) and value>=0,"trace number")
    return Decimal(str(value))


def join_annotations(raw,identity,batches,window):
    """Join CPU markers to graph API calls; never infer GPU dependence from order."""
    need(window in WINDOWS and type(raw) is bytes and len(raw)<=RAW_LIMIT,"window/raw bound")
    trace=_json(raw)
    info=trace.get("distributedInfo",{})
    need(info.get("rank")==identity["rank"] and info.get("world_size")==2,"trace rank identity")
    events=trace.get("traceEvents")
    need(type(events) is list and 0<len(events)<=250000,"trace event count")
    prefix=f"OBS:{identity['nonce']}:"
    markers={}
    for e in events:
        need(type(e) is dict,"trace event object")
        if e.get("ph")=="X":
            number(e.get("ts"));number(e.get("dur"))
        name=e.get("name","")
        if type(name) is str and name.startswith("OBS:"):
            need(name.startswith(prefix) and e.get("cat")=="user_annotation" and e.get("ph")=="X","marker namespace/schema")
            suffix=name[len(prefix):]
            need(suffix.isascii() and suffix.isdigit() and str(int(suffix))==suffix,"marker sequence")
            sequence=int(suffix)
            need(sequence not in markers and e.get("pid")==identity["pid"] and type(e.get("tid")) is int,"unique same-process marker")
            need(number(e["dur"])>0,"positive marker duration");markers[sequence]=e
    count=1 if window.endswith("-prefill") else 2
    need(len(markers)==count,"fixed complete window marker count")
    sequence_rows={r["sequence"]:r for r in batches}
    need(len(sequence_rows)==len(batches),"unique observation sequence")
    ordered=sorted(markers)
    if count==2:need(ordered[1]==ordered[0]+1,"two adjacent target rounds")
    graph_apis=[(i,e) for i,e in enumerate(events) if e.get("cat")=="cuda_runtime" and
                e.get("name")=="hipGraphLaunch" and e.get("ph")=="X"]
    need(len(graph_apis)<=2*count,"bounded graph API count")
    graph_correlations=[e.get("args",{}).get("correlation") for _,e in graph_apis]
    for c in graph_correlations:integer(c)
    need(len(set(graph_correlations))==len(graph_correlations),"global graph correlation ambiguity")
    for c in graph_correlations:
        owners=[e for e in events if e.get("cat")=="cuda_runtime" and e.get("ph")=="X" and
                e.get("args",{}).get("correlation")==c]
        need(len(owners)==1,"correlation reused by runtime APIs")
    selected=[];owned_api=set()
    for sequence in ordered:
        need(sequence in sequence_rows,"marker missing from observation")
        row=sequence_rows[sequence];marker=markers[sequence]
        phase="prefill" if window.endswith("-prefill") else "decode"
        case=window.rsplit("-",1)[0]
        need(row["phase"]==phase and row["case"]==case and row["repetition"]==0,"window workload join")
        need(row["can_run_cuda_graph"] is (phase=="decode"),"observed graph route contradicts trace")
        need(type(row["members"]) is list and len(row["members"])==(2 if "-c2-" in case else 1)
             and len(set(row["members"]))==len(row["members"]),"window membership")
        if selected:need(set(row["members"])==set(selected[0]["members"]),"adjacent round membership changed")
        begin=number(marker["ts"]);end=begin+number(marker["dur"])
        apis=[]
        for i,e in enumerate(events):
            if e.get("cat")=="cuda_runtime" and e.get("name")=="hipGraphLaunch" and e.get("ph")=="X":
                t=number(e["ts"]);stop=t+number(e["dur"])
                if e.get("pid")==marker["pid"] and e.get("tid")==marker["tid"] and begin<=t and stop<=end:
                    need(i not in owned_api,"API belongs to multiple markers")
                    owned_api.add(i);apis.append(e)
        need(len(apis)==(2 if phase=="decode" else 0),"selected draft/target graph API count")
        correlations=[e.get("args",{}).get("correlation") for e in apis]
        for c in correlations:integer(c)
        need(len(set(correlations))==len(correlations),"graph API correlation ambiguity")
        groups=[]
        for c in correlations:
            kernels=[e for e in events if e.get("cat")=="kernel" and e.get("ph")=="X" and e.get("args",{}).get("correlation")==c]
            need(kernels,"graph API has no recorded kernels")
            need(all(number(e["dur"])>0 for e in kernels),"positive kernel durations")
            devices={e.get("args",{}).get("device") for e in kernels}
            need(len(devices)==1,"one graph device namespace")
            for device in devices:integer(device)
            groups.append({"correlation":c,"kernels":len(kernels),"device":next(iter(devices))})
        selected.append({"sequence":sequence,"members":row["members"],"groups":groups})
    need(len(owned_api)==len(graph_apis),"graph API outside fixed window annotations")
    return {"status":"JOINED_CPU_GRAPH_API_ONLY","window":window,"rounds":selected,
            "gpu_dependency_established":False,"critical_path_established":False,
            "gpu_authority":False,"timing_claim":False}
