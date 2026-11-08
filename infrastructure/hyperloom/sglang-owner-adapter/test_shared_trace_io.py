"""Synthetic Kineto-format files; no profiler or device execution."""
import copy
import gzip
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parent))
import shared_diagnostic as d
import shared_trace_io as t
from test_shared_diagnostic import identity


def fixture(case="long-c1-cold"):
    ident=identity();events=[];rows=[]
    for seq in (3,4):
        start=seq*1000
        events.append(dict(name=f"OBS:{ident['nonce']}:{seq}",cat="user_annotation",ph="X",pid=100,tid=5,ts=start,dur=500))
        rows.append(dict(sequence=seq,case=case,repetition=0,phase="decode",can_run_cuda_graph=True,members=[3,4] if "c2" in case else [3]))
        for j in range(2):
            correlation=seq*10+j
            events.append(dict(name="hipGraphLaunch",cat="cuda_runtime",ph="X",pid=100,tid=5,
                               ts=start+10+j*100,dur=5,args={"correlation":correlation}))
            # No GPU/CPU order inference: kernel times are deliberately outside markers.
            events.append(dict(name="kernel",cat="kernel",ph="X",pid=0,tid=7,
                               ts=start+2000+j*100,dur=100,args={"correlation":correlation,"device":0}))
    return dict(distributedInfo=dict(rank=0,world_size=2),traceEvents=events),rows


