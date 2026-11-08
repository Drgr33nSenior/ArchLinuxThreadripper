"""Seven fixed control captures; no framework is imported by this module.

The future owner supplies the authenticated Torch object and installs the scope.
Native profiler start/stop can synchronize internally; there is no extra explicit
device synchronization or tensor read here. Profiled request time is not scored.
"""
from contextlib import nullcontext
import os
from pathlib import Path
import re
import time

from shared_diagnostic import Observer, REPORT_BYTES, canonical, exclusive, integer, need, profiled, sha
from shared_trace_io import RAW_LIMIT, TraceStore, WINDOWS


class Windows:
    """Selection from completed CPU metadata; never adapts to timings or samples."""
    def __init__(self, identity, menu):
        self.start=identity["start"];self.profiled=profiled(identity)
        self.members={case:{r["ordinal"] for r in menu if r["case"]==case and r["repetition"]==0}
                      for case in {w.rsplit("-",1)[0] for w in WINDOWS}}
        self.menu=menu;self.committed={case:0 for case in self.members}
        self.rows={w:{"window":w,"status":"PENDING","sequences":[],"settled_rounds":0} for w in WINDOWS}
        self.active=None;self.last_sequence=-1;self.disabled=False

    def before(self,row):
        if not self.profiled or self.disabled:return None
        if self.active is not None:
            state=self.rows[self.active]
            need(row is not None and row["sequence"]==state["sequences"][-1]+1 and
                 row["case"]==self.active.rsplit("-",1)[0] and row["phase"]=="decode" and
                 row["repetition"]==0 and set(row["members"])==self.members[row["case"]],
                 "capture interrupted or C2 membership changed")
            need(len(state["sequences"])==1,"one second round only")
            state["sequences"].append(row["sequence"])
            return None
        if row is None:return None
        need(row["sequence"]>self.last_sequence,"monotone selection sequence")
        self.last_sequence=row["sequence"]
        case=row["case"]
        if row["repetition"]!=0 or case not in self.members:return None
        phase=row["phase"];window=case+"-"+phase
        if window not in self.rows or self.rows[window]["status"]!="PENDING":return None
        complete=set(row["members"])==self.members[case]
        if not complete:
            if phase=="decode" and self.committed[case]==2:
                self.rows[window]["status"]="INCOMPLETE_MEMBERSHIP_AT_TRIGGER"
            return None
        if phase=="prefill":
            idx=row["members"][0];length=self.menu[idx]["input_tokens"]
            frontier=row["prefix_lens"][0]+row["extend_lens"][0]
            need(frontier<=length,"prefill chunk extends beyond prompt")
            if frontier<(length+1)//2:return None
        else:
            if self.committed[case]<2:return None
            need(self.committed[case]==2,"missed fixed decode trigger")
        state=self.rows[window]
        state.update(status="SELECTED",sequences=[row["sequence"]],settled_rounds=self.committed[case])
        self.active=window
        return window

    def after(self,row):
        if not self.profiled or self.disabled:return None
        case=row["case"]
        if self.active is not None:
            window=self.active;state=self.rows[window]
            need(row["sequence"]==state["sequences"][-1],"completed capture row identity")
            if row["phase"]=="decode":
                need(row["can_run_cuda_graph"] is True,"selected decode graphs required")
                need(all(a["output"]>b["output"] and not a["finished"]
                         for a,b in zip(row["after"],row["before"],strict=True)),
                     "both requests must commit and stay active through capture")
            else:need(row["can_run_cuda_graph"] is False,"selected eager prefill required")
            if row["phase"]=="prefill" or len(state["sequences"])==2:
                state["status"]="ROUNDS_OBSERVED";self.active=None;return window
            return None
        if row["phase"]=="decode" and row["repetition"]==0 and case in self.members:
            state=self.rows[case+"-decode"]
            if state["status"]!="PENDING":return None
            if any(a["finished"] for a in row["after"]):
                state["status"]="INCOMPLETE_EARLY_FINISH";return None
            if set(row["members"])==self.members[case]:
                need(all(a["output"]>b["output"] for a,b in zip(row["after"],row["before"],strict=True)),
                     "settling round must commit for every member")
                self.committed[case]+=1
        return None

    def abort(self):
        if self.active is not None:self.rows[self.active]["status"]="INCOMPLETE_CAPTURE"
        if self.profiled:
            for row in self.rows.values():
                if row["status"]=="PENDING":row["status"]="NOT_ATTEMPTED_AFTER_CAPTURE_FAILURE"
        self.active=None;self.disabled=True

    def finish(self):
        if self.active is not None:self.abort()
        for row in self.rows.values():
            if row["status"]=="PENDING":row["status"]="MISSING" if self.profiled else "NOT_REQUESTED"
        return [dict(row) for row in self.rows.values()]


