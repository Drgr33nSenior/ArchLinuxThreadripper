"""Synthetic owner pins exercise joins; none is live qualification evidence."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parent))
import shared_diagnostic as d
import shared_readout as r
from test_shared_diagnostic import identity,menu,drive,scheduler_class,originals
import test_shared_capture as capture_fixture


def http_fixture(recording,plans):
    counts={i:[0,0,None] for i in range(30)}
    for row in recording["batches"]:
        for pos,idx in enumerate(row["members"]):
            if row["phase"]=="decode":counts[idx][0]+=1;counts[idx][1]+=row["correct_drafts_cpu"][pos]
            elif counts[idx][2] is None:counts[idx][2]=row["prefix_lens"][pos]
    rows=[]
    for p in plans:
        i=p["ordinal"];rounds,correct,cache=counts[i];output=recording["final_output_counts"][i]
        wire_sha=d.sha(d.canonical([recording["identity"]["start"],i,"fixture response bytes"]))
        contract=dict(case=p["case"],request_sha256=p["body_sha256"],prompt_token_ids_sha256=p["input_ids_sha256"],
                      status="NORMAL_STOP_COMPLETE",finish_reason="stop",metric_eligible=True,id_count_equals_reported_count=True,
                      raw_http_response_sha256=wire_sha,prompt_token_count=p["input_tokens"],response_id_count=output,
                      reported_completion_tokens=output,cached_tokens=cache,response_token_ids_sha256="d"*64,
                      usage=dict(prompt_tokens=p["input_tokens"],completion_tokens=output),
                      spec_tokens_details=dict(spec_verify_ct=rounds,spec_num_correct_drafts=correct),
                      terminal_meta_info=dict(id=p["rid"],prompt_tokens=p["input_tokens"],completion_tokens=output,
                                              cached_tokens=cache,num_retractions=0,spec_verify_ct=rounds,spec_num_correct_drafts=correct))
        rows.append(dict(ordinal=i,case=p["case"],body_sha256=p["body_sha256"],contract=contract,
                         wire=dict(status=200,response_observed=True,response_truncated=False,response_sha256=wire_sha)))
    return dict(stopped_early=False,paired_error=None,executed_case_count=30,frozen_case_count=30,rows=rows)


class TestReadout(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve();self.plans=menu()
    def tearDown(self):self.tmp.cleanup()
    def recording(self,start=1,rank=0):
        folder=self.root/f"{start}-{rank}";folder.mkdir()
        ident=dict(identity(rank),start=start,nonce=f"{start:016x}")
        o=d.Observer(folder,ident,self.plans);cls=scheduler_class()
        with d.scope(o,cls,originals(cls)):drive(cls,self.plans)
        return d.read_recording(folder,ident,self.plans,o.summary_sha256)
    def http(self,recording):
        raw=d.canonical(http_fixture(recording,self.plans))
        return self.join_summary(recording,raw)
    def receipt(self,recording,raw):
        ident=recording["identity"]
        return d.canonical(dict(schema=1,summary_sha256=d.sha(raw),menu_sha256=recording["menu_sha256"],
                               start_binding={k:ident[k] for k in
                                              ("start","nonce","session_sha256","source_manifest_sha256","control","power_cap_watts")}))
    def join_summary(self,recording,raw):
        receipt=self.receipt(recording,raw)
        return r.join_http(recording,self.plans,raw,d.sha(raw),receipt,d.sha(receipt))

    def test_http_counts_join_preserves_quality_boundary(self):
        obs=self.recording();result=self.http(obs)
        self.assertEqual(result["status"],"JOINED_HTTP_COUNTS_ONLY")
        self.assertTrue(result["upstream_wire_quality_release_validation_required"])
        self.assertFalse(result["diagnostic_success"])

    def test_http_digest_and_cross_source_mutations_refused(self):
        obs=self.recording();base=http_fixture(obs,self.plans)
        raw=d.canonical(base)
        receipt=self.receipt(obs,raw)
        with self.assertRaises(ValueError):r.join_http(obs,self.plans,raw,"0"*64,receipt,d.sha(receipt))
        changes=[lambda x:x["rows"][0]["contract"]["terminal_meta_info"].update(id="foreign"),
                 lambda x:x["rows"][0]["contract"].update(request_sha256="0"*64),
                 lambda x:x["rows"][0]["contract"].update(cached_tokens=1),
                 lambda x:x["rows"][0]["contract"]["usage"].update(completion_tokens=8),
                 lambda x:x["rows"][0]["contract"]["spec_tokens_details"].update(spec_verify_ct=9),
                 lambda x:x["rows"][0]["contract"]["spec_tokens_details"].update(spec_num_correct_drafts=9),
                 lambda x:x["rows"][0]["wire"].update(response_truncated=True),
                 lambda x:x["rows"][0]["contract"]["terminal_meta_info"].update(num_retractions=1),
                 lambda x:x["rows"].reverse()]
        for i,change in enumerate(changes):
            with self.subTest(i=i):
                altered=copy.deepcopy(base);change(altered);raw=d.canonical(altered)
                with self.assertRaises(ValueError):self.join_summary(obs,raw)

    def test_http_receipt_start_nonce_menu_and_digest_refused(self):
        obs=self.recording();raw=d.canonical(http_fixture(obs,self.plans))
        receipt=json.loads(self.receipt(obs,raw))
        for key,value in (("start",2),("nonce","f"*16),("session_sha256","f"*64)):
            altered=copy.deepcopy(receipt);altered["start_binding"][key]=value;bad=d.canonical(altered)
            with self.subTest(key=key),self.assertRaisesRegex(ValueError,"start binding"):
                r.join_http(obs,self.plans,raw,d.sha(raw),bad,d.sha(bad))
        for key,value in (("menu_sha256","f"*64),("summary_sha256","f"*64)):
            altered=copy.deepcopy(receipt);altered[key]=value;bad=d.canonical(altered)
            with self.subTest(key=key),self.assertRaisesRegex(ValueError,"start binding"):
                r.join_http(obs,self.plans,raw,d.sha(raw),bad,d.sha(bad))
        good=d.canonical(receipt)
        with self.assertRaisesRegex(ValueError,"owner-pinned HTTP start receipt"):
            r.join_http(obs,self.plans,raw,d.sha(raw),good,"0"*64)

    def test_same_http_summary_replayed_across_starts_is_refused(self):
        first=[self.recording(1,i) for i in (0,1)];second=[self.recording(2,i) for i in (0,1)]
        raw=d.canonical(http_fixture(second[0],self.plans))
        # Even separately bound fixture receipts cannot turn reused HTTP bytes
        # into independent evidence from two starts.
        a=[self.join_summary(x,raw) for x in first];b=[self.join_summary(x,raw) for x in second]
        with self.assertRaisesRegex(ValueError,"distinct HTTP evidence"):r.compare_starts(first,second,a,b)

    def test_rank_http_summary_or_receipt_disagreement_is_refused(self):
        first=[self.recording(1,i) for i in (0,1)];second=[self.recording(2,i) for i in (0,1)]
        a=[self.http(x) for x in first];b=[self.http(x) for x in second]
        altered=http_fixture(first[1],self.plans);altered["fixture_only_extra"]="different retained summary"
        a[1]=self.join_summary(first[1],d.canonical(altered))
        with self.assertRaisesRegex(ValueError,"both ranks require the same"):r.compare_starts(first,second,a,b)
        a=[self.http(x) for x in first];a[1]["receipt_sha256"]="f"*64
        with self.assertRaisesRegex(ValueError,"both ranks require the same"):r.compare_starts(first,second,a,b)

    def test_cross_start_work_is_compared_without_latency_transfer(self):
        first=[self.recording(1,i) for i in (0,1)];second=[self.recording(2,i) for i in (0,1)]
        a=[self.http(x) for x in first];b=[self.http(x) for x in second]
        result=r.compare_starts(first,second,a,b)
        self.assertEqual(result["status"],"SAME_OBSERVED_WORK");self.assertFalse(result["timing_transfer_established"])
        for h in b:h["responses"][0]["output_ids_sha256"]="0"*64
        self.assertEqual(r.compare_starts(first,second,a,b)["status"],"NOT_COMPARABLE")

    def test_cross_start_foreign_http_or_reused_nonce_refused(self):
        first=[self.recording(1,i) for i in (0,1)];second=[self.recording(2,i) for i in (0,1)]
        a=[self.http(x) for x in first];b=[self.http(x) for x in second]
        altered=copy.deepcopy(a);altered[0]["core_summary_sha256"]="f"*64
        with self.assertRaises(ValueError):r.compare_starts(first,second,altered,b)
        for obs in second:obs["identity"]["nonce"]=first[0]["identity"]["nonce"]
        with self.assertRaises(ValueError):r.compare_starts(first,second,a,b)

    def test_census_separates_overlap_sum_union_and_rank_clocks(self):
        def raw(rank,shift):
            events=[dict(name="kernel",cat="kernel",ph="X",ts=shift+t,dur=10,args=dict(device=0,stream=0)) for t in (0,5)]
            return d.canonical(dict(distributedInfo=dict(rank=rank,world_size=2),traceEvents=events))
        for rank in [r.trace_census(raw(0,0)),r.trace_census(raw(1,100000))]:
            self.assertEqual(rank["duration_sum_us"],"20");self.assertEqual(rank["interval_union_us"],"15")
            self.assertEqual(rank["stream_overlap_records"],{"0:0":1});self.assertFalse(rank["sum_is_execution_cost"])
            self.assertFalse(rank["critical_path_established"])

    def captured(self,rank):
        fixture=capture_fixture.TestCapture();fixture.setUp()
        try:
            fixture.identity=identity(rank);torch=capture_fixture.FakeTorch(rank=rank)
            o=fixture.observer(torch);fixture.drive(o,torch)
            receipt=o.close_receipt
            obs=d.read_recording(fixture.root,fixture.identity,fixture.menu,receipt["core_summary_sha256"])
            device=dict(rank=rank,pid=100+rank,worker_identity_sha256=fixture.identity["worker_identity_sha256"],
                        device_uuid=f"GPU-fixture-{rank}",trace_device_index=0)
            device_raw=d.canonical(device)
            return r.read_capture(fixture.root,obs,fixture.menu,receipt["capture_sha256"],
                                  o.backend.mount,device_raw,d.sha(device_raw))
        finally:fixture.tearDown()

    def test_rank_comparison_requires_same_start_window_and_distinct_devices(self):
        left,right=self.captured(0),self.captured(1);window="long-c1-cold-decode"
        joined=r.compare_rank_traces(left,right,window)
        self.assertEqual(joined["window"],window);self.assertFalse(joined["clocks_aligned"])
        self.assertFalse(joined["speedup_claim"])
        for mutate in (
            lambda x:x["identity"].update(nonce="f"*16),
            lambda x:x.update(menu_sha256="f"*64),
            lambda x:x["windows"].pop(),
            lambda x:next(y for y in x["windows"] if y["window"]==window)["joined"].update(device_uuid="GPU-fixture-0"),
            lambda x:next(y for y in x["windows"] if y["window"]==window)["joined"].update(window="long-c1-warm-decode")):
            bad=copy.deepcopy(right);mutate(bad)
            with self.assertRaises(ValueError):r.compare_rank_traces(left,bad,window)

    def test_capture_digest_window_replay_and_device_join(self):
        fixture=capture_fixture.TestCapture();fixture.setUp()
        try:
            torch=capture_fixture.FakeTorch();o=fixture.observer(torch);fixture.drive(o,torch)
            obs=d.read_recording(fixture.root,fixture.identity,fixture.menu,o.summary_sha256)
            device=dict(rank=0,pid=100,worker_identity_sha256=fixture.identity["worker_identity_sha256"],
                        device_uuid="GPU-fixture-only",trace_device_index=0)
            device_raw=d.canonical(device)
            args=(fixture.root,obs,fixture.menu,o.capture_sha256,o.backend.mount,device_raw,d.sha(device_raw))
            result=r.read_capture(*args)
            self.assertEqual(result["status"],"CAPTURE_METADATA_AND_CORRELATIONS_JOINED")
            self.assertEqual(len(result["windows"]),7);self.assertFalse(result["diagnostic_success"])
            extra=fixture.root/"CAPTURE-extra.json";extra.write_text("{}")
            with self.assertRaisesRegex(ValueError,"unexpected capture artifacts"):r.read_capture(*args)
            extra.unlink()
            with self.assertRaises(ValueError):r.read_capture(*args[:3],"0"*64,*args[4:])
            device["trace_device_index"]=1;bad=d.canonical(device)
            with self.assertRaises(ValueError):r.read_capture(*args[:5],bad,d.sha(bad))
            p=fixture.root/"CAPTURE-rank-0.json";report=json.loads(p.read_bytes())
            for mutate,reason in ((lambda x:x.update(schema=2),"capture schema"),
                                  (lambda x:x.update(trace_join_verified=True),"capture schema"),
                                  (lambda x:x["windows"][0].update(sequences=[999]),"window replay")):
                bad=copy.deepcopy(report);mutate(bad);raw=d.canonical(bad);p.write_bytes(raw)
                with self.assertRaisesRegex(ValueError,reason):r.read_capture(*args[:3],d.sha(raw),*args[4:])
        finally:fixture.tearDown()


if __name__=="__main__":unittest.main(verbosity=2)
