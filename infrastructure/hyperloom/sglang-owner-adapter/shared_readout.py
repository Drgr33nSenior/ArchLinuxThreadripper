"""Additional observation joins after the existing owner authenticates its inputs.

Pins must come from producer/owner receipts, never be manufactured by this reader.
This module does not replace the owner's wire, quality, source or release readers.
"""
from collections import Counter,defaultdict
from decimal import Decimal
from pathlib import Path
import statistics

from shared_diagnostic import REPORT_BYTES,canonical,hex64,identity_check,integer,join_ranks,menu_check,need,profiled,sha,_json,_file
from shared_trace_io import RAW_LIMIT,WINDOWS,join_annotations,number,unpack
from shared_capture import Windows


def read_capture(directory,recording,menu,capture_sha256,expected_raw_mount,device_raw=None,device_sha256=None):
    """Replay fixed-window selection and bind export files to the owner-pinned report."""
    directory=Path(directory);need(directory.resolve()==directory,"canonical capture root")
    need(recording["status"]=="COMPLETE" and recording["menu_sha256"]==sha(canonical(menu)),"complete inventory required")
    ident=recording["identity"];rank=ident["rank"]
    need({p.name for p in directory.glob("CAPTURE-*")} <=
         {"CAPTURE-rank-0.json","CAPTURE-rank-1.json"},"unexpected capture artifacts")
    raw=_file(directory,f"CAPTURE-rank-{rank}.json",REPORT_BYTES)
    need(sha(raw)==hex64(capture_sha256),"owner-pinned capture digest")
    report=_json(raw)
    need(type(report["schema"]) is int and report["schema"]==1 and
         report["trace_join_verified"] is False,"capture schema")
    need(report["identity"]==ident and report["core_summary_sha256"]==recording["summary_sha256"] and
         report["errors"]==[] and report["diagnostic_success"] is False and report["gpu_authority"] is False,
         "capture / worker / inventory join")
    need(report["raw_mount"]==expected_raw_mount and report["added_explicit_device_syncs"]==0 and
         report["profiler_internal_synchronization_possible"] is True,"capture resource declaration")
    windows=Windows(ident,menu)
    for row in recording["batches"]:windows.before(row);windows.after(row)
    need(report["windows"]==windows.finish(),"fixed-window replay mismatch")
    if not profiled(ident):
        need(report["status"]=="NOT_PROFILED" and report["exports"]==[] and expected_raw_mount is None and
             not list(directory.glob(f"TRACE-rank-{rank}-*")),"unprofiled start")
        return {"status":"UNPROFILED_INVENTORY_JOINED","capture_sha256":capture_sha256,"diagnostic_success":False}
    need(report["status"]=="CAPTURE_EXPORT_COMPLETE" and len(report["exports"])==7 and
         {e["window"] for e in report["exports"]}==set(WINDOWS),"complete seven-window exports")
    expected=set();results=[]
    for entry in report["exports"]:
        window=entry["window"];stem=f"TRACE-rank-{rank}-{window}"
        attempt=_json(_file(directory,stem+".attempt.json",4096))
        transport=_json(_file(directory,stem+".export.json",4096))
        need(attempt=={"window":window,"rank":rank,"retry":False} and transport==entry["transport"] and
             entry["status"]=="COMPLETE" and entry["window_observation_complete"] is True and
             entry["producer_hard_bound_verified"] is True and entry["profiler_export_error"] is None,"complete single attempt")
        trace=unpack(directory,transport)
        joined=join_device_trace(trace,recording,window,device_raw,device_sha256)
        selected=next(r for r in report["windows"] if r["window"]==window)
        need([r["sequence"] for r in joined["rounds"]]==selected["sequences"],"trace uses fixed selected rounds")
        results.append({"window":window,"raw_sha256":transport["raw_sha256"],
                        "joined":joined,"census":trace_census(trace)})
        expected.update((stem+".attempt.json",stem+".export.json",stem+".json.gz"))
    need({p.name for p in directory.glob(f"TRACE-rank-{rank}-*")}==expected,"exact trace artifact membership")
    result={"status":"CAPTURE_METADATA_AND_CORRELATIONS_JOINED","capture_sha256":capture_sha256,
            "identity":ident,"menu_sha256":recording["menu_sha256"],
            "windows":results,"diagnostic_success":False,"gpu_authority":False}
    need(len(canonical(result))<=1<<20,"offline capture readout bound")
    return result