def mount_size(mountinfo,path):
    """Parse a fixed Linux mount point; caller must also check the live filesystem."""
    need(type(mountinfo) is str and len(mountinfo.encode())<=128<<10,"mount inventory bound")
    matches=[]
    for line in mountinfo.splitlines():
        fields=line.split();
        if len(fields)<10 or fields[4]!=str(path):continue
        separator=fields.index("-")
        need(separator>=6 and len(fields)>separator+3 and fields[separator+1]=="tmpfs","dedicated tmpfs required")
        options=fields[separator+3].split(",")
        sizes=[x[5:] for x in options if x.startswith("size=")]
        need(len(sizes)==1,"one tmpfs size")
        match=re.fullmatch(r"([0-9]+)([kKmMgG]?)",sizes[0]);need(match is not None,"tmpfs size syntax")
        amount=int(match[1])*(1024**{"":0,"k":1,"m":2,"g":3}[match[2].lower()])
        matches.append(amount)
    need(matches==[RAW_LIMIT],"one exact 32-MiB raw mount")
    return matches[0]


def verify_raw_mount(path,rank):
    integer(rank,0,1);path=Path(path)
    need(path==Path(f"/mnt/shared-trace-raw-rank-{rank}") and path.resolve()==path and
         path.is_dir() and path.is_mount() and not list(path.iterdir()),"fresh fixed raw mount")
    with open("/proc/self/mountinfo","rb") as stream:raw=stream.read((128<<10)+1)
    need(len(raw)<=128<<10,"mount inventory bound")
    size=mount_size(raw.decode(),path);stats=os.statvfs(path)
    need(stats.f_blocks*stats.f_frsize==size,"live tmpfs capacity")
    return {"path":str(path),"fstype":"tmpfs","capacity_bytes":size,"verified":True}


class KinetoBackend:
    """Concrete profiler API adapter; no native call runs until supplied Torch is used."""
    def __init__(self,torch,raw_directory,directory,identity):
        need(torch.__version__=="2.14.0+git2e66fe81.rocm10.2.0" and
             torch.version.hip=="7.17.26416","pinned Torch/HIP version")
        self.raw=Path(raw_directory);self.mount=verify_raw_mount(self.raw,identity["rank"])
        self.torch=torch;self.store=TraceStore(directory,identity["rank"])
        self.profile=None;self.window=None;self.start_attempted=False;self.stop_failed=False
        self.start_returned_ns=None
        need(torch.autograd.kineto_available() and
             torch.profiler.ProfilerActivity.CUDA in torch.profiler.supported_activities(),"GPU Kineto capability")

    def begin(self,window):
        need(self.profile is None and window in WINDOWS,"one active fixed capture")
        # Profile construction happens once per selected window. Cleanup never restarts it.
        self.window=window;self.start_attempted=False
        self.profile=self.torch.profiler.profile(
            activities=[self.torch.profiler.ProfilerActivity.CPU,self.torch.profiler.ProfilerActivity.CUDA],
            record_shapes=True,with_stack=False,profile_memory=False)
        self.start_attempted=True;self.profile.start()
        # Host CLOCK_MONOTONIC after start returns: bounds round 1's marker begin.
        self.start_returned_ns=time.monotonic_ns()

    def annotation(self,name):
        return self.torch.profiler.record_function(name) if self.profile is not None else nullcontext()

    def end(self,usable):
        profile=self.profile;window=self.window;started=self.start_returned_ns
        self.profile=None;self.window=None;self.start_returned_ns=None
        need(profile is not None and self.start_attempted,"capture must have attempted start")
        self.start_attempted=False
        # stop() can synchronize internally. Do not add torch.cuda.synchronize().
        try:profile.stop()
        except BaseException:
            # The installed profiler synchronizes before disabling Kineto. A
            # failed stop leaves its process-wide state unresolved. Do not run
            # further model batches or retry stop in this worker.
            self.stop_failed=True
            raise
        if not usable:
            # An aborted window is unusable. Do not delay the original failure
            # with a raw export or compression; the capture report retains it.
            return {"window":window,"status":"INCOMPLETE","transport":None,
                    "window_observation_complete":False,"producer_hard_bound_verified":True,
                    "profiler_export_error":None,"export_skipped":"unusable_window",
                    "profile_start_returned_ns":started}
        target_dir=self.raw/window;target_dir.mkdir(mode=0o700)
        source=target_dir/"trace.json";export_error=None;row=None
        try:
            try:profile.export_chrome_trace(str(source))
            except Exception as error:export_error=type(error).__name__
            row=self.store.pack(source,window)
            return {"window":window,"status":"COMPLETE" if usable and export_error is None and row["status"]=="COMPLETE" else "INCOMPLETE",
                    "transport":row,"window_observation_complete":usable,
                    "producer_hard_bound_verified":True,"profiler_export_error":export_error,
                    "profile_start_returned_ns":started}
        finally:
            # Only this invocation's raw export is temporary; retained compressed files stay.
            if source.is_file() and not source.is_symlink() and source.stat().st_nlink==1:source.unlink()
            if not list(target_dir.iterdir()):target_dir.rmdir()


