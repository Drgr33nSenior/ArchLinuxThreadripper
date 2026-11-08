"""CPU fixtures only; never imports a serving framework or opens a device."""
from contextlib import contextmanager
from array import array
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
import shared_diagnostic as d


def identity(rank=0):
    return dict(schema=1,rank=rank,start=2,pid=100+rank,session_sha256="a"*64,
                source_manifest_sha256="b"*64,worker_identity_sha256=str(rank)*64,
                nonce="1234567890abcdef",control=True,power_cap_watts=235)


def menu():
    rows=[]
    for case,rep in [("warmup-c1",0),("warmup-c2",0)]+[(c,r) for r in range(3) for c in d.CASES]:
        concurrency=2 if "-c2" in case else 1
        for _ in range(concurrency):
            i=len(rows);ids=[i,42,7]
            rows.append(dict(ordinal=i,rid=f"request-{i}",cache_salt=f"salt-{i}",input_tokens=3,
                             input_ids_sha256=d.sha(d.canonical(ids)),body_sha256="c"*64,
                             case=case,repetition=rep,concurrency=concurrency))
    return rows


class Mode:
    def __init__(self,phase):self.phase=phase
    def is_extend(self):return self.phase=="prefill"
    def is_decode(self):return self.phase=="decode"


def batch(rows,phase="prefill"):
    reqs=[NS(rid=r["rid"],cache_salt=r["cache_salt"],origin_input_ids=[r["ordinal"],42,7],
             output_ids=array("q"),finished_len=None,finished_reason=None,kv=NS(kv_committed_len=0)) for r in rows]
    return NS(reqs=reqs,forward_mode=Mode(phase),prefix_lens=[0]*len(rows),
              extend_lens=[3]*len(rows),extend_num_tokens=3*len(rows),finish=False)


def scheduler_class():
    class Scheduler:
        enable_overlap=False
        def __init__(self):self.runs=0;self.processes=0;self.latest=None
        def run_batch(self,batch):
            self.runs+=1
            self.latest=NS(can_run_cuda_graph=batch.forward_mode.is_decode(),
                           speculative_num_draft_tokens=8,num_correct_drafts_per_req_cpu=None)
            return self.latest
        def process_batch_result(self,batch,result):
            self.processes+=1
            # CPU counters are published by native processing, not run_batch.
            result.num_correct_drafts_per_req_cpu=[1]*len(batch.reqs)
            for req in batch.reqs:
                n=2 if batch.forward_mode.is_decode() else 1
                req.output_ids.extend([0]*n);req.kv.kv_committed_len+=n
                if batch.finish:req.finished_reason="stop"
            return result
    return Scheduler


def originals(cls):return {k:cls.__dict__[k] for k in ("run_batch","process_batch_result")}


def drive(cls,rows):
    sched=cls();groups={}
    for r in rows:groups.setdefault((r["case"],r["repetition"]),[]).append(r)
    for group in groups.values():
        b=batch(group)
        result=sched.run_batch(b)
        assert result is sched.latest
        assert sched.process_batch_result(b,result) is result
        b.forward_mode=Mode("decode")
        for turn in range(4):
            b.finish=turn==3
            result=sched.run_batch(b);sched.process_batch_result(b,result)
    return sched