def start_binding(identity):
    identity_check(identity)
    keys=("start","nonce","session_sha256","source_manifest_sha256","control","power_cap_watts")
    if identity["schema"]==2:keys+=("role","members","profiled")
    return {k:identity[k] for k in keys}


def join_http(recording,menu,summary_raw,summary_sha256,receipt_raw,receipt_sha256):
    """Require a separately owner-pinned HTTP receipt, bound at the actual start."""
    menu_check(menu)
    need(recording["status"]=="COMPLETE" and recording["menu_sha256"]==sha(canonical(menu)),"complete bound inventory")
    need(type(summary_raw) is bytes and len(summary_raw)<=16<<20 and
         sha(summary_raw)==hex64(summary_sha256),"owner-pinned HTTP summary")
    need(type(receipt_raw) is bytes and len(receipt_raw)<=4096 and
         sha(receipt_raw)==hex64(receipt_sha256),"owner-pinned HTTP start receipt")
    receipt=_json(receipt_raw)
    need(set(receipt)=={"schema","start_binding","menu_sha256","summary_sha256"} and
         type(receipt["schema"]) is int and receipt["schema"]==1,"HTTP receipt schema")
    need(receipt["start_binding"]==start_binding(recording["identity"]) and
         receipt["menu_sha256"]==recording["menu_sha256"] and
         receipt["summary_sha256"]==summary_sha256,"HTTP receipt / start binding")
    summary=_json(summary_raw)
    need(summary["stopped_early"] is False and summary["paired_error"] is None and
         summary["executed_case_count"]==summary["frozen_case_count"]==30 and len(summary["rows"])==30,"complete HTTP menu")
    rounds=Counter();correct=Counter();first_prefix={}
    for row in recording["batches"]:
        for pos,idx in enumerate(row["members"]):
            if row["phase"]=="prefill":first_prefix.setdefault(idx,row["prefix_lens"][pos])
            else:rounds[idx]+=1;correct[idx]+=row["correct_drafts_cpu"][pos]
    responses=[]
    for idx,(plan,row) in enumerate(zip(menu,summary["rows"],strict=True)):
        need(type(row["ordinal"]) is int and row["ordinal"]==idx,"ordered HTTP menu")
        wire,contract=row["wire"],row["contract"]
        need(wire["status"]==200 and wire["response_observed"] is True and wire["response_truncated"] is False,"complete HTTP wire")
        need(row["body_sha256"]==contract["request_sha256"]==plan["body_sha256"] and
             row["case"]==contract["case"] and contract["prompt_token_ids_sha256"]==plan["input_ids_sha256"],"request/input binding")
        need(contract["status"]=="NORMAL_STOP_COMPLETE" and contract["finish_reason"]=="stop" and
             contract["metric_eligible"] is True and contract["id_count_equals_reported_count"] is True,"natural complete response")
        need(hex64(wire["response_sha256"])==hex64(contract["raw_http_response_sha256"]),"wire response binding")
        meta=contract["terminal_meta_info"];usage=contract["usage"];spec=contract["spec_tokens_details"]
        need(meta["id"]==plan["rid"],"native RID / HTTP identity")
        prompt=integer(usage["prompt_tokens"],1,131040);output=integer(usage["completion_tokens"],1,4096)
        need(prompt==plan["input_tokens"]==contract["prompt_token_count"]==meta["prompt_tokens"],"prompt count")
        need(output==contract["response_id_count"]==contract["reported_completion_tokens"]==meta["completion_tokens"]==
             recording["final_output_counts"][idx],"visible output / HTTP count")
        cache=integer(contract["cached_tokens"],0,prompt-1)
        need(cache==meta["cached_tokens"]==first_prefix[idx],"initial prefill / HTTP cache join")
        need(integer(meta["num_retractions"])==0,"unmodelled retraction")
        for field,observed in (("spec_verify_ct",rounds[idx]),("spec_num_correct_drafts",correct[idx])):
            need(integer(spec[field])==integer(meta[field])==observed,"native round counter / HTTP join")
        responses.append({"ordinal":idx,"rid":plan["rid"],"output_tokens":output,"cached_tokens":cache,
                          "verify_rounds":rounds[idx],"correct_drafts":correct[idx],
                          "output_ids_sha256":hex64(contract["response_token_ids_sha256"]),
                          "wire_sha256":wire["response_sha256"]})
    return {"status":"JOINED_HTTP_COUNTS_ONLY","summary_sha256":summary_sha256,
            "receipt_sha256":receipt_sha256,"start_binding":receipt["start_binding"],
            "core_summary_sha256":recording["summary_sha256"],"identity":recording["identity"],
            "menu_sha256":recording["menu_sha256"],"responses":responses,
            "upstream_wire_quality_release_validation_required":True,"diagnostic_success":False,"gpu_authority":False}


