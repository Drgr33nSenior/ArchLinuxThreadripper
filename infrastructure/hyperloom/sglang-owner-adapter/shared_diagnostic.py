"""Bounded CPU observation of the selected non-overlap scheduler.

No framework import, device read, model call or profiler activation lives here.
The owner must bind the menu, worker identity and source closure before install.
"""
from array import array
from contextlib import contextmanager
import hashlib
import json
import os
import stat
from pathlib import Path
import time

CHUNK_BYTES = 1 << 20
# Four rank/start streams share the SPEC's 16-MiB metadata partition. Each
# receives 4 MiB, of which 256 KiB is reserved for capture/branch inventory.
RANK_BYTES = (4 << 20) - (256 << 10)
REPORT_BYTES = 64 << 10
MAX_BATCHES = 65536
CASES = ("long-c1-cold","long-c1-warm","long-c2-cold","long-c2-warm",
         "short-c1-cold","short-c2-cold")
# Only these static predicate names may enter a failure receipt. Native error
# messages, request data and paths are never copied into retained diagnostics.
FAILURE_CHECKS = frozenset({
    "bounded integer", "CPU output IDs container", "visible output extent",
    "selected non-overlap scheduler", "non-overlap pending batch", "native request list",
    "mixed or oversized selected batch", "duplicate member", "mixed workload groups",
    "cache namespace", "finished request resubmitted", "native prompt IDs", "prompt identity",
    "unsupported forward mode", "host prefill extents", "prefill chunk extent",
    "prefill extent/order", "contiguous prefill coverage", "cold prefill starts at zero",
    "complete prefill before decode", "batch count", "output changed outside observed result",
    "foreign process while selected result pending", "run/result identity", "result membership",
    "host clock order", "graph replay flag", "existing CPU draft counters",
    "selected DFlash width", "bounded output progress",
})


def need(ok, message):
    if not ok:
        raise ValueError("shared diagnostic: " + message)


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(",",":"),allow_nan=False).encode()+b"\n"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def integer(value, low=0, high=1 << 63):
    need(type(value) is int and low <= value <= high,"bounded integer")
    return value


def hex64(value):
    need(type(value) is str and len(value)==64 and all(c in "0123456789abcdef" for c in value),"sha256")
    return value


IDENTITY_FIELDS = frozenset({"schema","rank","start","pid","session_sha256","source_manifest_sha256",
                             "worker_identity_sha256","nonce","control","power_cap_watts"})
# Matched baseline/candidate profiling (schema 2): both starts are profiled.
MATCHED_ROLES = {"control": [], "candidate": ["gatednorm_quant","gate_up_wave","verify_prefetch"]}
# Frozen op227 reversed order: candidate start 1, control start 2. identity_check is its one authority.
MATCHED_START = {"candidate": 1, "control": 2}


def identity_check(identity):
    matched = type(identity) is dict and type(identity.get("schema")) is int and identity["schema"]==2
    need(set(identity)==(IDENTITY_FIELDS|{"role","members","profiled"} if matched else IDENTITY_FIELDS),
         "identity fields")
    if matched:
        need(identity["role"] in MATCHED_ROLES and identity["members"]==MATCHED_ROLES[identity["role"]]
             and identity["control"] is (identity["role"]=="control") and identity["profiled"] is True
             and identity["start"]==MATCHED_START[identity["role"]],
             "matched profiling identity")
    else:
        need(type(identity["schema"]) is int and identity["schema"]==1 and identity["control"] is True,"control-only identity")
    integer(identity["rank"],0,1);integer(identity["start"],1,2);integer(identity["pid"],1)
    integer(identity["power_cap_watts"],235,235)
    for name in ("session_sha256","source_manifest_sha256","worker_identity_sha256"):
        hex64(identity[name])
    need(type(identity["nonce"]) is str and len(identity["nonce"])==16 and
         all(c in "0123456789abcdef" for c in identity["nonce"]),"nonce")


def profiled(identity):
    """Schema 1 profiles start 2 only; matched schema 2 profiles both starts."""
    return identity["profiled"] is True if identity["schema"]==2 else identity["start"]==2


