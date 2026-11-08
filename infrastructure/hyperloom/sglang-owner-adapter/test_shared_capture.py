"""Exercise the real capture controller/observer with an inert profiler API."""
from contextlib import contextmanager
import copy
import itertools
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parent))
import shared_capture as c
import shared_diagnostic as d
import shared_trace_io as t
from test_shared_diagnostic import identity,menu,batch,Mode,originals,scheduler_class,drive as drive_core


class FakeTorch:
    __version__="2.14.0+git2e66fe81.rocm10.2.0"
    version=NS(hip="7.17.26416")
    def __init__(self,fail=None,rank=0):
        self.events=[];self.active=None;self.fail=fail;self.time=0;self.profiles=[];self.rank=rank
        self.autograd=NS(kineto_available=lambda:True)
        self.cuda=NS(synchronize=lambda:(_ for _ in ()).throw(AssertionError("extra synchronization")))
        self.profiler=NS(ProfilerActivity=NS(CPU="cpu",CUDA="cuda"),supported_activities=lambda:{"cpu","cuda"},
                         profile=self.create,record_function=self.annotation)
    def create(self,**kw):
        assert kw==dict(activities=["cpu","cuda"],record_shapes=True,with_stack=False,profile_memory=False)
        owner=self
        class Profile:
            def __init__(self):self.trace=[]
            def start(self):
                owner.events.append("start");owner.active=self
                if owner.fail=="start":raise RuntimeError("start failed")
            def stop(self):
                owner.events.append("stop");owner.active=None
                if owner.fail=="stop":raise RuntimeError("stop failed")
                if owner.fail=="stop-interrupt":raise KeyboardInterrupt("cleanup interrupted")
            def export_chrome_trace(self,path):
                owner.events.append("export")
                Path(path).write_bytes(d.canonical(dict(distributedInfo=dict(rank=owner.rank,world_size=2),traceEvents=self.trace)))
                if owner.fail=="export":raise RuntimeError("partial export")
        profile=Profile();self.profiles.append(profile);return profile
    @contextmanager
    def annotation(self,name):
        profile=self.active;start=self.time;self.time+=10
        try:yield
        finally:
            self.time+=10
            self.events.append("annotation-exit")
            profile.trace.append(dict(name=name,cat="user_annotation",ph="X",pid=100+self.rank,tid=5,ts=start,dur=self.time-start))
    def dispatch(self,decode):
        if self.active is None:return
        if decode:
            for _ in range(2):
                self.time+=10;correlation=self.time
                self.active.trace.extend([
                    dict(name="hipGraphLaunch",cat="cuda_runtime",ph="X",pid=100+self.rank,tid=5,ts=self.time,dur=1,args={"correlation":correlation}),
                    dict(name="kernel",cat="kernel",ph="X",pid=0,tid=7,ts=self.time+1000,dur=7,args={"correlation":correlation,"device":0,"stream":0})])
        else:self.active.trace.append(dict(name="prefill",cat="kernel",ph="X",pid=0,tid=7,ts=self.time+1000,dur=50,args={"device":0,"stream":0,"correlation":999}))
        self.time+=10