def compare_starts(first,second,first_http,second_http):
    need(len(first)==len(second)==len(first_http)==len(second_http)==2,"two ranks per start")
    need(join_ranks(*first)["status"]==join_ranks(*second)["status"]=="COMPLETE","complete within-start joins")
    for pair in (first_http,second_http):
        need(hex64(pair[0]["summary_sha256"])==hex64(pair[1]["summary_sha256"]) and
             hex64(pair[0]["receipt_sha256"])==hex64(pair[1]["receipt_sha256"]),
             "both ranks require the same HTTP summary and receipt")
    need(first_http[0]["summary_sha256"]!=second_http[0]["summary_sha256"] and
         first_http[0]["receipt_sha256"]!=second_http[0]["receipt_sha256"],
         "different starts require distinct HTTP evidence")
    same=True
    fields=("phase","case","repetition","members","before","after","prefix_lens","extend_lens",
            "correct_drafts_cpu","verify_width","can_run_cuda_graph")
    for a,b,ha,hb in zip(first,second,first_http,second_http,strict=True):
        need(a["identity"]["start"]==1 and b["identity"]["start"]==2 and
             a["identity"]["nonce"]!=b["identity"]["nonce"],"distinct ordered starts")
        for key in ("rank","session_sha256","source_manifest_sha256","control","power_cap_watts"):
            need(a["identity"][key]==b["identity"][key],"same control and session")
        need(a["menu_sha256"]==b["menu_sha256"],"identical start menu")
        for rec,http in ((a,ha),(b,hb)):
            need(http["status"]=="JOINED_HTTP_COUNTS_ONLY" and http["core_summary_sha256"]==rec["summary_sha256"] and
                 http["identity"]==rec["identity"] and http["menu_sha256"]==rec["menu_sha256"] and
                 http["start_binding"]==start_binding(rec["identity"]),"HTTP/readout join")
        work=lambda rec:[{k:row[k] for k in fields} for row in rec["batches"]]
        outputs=lambda http:[{k:v for k,v in row.items() if k!="wire_sha256"} for row in http["responses"]]
        same=same and work(a)==work(b) and outputs(ha)==outputs(hb)
    return {"status":"SAME_OBSERVED_WORK" if same else "NOT_COMPARABLE","model_instantiated":False,
            "clock_alignment":False,"timing_transfer_established":False,"diagnostic_success":False}