class TestTraceIO(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve()
        self.raw=self.root/"raw.json";self.raw.write_bytes(d.canonical(fixture()[0]))
    def tearDown(self):self.tmp.cleanup()
    def pack(self,window="long-c1-cold-decode"):
        return t.TraceStore(self.root,0).pack(self.raw,window)

    def test_round_trip_bounded_export_is_not_producer_proof(self):
        row=self.pack()
        self.assertEqual(row["status"],"COMPLETE")
        self.assertEqual(t.unpack(self.root,row),self.raw.read_bytes())
        self.assertFalse(row["producer_hard_bound_verified"])
        self.assertEqual(t.raw_mount_plan(),["--size",str(32<<20),"--tmpfs","/mnt/shared-trace-raw-rank-0",
                                           "--size",str(32<<20),"--tmpfs","/mnt/shared-trace-raw-rank-1"])

    def test_compressed_overflow_retained_bounded_and_no_retry(self):
        self.raw.write_bytes(os.urandom(4096));store=t.TraceStore(self.root,0)
        with patch.object(t,"DECODE_LIMIT",128):row=store.pack(self.raw,"long-c1-cold-decode")
        self.assertEqual(row["status"],"INCOMPLETE")
        self.assertLessEqual((self.root/row["file"]).stat().st_size,128)
        self.assertEqual(store.used,row["bytes"])
        with self.assertRaises(ValueError):store.pack(self.raw,"long-c1-cold-decode")
        with self.assertRaises(FileExistsError):t.TraceStore(self.root,0).pack(self.raw,"long-c1-cold-decode")
        with self.assertRaises(ValueError):t.unpack(self.root,row)

    def test_raw_overflow_and_symlink_refused_before_output(self):
        with patch.object(t,"RAW_LIMIT",8):row=self.pack()
        self.assertEqual(row["status"],"INCOMPLETE");self.assertEqual(row["bytes"],0)
        self.assertFalse((self.root/row["file"]).exists())
        self.raw.unlink();target=self.root/"other.json";target.write_bytes(b"{}");self.raw.symlink_to(target)
        row=self.pack("long-c1-warm-decode")
        self.assertEqual(row["status"],"INCOMPLETE")

    def test_failed_export_bytes_charge_rank_budget(self):
        store=t.TraceStore(self.root,0)
        with patch.object(t.os,"fsync",side_effect=OSError("fixture full")):
            # Retain attempt normally; fail only profiler output synchronization.
            original=t.exclusive
            def create(path,raw):
                with patch.object(t.os,"fsync",return_value=None):original(path,raw)
            with patch.object(t,"exclusive",side_effect=create):row=store.pack(self.raw,"long-c1-cold-decode")
        self.assertEqual(row["status"],"INCOMPLETE");self.assertGreater(row["bytes"],0)
        self.assertEqual(store.used,(self.root/row["file"]).stat().st_size)

    def test_aggregate_cap_is_hard(self):
        self.raw.write_bytes(os.urandom(4096));store=t.TraceStore(self.root,0)
        with patch.multiple(t,DECODE_LIMIT=300,RANK_TRACE_LIMIT=400):
            first=store.pack(self.raw,"long-c1-cold-decode")
            second=store.pack(self.raw,"long-c1-warm-decode")
        self.assertEqual(first["status"],"INCOMPLETE");self.assertEqual(second["status"],"INCOMPLETE")
        self.assertEqual(store.used,400)

    def test_unpack_rejects_trailing_member_and_expansion(self):
        row=self.pack();p=self.root/row["file"]
        packed=p.read_bytes()+gzip.compress(b"{}");p.write_bytes(packed)
        altered=dict(row,bytes=len(packed),sha256=d.sha(packed))
        with self.assertRaises(ValueError):t.unpack(self.root,altered)
        raw=b"x"*4096;packed=gzip.compress(raw);p.write_bytes(packed)
        altered.update(bytes=len(packed),sha256=d.sha(packed),raw_bytes=len(raw),raw_sha256=d.sha(raw))
        with patch.object(t,"RAW_LIMIT",100),self.assertRaises(ValueError):t.unpack(self.root,altered)

    def test_unpack_rejects_truncated_or_changed_bytes(self):
        row=self.pack();p=self.root/row["file"];packed=p.read_bytes()[:-3];p.write_bytes(packed)
        with self.assertRaises(ValueError):t.unpack(self.root,row)
        row.update(bytes=len(packed),sha256=d.sha(packed))
        with self.assertRaises(ValueError):t.unpack(self.root,row)

    def test_annotation_join_does_not_claim_gpu_order(self):
        trace,rows=fixture();joined=t.join_annotations(d.canonical(trace),identity(),rows,"long-c1-cold-decode")
        self.assertEqual(joined["status"],"JOINED_CPU_GRAPH_API_ONLY")
        self.assertFalse(joined["gpu_dependency_established"]);self.assertFalse(joined["critical_path_established"])
        self.assertEqual([g["kernels"] for r in joined["rounds"] for g in r["groups"]],[1]*4)

    def test_prefill_single_annotation_no_graphs(self):
        trace,rows=fixture();trace["traceEvents"]=[trace["traceEvents"][0]];rows=rows[:1];rows[0].update(phase="prefill",can_run_cuda_graph=False)
        result=t.join_annotations(d.canonical(trace),identity(),rows,"long-c1-cold-prefill")
        self.assertEqual(result["rounds"][0]["groups"],[])

    def test_marker_and_workload_mutations_refused(self):
        changes=[lambda t,r:t["distributedInfo"].update(rank=1),
                 lambda t,r:t["traceEvents"][0].update(pid=101),
                 lambda t,r:t["traceEvents"][0].update(name="OBS:"+"0"*16+":3"),
                 lambda t,r:t["traceEvents"].append(dict(t["traceEvents"][0])),
                 lambda t,r:t["traceEvents"][0].update(dur=0),
                 lambda t,r:r[0].update(phase="prefill"),
                 lambda t,r:r[0].update(repetition=1),
                 lambda t,r:t["traceEvents"].pop(0)]
        for i,change in enumerate(changes):
            with self.subTest(i=i):
                trace,rows=fixture();change(trace,rows)
                with self.assertRaises(ValueError):t.join_annotations(d.canonical(trace),identity(),rows,"long-c1-cold-decode")

    def test_c2_requires_both_members(self):
        trace,rows=fixture("long-c2-cold")
        t.join_annotations(d.canonical(trace),identity(),rows,"long-c2-cold-decode")
        rows[1]["members"]=[3]
        with self.assertRaises(ValueError):t.join_annotations(d.canonical(trace),identity(),rows,"long-c2-cold-decode")

    def test_adjacent_round_membership_must_match(self):
        trace,rows=fixture("long-c2-cold");rows[1]["members"]=[4,5]
        with self.assertRaises(ValueError):t.join_annotations(d.canonical(trace),identity(),rows,"long-c2-cold-decode")

    def test_extra_unowned_graph_api_refused(self):
        trace,rows=fixture()
        trace["traceEvents"].append(dict(trace["traceEvents"][1],ts=0,args={"correlation":999}))
        with self.assertRaises(ValueError):
            t.join_annotations(d.canonical(trace),identity(),rows,"long-c1-cold-decode")

    def test_contradicting_graph_flag_refused(self):
        trace,rows=fixture();rows[0]["can_run_cuda_graph"]=False
        with self.assertRaisesRegex(ValueError,"graph route contradicts"):
            t.join_annotations(d.canonical(trace),identity(),rows,"long-c1-cold-decode")

    def test_ambiguous_api_and_kernel_relations_refused(self):
        changes=[lambda ev:ev.append(dict(ev[1],ts=0)),
                 lambda ev:ev.append(dict(ev[1],name="hipMemcpyAsync")),
                 lambda ev:ev[1].update(ts=0),
                 lambda ev:ev[2].update(dur=0),
                 lambda ev:ev[2]["args"].update(correlation=999),
                 lambda ev:ev.append(dict(ev[2],args=dict(ev[2]["args"],device=1))),
                 lambda ev:ev[3]["args"].update(correlation=ev[1]["args"]["correlation"])]
        for i,change in enumerate(changes):
            with self.subTest(i=i):
                trace,rows=fixture();change(trace["traceEvents"])
                with self.assertRaises(ValueError):t.join_annotations(d.canonical(trace),identity(),rows,"long-c1-cold-decode")


if __name__=="__main__":unittest.main(verbosity=2)