def menu_check(menu):
    need(type(menu) is list and len(menu)==30,"frozen thirty-request menu")
    names, groups = set(), {}
    for i,row in enumerate(menu):
        need(set(row)=={"ordinal","rid","cache_salt","input_tokens","input_ids_sha256",
                        "body_sha256","case","repetition","concurrency"},"menu fields")
        need(type(row["ordinal"]) is int and row["ordinal"]==i,"ordinal")
        for key in ("rid","cache_salt"):
            need(type(row[key]) is str and 0<len(row[key])<=128,"bounded request identity")
        need(row["rid"] not in names,"duplicate rid");names.add(row["rid"])
        integer(row["input_tokens"],1,131040)
        hex64(row["input_ids_sha256"]);hex64(row["body_sha256"])
        integer(row["repetition"],0,2);integer(row["concurrency"],1,2)
        need(row["case"] in CASES+("warmup-c1","warmup-c2"),"case")
        need(row["concurrency"]==(2 if "-c2" in row["case"] else 1),"case concurrency")
        groups.setdefault((row["case"],row["repetition"]),[]).append(row)
    expected={(c,r) for c in CASES for r in range(3)}|{("warmup-c1",0),("warmup-c2",0)}
    need(set(groups)==expected,"complete scored and warmup groups")
    need(all(len(g)==g[0]["concurrency"] for g in groups.values()),"complete group members")


def exclusive(path, raw):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,"wb") as f:
        f.write(raw);f.flush();os.fsync(f.fileno())