def compare_roles(first,second,first_http,second_http):
    """Matched schema-2 starts: per-request token/speculative identity gates; schedule is report-only."""
    need(len(first)==len(second)==len(first_http)==len(second_http)==2,"two ranks per start")
    need(join_ranks(*first)["status"]==join_ranks(*second)["status"]=="COMPLETE","complete within-start joins")
    roles=[]
    for a,b,ha,hb in zip(first,second,first_http,second_http,strict=True):
        ia,ib=a["identity"],b["identity"]
        identity_check(ia);identity_check(ib)  # one authority: role, members and frozen start order
        need(ia["schema"]==ib["schema"]==2 and ia["start"]==1 and ib["start"]==2 and ia["nonce"]!=ib["nonce"],
             "matched ordered role starts")
        for key in ("rank","session_sha256","source_manifest_sha256","power_cap_watts"):
            need(ia[key]==ib[key],"same session across roles")
        need(a["menu_sha256"]==b["menu_sha256"],"identical role menu")
        for rec,http in ((a,ha),(b,hb)):
            need(http["status"]=="JOINED_HTTP_COUNTS_ONLY" and http["core_summary_sha256"]==rec["summary_sha256"]
                 and http["start_binding"]==start_binding(rec["identity"]),"HTTP/readout join")
        roles.append((ia["role"],ib["role"]))
    outputs=lambda http:[{k:v for k,v in row.items() if k!="wire_sha256"} for row in http["responses"]]
    same_requests=all(outputs(x)==outputs(y) for x,y in zip(first_http,second_http))
    fields=("phase","case","repetition","members","before","after","prefix_lens","extend_lens",
            "correct_drafts_cpu","verify_width","can_run_cuda_graph")
    work=lambda rec:[{k:row[k] for k in fields} for row in rec["batches"]]
    same_schedule=all(work(x)==work(y) for x,y in zip(first,second))
    return {"status":"SAME_REQUEST_WORK" if same_requests else "NOT_COMPARABLE","roles":roles[0],
            "same_batch_schedule_report_only":same_schedule,"clock_alignment":False,
            "timing_transfer_established":False,"diagnostic_success":False}


def trace_census(raw):
    """Keep interval unions separate from duration sums, including anomalous streams."""
    need(type(raw) is bytes and len(raw)<=RAW_LIMIT,"trace byte bound")
    trace=_json(raw);events=trace["traceEvents"]
    need(type(events) is list and len(events)<=250000,"trace event bound")
    kernels=[e for e in events if e.get("cat")=="kernel" and e.get("ph")=="X"]
    need(kernels,"GPU kernel records required")
    streams=defaultdict(list);symbols=defaultdict(list);intervals=[]
    for e in kernels:
        start=number(e["ts"]);duration=number(e["dur"]);need(duration>0,"positive kernel duration")
        args=e["args"];device=integer(args["device"]);stream=integer(args["stream"])
        name=e["name"];need(type(name) is str and 0<len(name)<=8192,"bounded kernel symbol")
        interval=(start,start+duration);streams[(device,stream)].append(interval);intervals.append(interval)
        symbols[name].append(duration)
    need(len(symbols)<=4096 and len(streams)<=256,"bounded symbol/stream census")
    def union(rows):
        total=Decimal(0);end=None
        for start,stop in sorted(rows):
            if end is None or start>=end:total+=stop-start
            elif stop>end:total+=stop-end
            end=stop if end is None else max(stop,end)
        return total
    overlaps={}
    for key,rows in streams.items():
        prior=None;count=0
        for start,end in sorted(rows):
            if prior is not None and start<prior:count+=1
            prior=end if prior is None else max(prior,end)
        overlaps[f"{key[0]}:{key[1]}"]=count
    need(len({device for device,stream in streams})==1,"one device per rank census")
    result={"kernel_records":len(kernels),"duration_sum_us":str(sum((end-start for start,end in intervals),Decimal(0))),
            "interval_union_us":str(union(intervals)),"stream_overlap_records":overlaps,
            "symbols":{name:{"count":len(values),"median_us":str(statistics.median(values)),
                             "minimum_us":str(min(values)),"maximum_us":str(max(values))} for name,values in sorted(symbols.items())},
            "start_order_causal_attribution_allowed":False,"sum_is_execution_cost":False,
            "utilization_established":False,"critical_path_established":False}
    need(len(canonical(result))<=64<<10,"offline per-window census bound")
    return result