class CaptureObserver(Observer):
    """One shared scheduler wrapper for scalar observation and fixed profiling."""
    def __init__(self,directory,identity,menu,backend=None,**kwargs):
        need((not profiled(identity) and backend is None) or (profiled(identity) and backend is not None),
             "backend only on profiled start")
        if backend is not None:
            need(backend.store.directory==Path(directory),"capture and trace output directory must match")
        self.windows=Windows(identity,menu);self.backend=backend;self.capture_errors=[];self.exports=[]
        self.active_capture=False;self.capture_failed=False;self.capture_sha256=None;self.close_receipt=None
        super().__init__(directory,identity,menu,annotate=self.annotation,**kwargs)

    def annotation(self,name):
        return self.backend.annotation(name) if self.active_capture else nullcontext()

    def require_profiler_closed(self):
        if self.backend is not None and self.backend.stop_failed:
            raise RuntimeError("shared diagnostic: profiler stop unresolved; worker must exit")

    def run(self,*args,**kwargs):
        self.require_profiler_closed()
        answer=super().run(*args,**kwargs)
        self.require_profiler_closed()
        return answer

    def process(self,*args,**kwargs):
        self.require_profiler_closed()
        answer=super().process(*args,**kwargs)
        self.require_profiler_closed()
        return answer

    def capture_failure(self,where,error,preserve_original=False):
        self.capture_failed=True;self.windows.abort()
        if len(self.capture_errors)<16:self.capture_errors.append({"where":where,"type":type(error).__name__})
        if self.active_capture:
            self.active_capture=False
            try:self.exports.append(self.backend.end(False))
            except BaseException as cleanup:
                if len(self.capture_errors)<16:self.capture_errors.append({"where":"abort-stop","type":type(cleanup).__name__})
                if not preserve_original and not isinstance(cleanup,Exception):raise

    def failure(self,where,error):
        super().failure(where,error)
        self.capture_failure("observer-"+where,error,preserve_original=where in ("model","result-processing"))

    def begin(self,batch):
        row=super().begin(batch)
        if not self.capture_failed:
            try:
                window=self.windows.before(row)
                if window is not None:
                    self.active_capture=True
                    self.backend.begin(window)
            except Exception as error:self.capture_failure("select-or-start",error)
        return row

    def complete(self,batch,result,row):
        super().complete(batch,result,row)
        if not self.capture_failed:
            try:
                window=self.windows.after(row)
                if window is not None:
                    self.active_capture=False
                    exported=self.backend.end(True);self.exports.append(exported)
                    need(exported["status"]=="COMPLETE","bounded profile export failed")
            except Exception as error:self.capture_failure("complete-or-export",error)

    def close(self,restored):
        if self.active_capture:self.capture_failure("unfinished-window",ValueError())
        core_sha=super().close(restored)
        rows=self.windows.finish()
        exported={r["window"]:r for r in self.exports}
        complete=(profiled(self.identity) and not self.capture_errors and not self.errors and restored is True and
                  len(exported)==7 and all(r["status"]=="ROUNDS_OBSERVED" for r in rows) and
                  all(r["status"]=="COMPLETE" for r in exported.values()))
        report={"schema":1,"identity":self.identity,"core_summary_sha256":core_sha,"windows":rows,
                "exports":self.exports,"errors":self.capture_errors,
                "status":"CAPTURE_EXPORT_COMPLETE" if complete else "NOT_PROFILED" if not profiled(self.identity) else "INCOMPLETE",
                "raw_mount":None if self.backend is None else self.backend.mount,
                "profiler_internal_synchronization_possible":True,"added_explicit_device_syncs":0,
                "trace_join_verified":False,"diagnostic_success":False,"gpu_authority":False}
        raw=canonical(report);need(len(raw)<=REPORT_BYTES,"capture report bound")
        write_error=None
        try:
            exclusive(self.journal.directory/f"CAPTURE-rank-{self.identity['rank']}.json",raw)
            self.capture_sha256=sha(raw)
        except Exception as error:
            write_error=type(error).__name__
        self.close_receipt={"schema":1,"identity":self.identity,"core_summary_sha256":core_sha,
                            "capture_sha256":self.capture_sha256,
                            "capture_status":report["status"] if write_error is None else "INCOMPLETE",
                            "capture_write_error":write_error}
        # The owner must publish this returned receipt in its authenticated
        # closeout. A failed capture write still hands back the completed core pin.
        return self.close_receipt