class Journal:
    def __init__(self,directory,rank):
        self.directory=Path(directory)
        need(self.directory.is_dir() and self.directory.resolve()==self.directory,"canonical output directory")
        self.prefix=f"OBS-rank-{integer(rank,0,1)}"
        need(not list(self.directory.glob(self.prefix+"*")),"exclusive observation output")
        self.parts=[];self.stream=None;self.used=0;self.size=0;self.digest=None
        self.closed=False

    def _finish_part(self):
        if self.stream is not None:
            self.stream.flush();os.fsync(self.stream.fileno());self.stream.close()
            self.parts.append({"file":self.name,"bytes":self.size,"sha256":self.digest.hexdigest()})
            self.stream=None

    def append(self,event):
        need(not self.closed,"journal closed")
        raw=canonical(event)
        need(len(raw)<=REPORT_BYTES,"bounded event")
        need(self.used+len(raw)<=RANK_BYTES-REPORT_BYTES,"rank metadata budget")
        if self.stream is None or self.size+len(raw)>CHUNK_BYTES:
            self._finish_part()
            self.name=f"{self.prefix}-chunk-{len(self.parts):03d}.jsonl"
            fd=os.open(self.directory/self.name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            self.stream=os.fdopen(fd,"wb");self.size=0;self.digest=hashlib.sha256()
        self.stream.write(raw);self.stream.flush()
        self.digest.update(raw);self.size+=len(raw);self.used+=len(raw)

    def finish(self,summary):
        need(not self.closed,"single-use finish")
        self.closed=True
        try:self._finish_part()
        finally:
            if self.stream is not None:self.stream.close();self.stream=None
        summary=dict(summary,parts=self.parts,journal_bytes=self.used)
        raw=canonical(summary);need(len(raw)<=REPORT_BYTES,"summary bound")
        exclusive(self.directory/(self.prefix+".json"),raw)
        return sha(raw)


def request_state(req):
    output=req.output_ids
    # The pinned Req constructor uses array('q'); inspect its length in place.
    # Do not coerce arbitrary containers or materialize any device tensor.
    need(type(output) is list or (type(output) is array and output.typecode=="q"),
         "CPU output IDs container")
    visible=getattr(req,"finished_len",None)
    if visible is None:visible=len(output)
    integer(visible,0,4096);need(visible<=len(output),"visible output extent")
    finished=getattr(req,"finished_reason",None) is not None
    kv=getattr(getattr(req,"kv",None),"kv_committed_len",None)
    if kv is not None:integer(kv,0,262144)
    return {"output":visible,"finished":finished,"kv_committed_len":kv}


class Observer:
    def __init__(self,directory,identity,menu,clock=time.monotonic_ns,annotate=None):
        identity_check(identity);menu_check(menu)
        self.identity=json.loads(canonical(identity));self.menu=json.loads(canonical(menu))
        self.by_rid={r["rid"]:r for r in self.menu};self.clock=clock
        self.annotate=annotate
        self.journal=Journal(directory,identity["rank"])
        self.errors=[];self.disabled=False;self.pending=None;self.sequence=0
        self.seen=set();self.finished=set();self.closed=False;self.summary_sha256=None
        self.last_output={};self.prefill_end={};self.decode_seen=set()
        self.journal.append({"kind":"header","identity":self.identity,"menu":self.menu,
                             "extra_device_reads":0,"timing_claim":False})

    def failure(self,where,error):
        self.disabled=True;self.pending=None
        if len(self.errors)<16:
            row={"where":where,"type":type(error).__name__}
            if type(error) is ValueError and len(error.args)==1 and type(error.args[0]) is str:
                for check in FAILURE_CHECKS:
                    if error.args[0]=="shared diagnostic: "+check:
                        row.update(check=check,sequence=self.sequence)
                        break
            self.errors.append(row)

    def begin(self,batch):
        need(self.pending is None,"non-overlap pending batch")
        reqs=batch.reqs
        need(type(reqs) is list,"native request list")
        selected=[r for r in reqs if getattr(r,"rid",None) in self.by_rid]
        if not selected:return None
        need(len(selected)==len(reqs) and 1<=len(reqs)<=2,"mixed or oversized selected batch")
        rows=[self.by_rid[r.rid] for r in reqs]
        members=[r["ordinal"] for r in rows]
        need(len(set(members))==len(members),"duplicate member")
        group={(r["case"],r["repetition"]) for r in rows}
        need(len(group)==1,"mixed workload groups")
        for req,row in zip(reqs,rows):
            need(req.cache_salt==row["cache_salt"],"cache namespace")
            need(row["ordinal"] not in self.finished,"finished request resubmitted")
            if req.rid not in self.seen:
                ids=req.origin_input_ids
                need(isinstance(ids,(list,array)) and len(ids)==row["input_tokens"] and
                     all(type(n) is int and n>=0 for n in ids),"native prompt IDs")
                need(sha(canonical(list(ids)))==row["input_ids_sha256"],"prompt identity")
                self.seen.add(req.rid)
        phase="prefill" if batch.forward_mode.is_extend() else "decode" if batch.forward_mode.is_decode() else None
        need(phase is not None,"unsupported forward mode")
        if phase=="prefill":
            starts,counts=batch.prefix_lens,batch.extend_lens
            need(type(starts) is list and type(counts) is list and len(starts)==len(counts)==len(reqs),"host prefill extents")
            for x in starts:integer(x,0,262144)
            for x in counts:integer(x,1,2050)
            need(sum(counts)==batch.extend_num_tokens and sum(counts)<=2050,"prefill chunk extent")
            for idx,start,count in zip(members,starts,counts,strict=True):
                need(idx not in self.decode_seen and start+count<=self.menu[idx]["input_tokens"],"prefill extent/order")
                if idx in self.prefill_end:need(start==self.prefill_end[idx],"contiguous prefill coverage")
                elif "-warm" not in self.menu[idx]["case"]:need(start==0,"cold prefill starts at zero")
        else:starts=counts=None
        if phase=="decode":
            need(all(self.prefill_end.get(idx)==self.menu[idx]["input_tokens"] for idx in members),"complete prefill before decode")
        need(self.sequence<MAX_BATCHES,"batch count")
        states=[request_state(r) for r in reqs]
        for idx,state in zip(members,states):
            need(state["output"]==self.last_output.get(idx,0),"output changed outside observed result")
        row={"kind":"batch","sequence":self.sequence,"phase":phase,
             "case":rows[0]["case"],"repetition":rows[0]["repetition"],"members":members,
             "before":states,"prefix_lens":None if starts is None else list(starts),
             "extend_lens":None if counts is None else list(counts),"host_ns":[integer(self.clock())]}
        self.sequence+=1
        return row

    def run(self,original,scheduler,batch,*args,**kwargs):
        token=None
        if not self.disabled:
            try:
                need(scheduler.enable_overlap is False,"selected non-overlap scheduler")
                token=self.begin(batch)
            except Exception as e:self.failure("begin",e)
        marker=None
        if token is not None and self.annotate is not None and not self.disabled:
            try:
                marker=self.annotate(f"OBS:{self.identity['nonce']}:{token['sequence']}")
                marker.__enter__()
            except Exception as e:
                marker=None;self.failure("annotation-enter",e)
        try:
            result=original(scheduler,batch,*args,**kwargs)
        except BaseException as e:
            # Close the active annotation before stopping its profiler.
            # Cleanup interrupts must not replace the native exception.
            if marker is not None:
                try:marker.__exit__(type(e),e,e.__traceback__)
                except BaseException as cleanup:
                    try:self.failure("annotation-exit",cleanup)
                    except BaseException:pass
            try:self.failure("model",e)
            except BaseException:pass
            raise
        if marker is not None:
            try:marker.__exit__(None,None,None)
            except Exception as e:self.failure("annotation-exit",e)
        if token is not None and not self.disabled:
            try:
                token["host_ns"].append(integer(self.clock()))
                self.pending=(batch,result,token)
            except Exception as e:self.failure("after-run",e)
        return result

    def process(self,original,scheduler,batch,result,*args,**kwargs):
        pending=self.pending
        token=None
        if not self.disabled:
            try:
                if pending is not None:
                    need(pending[0] is batch and pending[1] is result,"foreign process while selected result pending")
                if any(getattr(r,"rid",None) in self.by_rid for r in batch.reqs):
                    need(pending is not None and pending[0] is batch and pending[1] is result,"run/result identity")
                    token=pending[2];token["host_ns"].append(integer(self.clock()))
            except Exception as e:self.failure("before-process",e)
        try:
            answer=original(scheduler,batch,result,*args,**kwargs)
        except BaseException as e:
            try:self.failure("result-processing",e)
            except BaseException:pass
            raise
        if token is not None and not self.disabled:
            try:self.complete(batch,result,token)
            except Exception as e:self.failure("after-process",e)
        self.pending=None
        return answer

    def complete(self,batch,result,token):
        need([self.by_rid[r.rid]["ordinal"] for r in batch.reqs]==token["members"],"result membership")
        token["host_ns"].append(integer(self.clock()))
        need(token["host_ns"]==sorted(token["host_ns"]),"host clock order")
        token["after"]=[request_state(r) for r in batch.reqs]
        graph=getattr(result,"can_run_cuda_graph",None)
        need(type(graph) is bool,"graph replay flag")
        token["can_run_cuda_graph"]=graph
        correct=getattr(result,"num_correct_drafts_per_req_cpu",None)
        if token["phase"]=="decode":
            need(type(correct) is list and len(correct)==len(batch.reqs),"existing CPU draft counters")
            for count in correct:integer(count,0,8)
            token["correct_drafts_cpu"]=list(correct)
            width=getattr(result,"speculative_num_draft_tokens",None)
            need(type(width) is int and width==8,"selected DFlash width")
            token["verify_width"]=width
        else:token["correct_drafts_cpu"]=None;token["verify_width"]=None
        for idx,before,after in zip(token["members"],token["before"],token["after"]):
            need(before["output"]<=after["output"]<=before["output"]+8,"bounded output progress")
            self.last_output[idx]=after["output"]
            if after["finished"]:self.finished.add(idx)
        self.journal.append(token)
        if token["phase"]=="prefill":
            for idx,start,count in zip(token["members"],token["prefix_lens"],token["extend_lens"],strict=True):
                self.prefill_end[idx]=start+count
        else:self.decode_seen.update(token["members"])

    def close(self,restored):
        need(not self.closed,"single-use observer")
        self.closed=True;self.disabled=True
        if self.pending is not None:self.failure("pending-at-close",ValueError())
        complete=(restored is True and not self.errors and len(self.finished)==30 and len(self.seen)==30 and
                  all(self.prefill_end.get(i)==r["input_tokens"] for i,r in enumerate(self.menu)))
        summary={"schema":1,"identity":self.identity,"menu_sha256":sha(canonical(self.menu)),
                 "status":"COMPLETE" if complete else "INCOMPLETE","errors":self.errors,
                 "restored":restored,"finished":sorted(self.finished),"batches":self.sequence,
                 "extra_device_reads":0,"gpu_authority":False,"timing_claim":False}
        self.summary_sha256=self.journal.finish(summary)
        return self.summary_sha256


def install(observer,scheduler_type,expected_originals):
    """Install two explicitly supplied original callables; owner source audit is separate."""
    names=("run_batch","process_batch_result")
    need(set(expected_originals)==set(names),"exact hooks")
    for name in names:
        need(scheduler_type.__dict__.get(name) is expected_originals[name],"hook identity before install")
    need(not hasattr(scheduler_type,"_shared_observation_installed"),"hook already installed")
    original_run=expected_originals["run_batch"];original_process=expected_originals["process_batch_result"]
    def run(instance,batch,*args,**kwargs):
        return observer.run(original_run,instance,batch,*args,**kwargs)
    def process(instance,batch,result,*args,**kwargs):
        return observer.process(original_process,instance,batch,result,*args,**kwargs)
    replacements={"run_batch":run,"process_batch_result":process}
    applied=[];marker=object()
    try:
        for name in names:setattr(scheduler_type,name,replacements[name]);applied.append(name)
        scheduler_type._shared_observation_installed=marker
    except BaseException:
        for name in reversed(applied):
            if scheduler_type.__dict__.get(name) is replacements[name]:
                setattr(scheduler_type,name,expected_originals[name])
        observer.failure("partial-install",ValueError())
        try:observer.close(False)
        except Exception:pass
        raise
    done=False;summary_sha256=None
    def restore():
        nonlocal done,summary_sha256
        if done:return summary_sha256
        done=True;conflicts=[]
        for name in reversed(names):
            if scheduler_type.__dict__.get(name) is replacements[name]:
                setattr(scheduler_type,name,expected_originals[name])
            else:conflicts.append(name)
        if scheduler_type.__dict__.get("_shared_observation_installed") is marker:
            delattr(scheduler_type,"_shared_observation_installed")
        else:conflicts.append("marker")
        if conflicts:observer.failure("foreign-hook",ValueError())
        summary_sha256=observer.close(not conflicts)
        need(not conflicts,"foreign hook retained; other owned hooks restored")
        return summary_sha256
    return restore


@contextmanager
def scope(observer,scheduler_type,expected_originals):
    restore=install(observer,scheduler_type,expected_originals)
    try:yield observer
    except BaseException:
        try:restore()
        except BaseException:pass  # Keep the original model/interrupt exception.
        raise
    else:restore()


def _json(raw):
    def pairs(values):
        out={}
        for key,value in values:
            need(key not in out,"duplicate JSON key");out[key]=value
        return out
    return json.loads(raw,object_pairs_hook=pairs,
                      parse_constant=lambda _:need(False,"nonfinite JSON"))


def _file(directory,name,limit):
    """Read one bounded leaf through a no-follow directory descriptor."""
    directory=Path(directory)
    need(type(name) is str and name not in {"", ".", ".."} and Path(name).name==name
         and directory.is_absolute() and directory.resolve()==directory,
         "canonical artifact leaf")
    integer(limit,1,32 << 20)
    directory_fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        before=os.stat(name,dir_fd=directory_fd,follow_symlinks=False)
        need(stat.S_ISREG(before.st_mode) and before.st_nlink==1 and before.st_size<=limit,
             "bounded regular artifact")
        fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory_fd)
        try:
            initial=os.fstat(fd)
            def stamp(value):
                return (value.st_dev,value.st_ino,value.st_size,value.st_mtime_ns,value.st_ctime_ns,value.st_nlink)
            need(stamp(initial)==stamp(before),"artifact changed before read")
            parts=[];size=0
            while size<=limit:
                block=os.read(fd,min(65536,limit+1-size))
                if not block:break
                parts.append(block);size+=len(block)
            need(size==initial.st_size and size<=limit and stamp(initial)==stamp(os.fstat(fd))
                 and stamp(initial)==stamp(os.stat(name,dir_fd=directory_fd,follow_symlinks=False)),
                 "artifact changed during read")
            current=os.stat(directory,follow_symlinks=False);opened=os.fstat(directory_fd)
            need((current.st_dev,current.st_ino)==(opened.st_dev,opened.st_ino),"artifact directory changed")
            return b"".join(parts)
        finally:os.close(fd)
    finally:os.close(directory_fd)