def compare_rank_traces(left_capture,right_capture,window):
    """Compare one fixed window from two successful read_capture results."""
    need(window in WINDOWS,"fixed comparison window")
    a,b=left_capture["identity"],right_capture["identity"]
    need(a["rank"]==0 and b["rank"]==1 and a["start"]==b["start"] and profiled(a) and profiled(b),
         "profiled trace ranks")
    need(start_binding(a)==start_binding(b) and
         hex64(left_capture["menu_sha256"])==hex64(right_capture["menu_sha256"]),"same trace start and menu")
    entries=[]
    for capture in (left_capture,right_capture):
        need(capture["status"]=="CAPTURE_METADATA_AND_CORRELATIONS_JOINED","joined captures required")
        hex64(capture["capture_sha256"])
        need(len(capture["windows"])==7 and {x["window"] for x in capture["windows"]}==set(WINDOWS),
             "complete fixed capture inventory")
        row=next(x for x in capture["windows"] if x["window"]==window)
        need(row["joined"]["status"]=="JOINED_CPU_GRAPH_API_ONLY" and
             row["joined"]["window"]==window,"joined fixed window")
        hex64(row["raw_sha256"]);hex64(row["joined"]["device_binding_sha256"])
        entries.append(row)
    uuids=[row["joined"]["device_uuid"] for row in entries]
    need(all(type(x) is str and 0<len(x)<=128 for x in uuids) and uuids[0]!=uuids[1],
         "distinct rank devices")
    need([x["members"] for x in entries[0]["joined"]["rounds"]]==
         [x["members"] for x in entries[1]["joined"]["rounds"]],"same window request membership")
    left,right=[row["census"] for row in entries]
    common=sorted(set(left["symbols"])&set(right["symbols"]))
    result={"window":window,"start_binding":start_binding(a),"device_uuids":uuids,
            "capture_sha256":[left_capture["capture_sha256"],right_capture["capture_sha256"]],
            "raw_sha256":[row["raw_sha256"] for row in entries],
            "rank_census":[left,right],"common_symbols":[{"name":name,"rank0":left["symbols"][name],"rank1":right["symbols"][name]} for name in common],
            "only_rank0":sorted(set(left["symbols"])-set(common)),"only_rank1":sorted(set(right["symbols"])-set(common)),
            "clocks_aligned":False,"paired_kernel_instances_inferred":False,"speedup_claim":False}
    need(len(canonical(result))<=1<<20,"offline rank comparison bound")
    return result


def join_device_trace(raw,recording,window,device_binding_raw,device_binding_sha256):
    need(type(device_binding_raw) is bytes and len(device_binding_raw)<=4096 and
         sha(device_binding_raw)==hex64(device_binding_sha256),"owner-pinned device namespace")
    binding=_json(device_binding_raw);identity=recording["identity"]
    need(set(binding)=={"rank","pid","worker_identity_sha256","device_uuid","trace_device_index"},"device binding fields")
    need(all(binding[k]==identity[k] for k in ("rank","pid","worker_identity_sha256")),"worker/device join")
    need(type(binding["device_uuid"]) is str and 0<len(binding["device_uuid"])<=128,"bounded device UUID")
    device=integer(binding["trace_device_index"])
    joined=join_annotations(raw,identity,recording["batches"],window)
    kernels=[e for e in _json(raw)["traceEvents"] if e.get("cat")=="kernel" and e.get("ph")=="X"]
    need(kernels and all(e.get("args",{}).get("device")==device for e in kernels),"trace device namespace drift")
    joined.update(device_binding_sha256=device_binding_sha256,device_uuid=binding["device_uuid"],
                  device_identity_source="owner-supplied authenticated worker binding")
    return joined