class TestObserver(unittest.TestCase):
    def test_native_array_and_legacy_list_preserve_visible_extent(self):
        for output in (array("q",[11,12,13]),[11,12,13]):
            req=NS(output_ids=output,finished_len=2,finished_reason="stop",kv=NS(kv_committed_len=9))
            self.assertEqual(d.request_state(req),dict(output=2,finished=True,kv_committed_len=9))
            self.assertIs(req.output_ids,output)
            req.finished_len=4
            with self.assertRaisesRegex(ValueError,"visible output extent"):d.request_state(req)

    def test_unknown_output_container_refused_without_coercion(self):
        class DeviceLike:
            def __len__(self):raise AssertionError("device-like value was accessed")
            def __iter__(self):raise AssertionError("device-like value was iterated")
        for output in ((),array("d"),array("i"),DeviceLike()):
            with self.subTest(kind=type(output).__name__),self.assertRaisesRegex(ValueError,"CPU output IDs container"):
                d.request_state(NS(output_ids=output))

    def test_failure_receipt_has_static_check_but_no_arbitrary_exception_text(self):
        o=self.observer()
        try:d.request_state(NS(output_ids=()))
        except ValueError as error:o.failure("begin",error)
        self.assertEqual(o.errors[0],dict(where="begin",type="ValueError",check="CPU output IDs container",sequence=0))
        o.failure("model",ValueError("secret prompt /private/request"))
        o.failure("model",ValueError("shared diagnostic: unlisted private value"))
        o.failure("model",ValueError("shared diagnostic: prompt identity", "private extra argument"))
        pin=o.close(True)
        raw=(self.root/"OBS-rank-0.json").read_bytes()
        self.assertNotIn(b"secret",raw);self.assertNotIn(b"private",raw)
        self.assertEqual(self.read(summary_pin=pin)["status"],"INCOMPLETE")

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve();self.menu=menu();self.observers={}
    def tearDown(self):self.tmp.cleanup()
    def observer(self,rank=0,**kw):
        self.observers[rank]=d.Observer(self.root,identity(rank),self.menu,clock=itertools.count(10000*rank+100).__next__,**kw)
        return self.observers[rank]
    def read(self,rank=0,summary_pin=None):
        return d.read_recording(self.root,identity(rank),self.menu,summary_pin or self.observers[rank].summary_sha256)
    def full(self,rank=0):
        cls=scheduler_class();original=originals(cls);o=self.observer(rank)
        with d.scope(o,cls,original):sched=drive(cls,self.menu)
        self.assertEqual(sched.runs,100);self.assertEqual(sched.processes,100)
        self.assertEqual(originals(cls),original)
        return self.read(rank)

    def test_complete_two_rank_logical_join_ignores_clock_offsets(self):
        left=self.full();right=self.full(1)
        self.assertEqual(left["status"],"COMPLETE")
        self.assertEqual(set(left["final_output_counts"].values()),{9})
        joined=d.join_ranks(left,right)
        self.assertEqual(joined["status"],"COMPLETE")
        self.assertFalse(joined["clocks_aligned"]);self.assertFalse(joined["http_join_complete"])
        self.assertTrue(all(r["prefix_lens"] is None for r in left["batches"] if r["phase"]=="decode"))

    def test_rank_work_or_identity_mismatch(self):
        left=self.full();right=self.full(1)
        right["batches"][1]["correct_drafts_cpu"]=[2]
        self.assertEqual(d.join_ranks(left,right)["status"],"INCOMPLETE")
        right["identity"]["session_sha256"]="f"*64
        with self.assertRaises(ValueError):d.join_ranks(left,right)

    def test_bad_inputs_disable_observation_but_call_native_once(self):
        mutations=[lambda s,b:setattr(s,"enable_overlap",True),
                   lambda s,b:setattr(b.reqs[0],"cache_salt","foreign"),
                   lambda s,b:setattr(b.reqs[0],"origin_input_ids",[3,2,1]),
                   lambda s,b:b.reqs.append(NS(rid="foreign")),
                   lambda s,b:setattr(b,"forward_mode",Mode("unknown"))]
        for i,mutate in enumerate(mutations):
            with self.subTest(i=i),tempfile.TemporaryDirectory() as folder:
                o=d.Observer(Path(folder).resolve(),identity(),self.menu);cls=scheduler_class();s=cls();b=batch(self.menu[:1]);mutate(s,b)
                result=object();calls=[]
                self.assertIs(o.run(lambda *a:calls.append("run") or result,s,b),result)
                self.assertIs(o.process(lambda *a:calls.append("process") or result,s,b,result),result)
                self.assertEqual(calls,["run","process"]);self.assertTrue(o.disabled);o.close(True)

    def test_foreign_batch_is_ignored(self):
        o=self.observer();b=NS(reqs=[NS(rid="health-check")]);s=NS(enable_overlap=False);result=object()
        self.assertIs(o.run(lambda *a:result,s,b),result)
        self.assertIs(o.process(lambda *a:result,s,b,result),result)
        self.assertFalse(o.disabled);self.assertEqual(o.sequence,0);o.close(True)

    def test_wrapper_preserves_extra_arguments(self):
        o=self.observer();cls=scheduler_class();seen=[];answer=object()
        def native(instance,batch,pp_proxy_tensors=None,*,flag=False):
            seen.append((pp_proxy_tensors,flag));return answer
        cls.run_batch=native;original=originals(cls);proxy=object()
        with d.scope(o,cls,original):
            self.assertIs(cls().run_batch(NS(reqs=[]),proxy,flag=True),answer)
        self.assertEqual(seen,[(proxy,True)])

    def test_process_inspection_failure_preserves_native_call(self):
        o=self.observer();calls=[];b=NS();answer=object()
        self.assertIs(o.process(lambda *a:calls.append(1) or answer,object(),b,object()),answer)
        self.assertEqual(calls,[1]);self.assertTrue(o.disabled);o.close(True)

    def test_cpu_counter_guard_never_converts_device_like_value(self):
        class DeviceBomb:
            def __getattr__(self,name):raise AssertionError("device access forbidden")
        o=self.observer();cls=scheduler_class();s=cls();b=batch(self.menu[:1])
        first=o.run(cls.run_batch,s,b);o.process(cls.process_batch_result,s,b,first)
        b.forward_mode=Mode("decode")
        result=NS(can_run_cuda_graph=True,num_correct_drafts_per_req_cpu=DeviceBomb(),speculative_num_draft_tokens=8)
        self.assertIs(o.run(lambda *a:result,s,b),result)
        o.process(lambda *a:None,s,b,result)
        self.assertTrue(o.disabled);self.assertEqual(o.errors[-1]["type"],"ValueError");o.close(True)

    def test_mismatched_result_identity_and_pending_close(self):
        o=self.observer();s=NS(enable_overlap=False);b=batch(self.menu[:1]);result=object()
        o.run(lambda *a:result,s,b);o.process(lambda *a:None,s,b,object())
        self.assertTrue(o.disabled);self.assertIsNone(o.pending);o.close(True)
        self.assertEqual(self.read()["status"],"INCOMPLETE")

    def test_model_exception_preserved_and_hooks_restored(self):
        for error in (RuntimeError("do not retain this text"),KeyboardInterrupt()):
            with self.subTest(type=type(error).__name__),tempfile.TemporaryDirectory() as folder:
                cls=scheduler_class()
                def broken(*a):raise error
                cls.run_batch=broken;original=originals(cls)
                o=d.Observer(Path(folder).resolve(),identity(),self.menu)
                with self.assertRaises(type(error)) as caught:
                    with d.scope(o,cls,original):cls().run_batch(batch(self.menu[:1]))
                self.assertIs(caught.exception,error);self.assertEqual(originals(cls),original)
                self.assertIsNone(o.pending)
                self.assertNotIn("do not retain",(Path(folder)/"OBS-rank-0.json").read_text())

    def test_native_processing_exception_preserved(self):
        o=self.observer();cls=scheduler_class();error=RuntimeError("native")
        def broken(*a):raise error
        cls.process_batch_result=broken;original=originals(cls)
        with self.assertRaises(RuntimeError) as caught:
            with d.scope(o,cls,original):
                s=cls();b=batch(self.menu[:1]);s.process_batch_result(b,s.run_batch(b))
        self.assertIs(caught.exception,error);self.assertEqual(originals(cls),original);self.assertIsNone(o.pending)

    def test_annotation_failure_and_native_call(self):
        @contextmanager
        def annotation(_):
            yield
            raise ValueError("annotation cleanup")
        o=self.observer(annotate=annotation);calls=[];result=object();b=batch(self.menu[:1])
        self.assertIs(o.run(lambda *a:calls.append(1) or result,NS(enable_overlap=False),b),result)
        self.assertEqual(calls,[1]);self.assertTrue(o.disabled);o.close(True)

    def test_scope_restores_owned_hooks_preserves_foreign_hook(self):
        o=self.observer();cls=scheduler_class();original=originals(cls);foreign=lambda *a:None
        with self.assertRaisesRegex(ValueError,"foreign hook"):
            with d.scope(o,cls,original):cls.run_batch=foreign
        self.assertIs(cls.run_batch,foreign);self.assertIs(cls.process_batch_result,original["process_batch_result"])
        self.assertFalse(hasattr(cls,"_shared_observation_installed"));self.assertEqual(self.read()["status"],"INCOMPLETE")

    def test_partial_install_restored(self):
        class Reject(type):
            armed=False
            def __setattr__(cls,name,value):
                if cls.armed and name=="process_batch_result":raise RuntimeError("fixture install refusal")
                super().__setattr__(name,value)
        donor=scheduler_class()
        cls=Reject("Selected",(),originals(donor));original=originals(cls);cls.armed=True;o=self.observer()
        with self.assertRaises(RuntimeError):d.install(o,cls,original)
        self.assertEqual(originals(cls),original);self.assertTrue(o.closed)

    def test_double_install_and_wrong_originals_refused(self):
        o=self.observer();cls=scheduler_class();original=originals(cls)
        bad=dict(original,run_batch=lambda *a:None)
        with self.assertRaises(ValueError):d.install(o,cls,bad)
        restore=d.install(o,cls,original)
        with self.assertRaises(ValueError):d.install(o,cls,originals(cls))
        restore();restore()

    def test_bounded_journal_failure_does_not_repeat_native(self):
        o=self.observer();cls=scheduler_class();original=originals(cls)
        with patch.object(o.journal,"append",side_effect=OSError("disk full")):
            with d.scope(o,cls,original):
                s=cls();b=batch(self.menu[:1]);s.process_batch_result(b,s.run_batch(b))
        self.assertEqual((s.runs,s.processes),(1,1));self.assertTrue(o.disabled)
        self.assertEqual(self.read()["status"],"INCOMPLETE")

    def test_reader_rejects_wrong_identity_digest_and_extra_files(self):
        self.full();p=self.root/"OBS-rank-0.json";pin=d.sha(p.read_bytes())
        with self.assertRaises(ValueError):d.read_recording(self.root,identity(),self.menu,"0"*64)
        other=identity();other["pid"]+=1
        with self.assertRaises(ValueError):d.read_recording(self.root,other,self.menu,pin)
        (self.root/"OBS-rank-0-extra").write_text("unexpected")
        with self.assertRaises(ValueError):self.read()

    def test_reader_rejects_digest_consistent_false_complete(self):
        o=self.observer();o.close(True);p=self.root/"OBS-rank-0.json"
        row=json.loads(p.read_bytes());row["status"]="COMPLETE";p.write_bytes(d.canonical(row))
        with self.assertRaisesRegex(ValueError,"complete claim mismatch"):self.read(summary_pin=d.sha(p.read_bytes()))

    def test_reader_rejects_digest_consistent_sequence_mutation(self):
        self.full();p=self.root/"OBS-rank-0.json";summary=json.loads(p.read_bytes());part=summary["parts"][0]
        chunk=self.root/part["file"];rows=[json.loads(x) for x in chunk.read_bytes().splitlines()]
        rows[2]["sequence"]=999;raw=b"".join(map(d.canonical,rows));chunk.write_bytes(raw)
        part.update(bytes=len(raw),sha256=d.sha(raw));summary["journal_bytes"]=len(raw);p.write_bytes(d.canonical(summary))
        with self.assertRaisesRegex(ValueError,"batch sequence"):self.read(summary_pin=d.sha(p.read_bytes()))

    def test_restore_returns_writer_digest_and_retained_wrapper_is_inert(self):
        markers=[]
        @contextmanager
        def annotation(name):markers.append(name);yield
        o=self.observer(annotate=annotation);cls=scheduler_class();restore=d.install(o,cls,originals(cls))
        retained=cls.run_batch
        pin=restore();self.assertEqual(pin,o.summary_sha256);self.assertEqual(restore(),pin)
        calls=cls();retained(calls,batch(self.menu[:1]))
        self.assertEqual(calls.runs,1);self.assertEqual(markers,[]);self.assertEqual(o.seen,set())

    def test_foreign_process_cannot_discard_pending_prefill(self):
        o=self.observer();s=NS(enable_overlap=False);b=batch(self.menu[:1]);result=object()
        o.run(lambda *a:result,s,b)
        foreign=NS(reqs=[]);calls=[]
        o.process(lambda *a:calls.append(1),s,foreign,object())
        self.assertEqual(calls,[1]);self.assertTrue(o.disabled);self.assertIsNone(o.pending)
        self.assertEqual(o.errors[0]["where"],"before-process");o.close(True)

    def test_rank_menu_and_schema_must_match(self):
        left=self.full();right=self.full(1)
        right["menu_sha256"]="f"*64
        with self.assertRaisesRegex(ValueError,"rank menu"):d.join_ranks(left,right)
        right["menu_sha256"]=left["menu_sha256"];right["schema"]=True
        with self.assertRaisesRegex(ValueError,"rank schema"):d.join_ranks(left,right)

    def test_four_stream_metadata_allocation_fits_spec(self):
        self.assertEqual(4*(d.RANK_BYTES+(256<<10)),16<<20)

    def test_missing_prefill_or_gap_disables_observation(self):
        for gap in (False,True):
            with self.subTest(gap=gap),tempfile.TemporaryDirectory() as folder:
                o=d.Observer(Path(folder).resolve(),identity(),self.menu);cls=scheduler_class();s=cls();b=batch(self.menu[:1])
                if gap:
                    b.extend_lens=[1];b.extend_num_tokens=1
                    r=o.run(cls.run_batch,s,b);o.process(cls.process_batch_result,s,b,r)
                    b.prefix_lens=[2]
                else:b.forward_mode=Mode("decode")
                o.run(cls.run_batch,s,b);self.assertTrue(o.disabled);o.close(True)

    def test_identity_and_menu_refusals(self):
        for change in ({"schema":True},{"power_cap_watts":300},{"rank":True},{"control":False}):
            with self.subTest(change=change),self.assertRaises(ValueError):d.identity_check(dict(identity(),**change))
        for change in ({"concurrency":2},{"repetition":2},{"ordinal":True}):
            altered=copy.deepcopy(self.menu);altered[0].update(change)
            with self.subTest(change=change),self.assertRaises(ValueError):d.menu_check(altered)

    def test_journal_rotation_and_caps(self):
        with patch.multiple(d,CHUNK_BYTES=120,REPORT_BYTES=1000,RANK_BYTES=2000):
            j=d.Journal(self.root,0)
            for _ in range(3):j.append({"data":"x"*70})
            self.assertEqual(len(j.parts),2)
            j.finish({"status":"INCOMPLETE"})
            self.assertTrue(all(p["bytes"]<=120 for p in j.parts))
        with self.assertRaises(ValueError):d.Journal(self.root,0)
        with tempfile.TemporaryDirectory() as folder,patch.multiple(d,REPORT_BYTES=128,RANK_BYTES=256):
            j=d.Journal(Path(folder).resolve(),0);j.append({"data":"x"*70})
            with self.assertRaises(ValueError):j.append({"data":"x"*70})
            j._finish_part()

    def test_symlink_and_duplicate_json_refused(self):
        self.full();p=self.root/"OBS-rank-0.json";pin=d.sha(p.read_bytes());moved=self.root/"other";p.rename(moved);p.symlink_to(moved)
        with self.assertRaises(ValueError):d.read_recording(self.root,identity(),self.menu,pin)
        with self.assertRaises(ValueError):d._json(b'{"a":1,"a":2}')


if __name__=="__main__":unittest.main(verbosity=2)