def read_recording(directory,identity,menu,summary_sha256):
    """Authenticate bounded bytes against owner-supplied identities and digest."""
    identity_check(identity);menu_check(menu);hex64(summary_sha256)
    directory=Path(directory);need(directory.resolve()==directory,"canonical read root")
    prefix=f"OBS-rank-{identity['rank']}"
    raw=_file(directory,prefix+".json",REPORT_BYTES)
    need(sha(raw)==summary_sha256,"summary digest")
    summary=_json(raw)
    need(type(summary["schema"]) is int and summary["schema"]==1,"record schema")
    need(summary["identity"]==identity and summary["menu_sha256"]==sha(canonical(menu)),"record identity")
    need(summary["extra_device_reads"]==0 and summary["gpu_authority"] is False and
         summary["timing_claim"] is False,"record scope")
    records=[];used=0;expected={prefix+".json"}
    # Rotation can leave up to one event's unused space in each completed chunk.
    max_parts=1+(RANK_BYTES-REPORT_BYTES-1)//(CHUNK_BYTES-REPORT_BYTES)
    need(type(summary["parts"]) is list and len(summary["parts"])<=max_parts,"part count")
    for i,part in enumerate(summary["parts"]):
        name=f"{prefix}-chunk-{i:03d}.jsonl"
        need(part["file"]==name,"part order")
        raw=_file(directory,name,CHUNK_BYTES)
        need(len(raw)==part["bytes"] and sha(raw)==part["sha256"],"part digest")
        used+=len(raw);need(used<=RANK_BYTES-REPORT_BYTES,"rank byte budget")
        expected.add(name);records.extend(_json(line) for line in raw.splitlines())
    need({p.name for p in directory.glob(prefix+"*")}==expected,"exact observation artifact membership")
    need(used==summary["journal_bytes"] and records,"journal extent")
    need(records[0]=={"kind":"header","identity":identity,"menu":menu,"extra_device_reads":0,"timing_claim":False},"header")
    batches=records[1:];last={};finished=set();prefill_end={};decode_seen=set()
    need(len(batches)<=MAX_BATCHES,"batch count")
    for i,row in enumerate(batches):
        need(row["kind"]=="batch" and row["sequence"]==i,"batch sequence")
        members=row["members"];need(type(members) is list and len(members) in (1,2) and len(set(members))==len(members),"members")
        for idx in members:integer(idx,0,29)
        need(all(menu[idx]["case"]==row["case"] and menu[idx]["repetition"]==row["repetition"] for idx in members),"case join")
        need(len(row["before"])==len(row["after"])==len(members),"state extents")
        times=row["host_ns"]
        need(type(times) is list and len(times)==4 and times==sorted(times),"host intervals")
        for t in times:integer(t)
        need(type(row["can_run_cuda_graph"]) is bool,"graph flag")
        need(row["phase"] in ("prefill","decode"),"phase")
        if row["phase"]=="decode":
            need(row["verify_width"]==8 and type(row["verify_width"]) is int and
                 type(row["correct_drafts_cpu"]) is list and len(row["correct_drafts_cpu"])==len(members),"decode counters")
            for count in row["correct_drafts_cpu"]:integer(count,0,8)
            need(row["prefix_lens"] is None and row["extend_lens"] is None,"unknown device prefix remains unknown")
            need(all(prefill_end.get(idx)==menu[idx]["input_tokens"] for idx in members),"complete prefill before decode")
            decode_seen.update(members)
        else:
            need(row["verify_width"] is None and row["correct_drafts_cpu"] is None,"prefill counters")
            starts,counts=row["prefix_lens"],row["extend_lens"]
            need(type(starts) is list and type(counts) is list and len(starts)==len(counts)==len(members),"prefill extents")
            for x in starts:integer(x,0,262144)
            for x in counts:integer(x,1,2050)
            need(sum(counts)<=2050,"chunk bound")
            for idx,start,count in zip(members,starts,counts,strict=True):
                need(idx not in decode_seen and start+count<=menu[idx]["input_tokens"],"prefill extent/order")
                if idx in prefill_end:need(start==prefill_end[idx],"contiguous prefill coverage")
                elif "-warm" not in menu[idx]["case"]:need(start==0,"cold prefill starts at zero")
                prefill_end[idx]=start+count
        for idx,before,after in zip(members,row["before"],row["after"]):
            need(idx not in finished,"finished request resubmitted")
            for state in (before,after):
                need(set(state)=={"output","finished","kv_committed_len"},"state fields")
                integer(state["output"],0,4096);need(type(state["finished"]) is bool,"finish flag")
                if state["kv_committed_len"] is not None:integer(state["kv_committed_len"],0,262144)
            need(before["output"]==last.get(idx,0) and before["finished"] is False and
                 before["output"]<=after["output"]<=before["output"]+8,"output continuity")
            last[idx]=after["output"]
            if after["finished"]:finished.add(idx)
    complete=(summary["status"]=="COMPLETE" and summary["restored"] is True and
              summary["errors"]==[] and summary["finished"]==list(range(30)) and
              finished==set(range(30)) and summary["batches"]==len(batches) and
              all(prefill_end.get(i)==r["input_tokens"] for i,r in enumerate(menu)))
    need(summary["status"] in ("COMPLETE","INCOMPLETE"),"status")
    if summary["status"]=="COMPLETE":need(complete,"complete claim mismatch")
    return {"schema":summary["schema"],"status":"COMPLETE" if complete else "INCOMPLETE","identity":identity,
            "batches":batches,"final_output_counts":last,"summary_sha256":summary_sha256,
            "menu_sha256":summary["menu_sha256"],
            "gpu_authority":False,"timing_claim":False}


def join_ranks(left,right):
    """Compare logical work only; never align the ranks' clock values."""
    a,b=left["identity"],right["identity"]
    need(a["rank"]==0 and b["rank"]==1,"two ranks")
    need(type(left["schema"]) is int and type(right["schema"]) is int and left["schema"]==right["schema"]==1,"rank schema")
    need(hex64(left["menu_sha256"])==hex64(right["menu_sha256"]),"rank menu identity")
    keys=("start","nonce","session_sha256","source_manifest_sha256","control","power_cap_watts")
    if a["schema"]==2 or b["schema"]==2:keys+=("schema","role","members","profiled")
    for key in keys:
        need(a.get(key)==b.get(key) and key in a and key in b,"rank session join")
    keys=("sequence","phase","case","repetition","members","prefix_lens","extend_lens",
          "correct_drafts_cpu","verify_width","before","after","can_run_cuda_graph")
    work=lambda x:[{k:r[k] for k in keys} for r in x["batches"]]
    same=work(left)==work(right)
    return {"status":"COMPLETE" if same and left["status"]==right["status"]=="COMPLETE" else "INCOMPLETE",
            "same_logical_work":same,"clocks_aligned":False,"gpu_authority":False,
            "http_join_complete":False,"profile_join_complete":False}