class TestCapture(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve();self.raw=self.root/"raw";self.raw.mkdir()
        self.menu=menu();self.identity=identity()
    def tearDown(self):self.tmp.cleanup()
    def backend(self,torch):
        proof={"verified":True,"fstype":"tmpfs","capacity_bytes":32<<20,"path":str(self.raw),"fixture":True}
        with patch.object(c,"verify_raw_mount",return_value=proof):
            return c.KinetoBackend(torch,self.raw,self.root,self.identity)
    def observer(self,torch=None,start=2):
        ident=dict(self.identity,start=start)
        return c.CaptureObserver(self.root,ident,self.menu,self.backend(torch) if start==2 else None,
                                 clock=itertools.count(1).__next__)
    def drive(self,o,torch=None,finish_round=4):
        cls=scheduler_class();native=cls.run_batch
        def run(instance,b,*args,**kw):
            if torch is not None:torch.dispatch(b.forward_mode.is_decode())
            return native(instance,b,*args,**kw)
        cls.run_batch=run;s=cls();groups={}
        for r in self.menu:groups.setdefault((r["case"],r["repetition"]),[]).append(r)
        with d.scope(o,cls,originals(cls)):
            for group in groups.values():
                b=batch(group);s.process_batch_result(b,s.run_batch(b));b.forward_mode=Mode("decode")
                for turn in range(5):
                    b.finish=turn==finish_round;s.process_batch_result(b,s.run_batch(b))
                    if b.finish:break
        return s
    def report(self):return json.loads((self.root/"CAPTURE-rank-0.json").read_bytes())

    def test_seven_fixed_windows_integrate_and_join_real_core_records(self):
        torch=FakeTorch();o=self.observer(torch);s=self.drive(o,torch)
        self.assertEqual((s.runs,s.processes),(120,120))
        self.assertEqual(torch.events.count("start"),7);self.assertEqual(torch.events.count("stop"),7)
        report=self.report();self.assertEqual(report["status"],"CAPTURE_EXPORT_COMPLETE")
        self.assertEqual(d.sha((self.root/"CAPTURE-rank-0.json").read_bytes()),o.capture_sha256)
        obs=d.read_recording(self.root,self.identity,self.menu,o.summary_sha256)
        self.assertEqual(obs["status"],"COMPLETE")
        for export in report["exports"]:
            row=export["transport"];raw=t.unpack(self.root,row)
            joined=t.join_annotations(raw,self.identity,obs["batches"],export["window"])
            self.assertEqual(joined["status"],"JOINED_CPU_GRAPH_API_ONLY")
        windows={r["window"]:r for r in report["windows"]}
        for name,row in windows.items():
            self.assertEqual(row["settled_rounds"],2 if name.endswith("decode") else 0)
            self.assertEqual(len(row["sequences"]),2 if name.endswith("decode") else 1)
        self.assertEqual(list(self.raw.iterdir()),[])

    def test_start_one_never_constructs_or_calls_profiler(self):
        o=self.observer(start=1);s=self.drive(o)
        self.assertEqual(s.runs,120);self.assertEqual(self.report()["status"],"NOT_PROFILED")
        self.assertTrue(all(r["status"]=="NOT_REQUESTED" for r in self.report()["windows"]))

    def test_restore_returns_both_writer_digests(self):
        o=self.observer(start=1);cls=scheduler_class();restore=d.install(o,cls,originals(cls))
        drive_core(cls,self.menu);receipt=restore()
        self.assertIs(restore(),receipt);self.assertIs(receipt,o.close_receipt)
        self.assertEqual(receipt["capture_status"],"NOT_PROFILED")
        self.assertEqual(receipt["capture_sha256"],d.sha((self.root/"CAPTURE-rank-0.json").read_bytes()))
        obs=d.read_recording(self.root,dict(self.identity,start=1),self.menu,receipt["core_summary_sha256"])
        self.assertEqual(obs["status"],"COMPLETE")

    def test_capture_write_failure_returns_core_pin_and_incomplete_receipt(self):
        o=self.observer(start=1);cls=scheduler_class();restore=d.install(o,cls,originals(cls))
        drive_core(cls,self.menu)
        with patch.object(c,"exclusive",side_effect=OSError("fixture full directory")):receipt=restore()
        self.assertEqual(receipt["capture_status"],"INCOMPLETE")
        self.assertIsNone(receipt["capture_sha256"]);self.assertEqual(receipt["capture_write_error"],"OSError")
        self.assertEqual(d.read_recording(self.root,dict(self.identity,start=1),self.menu,
                                         receipt["core_summary_sha256"])["status"],"COMPLETE")
        self.assertIs(restore(),receipt)

    def test_mismatched_output_directories_refused_before_journal(self):
        torch=FakeTorch();backend=self.backend(torch);other=self.root/"other";other.mkdir()
        with self.assertRaisesRegex(ValueError,"output directory"):
            c.CaptureObserver(other,self.identity,self.menu,backend)
        self.assertEqual(list(other.iterdir()),[]);self.assertEqual(torch.events,[])

    def test_profiler_failures_stop_once_no_retry_native_calls_preserved(self):
        for where in ("start","export"):
            with self.subTest(where=where),tempfile.TemporaryDirectory() as folder:
                originalroot,originalraw=self.root,self.raw
                self.root=Path(folder).resolve();self.raw=self.root/"raw";self.raw.mkdir()
                try:
                    torch=FakeTorch(where);o=self.observer(torch);s=self.drive(o,torch)
                    self.assertEqual(s.runs,120);self.assertEqual(s.processes,120)
                    self.assertEqual(torch.events.count("start"),1);self.assertEqual(torch.events.count("stop"),1)
                    self.assertEqual(self.report()["status"],"INCOMPLETE");self.assertTrue(o.capture_failed)
                    self.assertEqual(sum(r["status"]=="NOT_ATTEMPTED_AFTER_CAPTURE_FAILURE"
                                         for r in self.report()["windows"]),6)
                finally:self.root,self.raw=originalroot,originalraw

    def test_unresolved_stop_exits_worker_without_another_batch_or_retry(self):
        torch=FakeTorch("stop");o=self.observer(torch);cls=scheduler_class()
        expected=originals(cls);s=cls();b=batch(self.menu[3:4])
        with d.scope(o,cls,expected):
            with self.assertRaisesRegex(RuntimeError,"profiler stop unresolved"):
                s.process_batch_result(b,s.run_batch(b))
            self.assertEqual((s.runs,s.processes),(1,1))
            with self.assertRaisesRegex(RuntimeError,"profiler stop unresolved"):s.run_batch(b)
            self.assertEqual((s.runs,s.processes),(1,1))
        self.assertEqual(originals(cls),expected)
        self.assertTrue(o.backend.stop_failed)
        self.assertEqual(torch.events.count("stop"),1);self.assertNotIn("export",torch.events)
        self.assertEqual(self.report()["status"],"INCOMPLETE")

    def test_model_interrupt_during_capture_propagates_and_restores(self):
        torch=FakeTorch();o=self.observer(torch);cls=scheduler_class();error=KeyboardInterrupt()
        def fail(*args):raise error
        cls.run_batch=fail;before=originals(cls)
        with self.assertRaises(KeyboardInterrupt) as got:
            with d.scope(o,cls,before):cls().run_batch(batch(self.menu[3:4]))
        self.assertIs(got.exception,error);self.assertEqual(originals(cls),before)
        self.assertEqual(torch.events.count("stop"),1);self.assertEqual(self.report()["status"],"INCOMPLETE")
        self.assertNotIn("export",torch.events)
        self.assertLess(torch.events.index("annotation-exit"),torch.events.index("stop"))

    def test_cleanup_interrupt_cannot_replace_original_model_error(self):
        torch=FakeTorch("stop-interrupt");o=self.observer(torch);cls=scheduler_class();error=RuntimeError("model fixture")
        def fail(*args):raise error
        cls.run_batch=fail;before=originals(cls)
        with self.assertRaises(RuntimeError) as got:
            with d.scope(o,cls,before):cls().run_batch(batch(self.menu[3:4]))
        self.assertIs(got.exception,error);self.assertEqual(originals(cls),before)
        self.assertEqual(torch.events.count("stop"),1);self.assertNotIn("export",torch.events)
        self.assertIn({"where":"abort-stop","type":"KeyboardInterrupt"},self.report()["errors"])

    def test_second_captured_round_model_failure_stops_without_export(self):
        torch=FakeTorch();o=self.observer(torch);cls=scheduler_class();native=cls.run_batch
        error=RuntimeError("second captured round fixture")
        def run(instance,b,*args,**kw):
            torch.dispatch(b.forward_mode.is_decode());result=native(instance,b,*args,**kw)
            if instance.runs==5:raise error
            return result
        cls.run_batch=run;before=originals(cls);s=cls();b=batch(self.menu[4:5])
        with self.assertRaises(RuntimeError) as got:
            with d.scope(o,cls,before):
                s.process_batch_result(b,s.run_batch(b));b.forward_mode=Mode("decode")
                for _ in range(4):s.process_batch_result(b,s.run_batch(b))
        self.assertIs(got.exception,error);self.assertEqual((s.runs,s.processes),(5,4))
        self.assertEqual(originals(cls),before);self.assertNotIn("export",torch.events)
        self.assertEqual(torch.events.count("stop"),1)
        rows={r["window"]:r for r in self.report()["windows"]}
        self.assertEqual(rows["long-c1-warm-decode"]["status"],"INCOMPLETE_CAPTURE")
        self.assertEqual(rows["long-c1-warm-decode"]["sequences"],[3,4])
        self.assertLess(torch.events.index("annotation-exit"),torch.events.index("stop"))

    def test_multichunk_prefill_middle_window_through_observer(self):
        ids=list(range(9));self.menu[3].update(input_tokens=9,input_ids_sha256=d.sha(d.canonical(ids)))
        torch=FakeTorch();o=self.observer(torch);cls=scheduler_class()
        native_run,native_process=cls.run_batch,cls.process_batch_result
        def run(instance,b,*args,**kw):
            torch.dispatch(b.forward_mode.is_decode());return native_run(instance,b,*args,**kw)
        def process(instance,b,result):
            if b.prefix_lens[0]+b.extend_lens[0]<9:
                instance.processes+=1;return result
            return native_process(instance,b,result)
        cls.run_batch=run;cls.process_batch_result=process;s=cls();b=batch(self.menu[3:4])
        b.reqs[0].origin_input_ids=ids
        with d.scope(o,cls,originals(cls)):
            for start in (0,3,6):
                b.prefix_lens=[start];s.process_batch_result(b,s.run_batch(b))
        self.assertEqual((s.runs,s.processes),(3,3))
        row=next(r for r in self.report()["windows"] if r["window"]=="long-c1-cold-prefill")
        self.assertEqual(row["sequences"],[1]);self.assertEqual(row["status"],"ROUNDS_OBSERVED")
        obs=d.read_recording(self.root,self.identity,self.menu,o.close_receipt["core_summary_sha256"])
        self.assertEqual([x["prefix_lens"][0] for x in obs["batches"]],[0,3,6])
        export=self.report()["exports"][0]
        joined=t.join_annotations(t.unpack(self.root,export["transport"]),self.identity,obs["batches"],export["window"])
        self.assertEqual([r["sequence"] for r in joined["rounds"]],[1])

    def test_c2_singleton_round_does_not_count_toward_settling(self):
        torch=FakeTorch();o=self.observer(torch);cls=scheduler_class();native=cls.run_batch
        def run(instance,b,*args,**kw):
            torch.dispatch(b.forward_mode.is_decode());return native(instance,b,*args,**kw)
        cls.run_batch=run;s=cls();b=batch(self.menu[5:7])
        with d.scope(o,cls,originals(cls)):
            s.process_batch_result(b,s.run_batch(b));b.forward_mode=Mode("decode")
            singleton=copy.copy(b);singleton.reqs=b.reqs[:1]
            s.process_batch_result(singleton,s.run_batch(singleton))
            for turn in range(5):
                b.finish=turn==4;s.process_batch_result(b,s.run_batch(b))
        self.assertEqual((s.runs,s.processes),(7,7))
        row=next(r for r in self.report()["windows"] if r["window"]=="long-c2-cold-decode")
        self.assertEqual(row["settled_rounds"],2);self.assertEqual(row["sequences"],[4,5])
        self.assertEqual(row["status"],"ROUNDS_OBSERVED")

    def test_early_finish_does_not_substitute_later_repetition(self):
        torch=FakeTorch();o=self.observer(torch);self.drive(o,torch,finish_round=1)
        self.assertEqual(torch.events.count("start"),1)
        self.assertEqual(self.report()["status"],"INCOMPLETE")
        self.assertTrue(all(r["status"]=="INCOMPLETE_EARLY_FINISH" for r in self.report()["windows"] if r["window"].endswith("decode")))

    def test_middle_prefill_trigger_uses_first_host_frontier_crossing(self):
        rows=copy.deepcopy(self.menu);rows[3]["input_tokens"]=100
        w=c.Windows(self.identity,rows)
        base=dict(sequence=0,phase="prefill",case="long-c1-cold",repetition=0,members=[3],prefix_lens=[0],extend_lens=[30])
        self.assertIsNone(w.before(base))
        base.update(sequence=1,prefix_lens=[30]);self.assertEqual(w.before(base),"long-c1-cold-prefill")

    def settled(self):
        w=c.Windows(self.identity,self.menu);members=sorted(w.members["long-c2-cold"])
        row=dict(case="long-c2-cold",phase="decode",repetition=0,members=members,can_run_cuda_graph=True,
                 before=[dict(output=1,finished=False)]*2,after=[dict(output=2,finished=False)]*2)
        for seq in (0,1):row["sequence"]=seq;self.assertIsNone(w.before(row));self.assertIsNone(w.after(row))
        row["sequence"]=2;return w,row

    def test_c2_membership_lost_at_trigger_is_final(self):
        w,row=self.settled();full=row["members"][:];row["members"]=full[:1]
        self.assertIsNone(w.before(row));row.update(sequence=3,members=full)
        self.assertIsNone(w.before(row));self.assertEqual(w.rows["long-c2-cold-decode"]["status"],"INCOMPLETE_MEMBERSHIP_AT_TRIGGER")

    def test_c2_membership_or_adjacency_break_aborts_second_round(self):
        for mutation in (lambda r:r.update(members=r["members"][:1]),lambda r:r.update(sequence=4)):
            w,row=self.settled();self.assertEqual(w.before(row),"long-c2-cold-decode");w.after(row)
            row["sequence"]=3;mutation(row)
            with self.assertRaises(ValueError):w.before(row)

    def test_finish_during_captured_round_is_incomplete(self):
        w,row=self.settled();w.before(row);row["after"][0]=dict(output=2,finished=True)
        with self.assertRaises(ValueError):w.after(row)

    def test_mount_parser_requires_exact_fixed_tmpfs(self):
        path=Path('/mnt/shared-trace-raw-rank-0')
        valid=f'1 2 0:1 / {path} rw - tmpfs tmpfs rw,size=32768k\n'
        self.assertEqual(c.mount_size(valid,path),32<<20)
        for raw in (valid.replace('32768k','65536k'),valid.replace('- tmpfs','- ext4'),valid+valid,valid.replace(str(path),'/tmp')):
            with self.subTest(raw=raw),self.assertRaises(ValueError):c.mount_size(raw,path)

    def test_real_backend_refuses_unbounded_raw_directory_before_profiler(self):
        torch=FakeTorch()
        with self.assertRaises(ValueError):c.KinetoBackend(torch,self.raw,self.root,self.identity)
        self.assertEqual(torch.events,[])


if __name__=="__main__":unittest.main(verbosity=2)
