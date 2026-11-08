"""Frozen F1 component contract; no registration or execution entry."""
import json
import math
from pathlib import Path
import statistics

ROOT=Path(__file__).resolve().parent
MODE='isolated-gfx1201-gatednorm-mxfp4-component-v1'
PROVENANCE='synthetic-exact-gatednorm-mxfp4-two-cards-v1'
TORCH_VERSION='2.14.0+git2e66fe81.rocm10.2.0'
HIP_VERSION='7.17.26416'
CARDS=(0,1)
TOKENS=(8,16)
STAGES=('norm','candidate','quantizer','gemm')
ARMS=('B','C')
LAYERS=48
SEED=20261103
WORKER_SECONDS,CARD_SECONDS,OWNER_SECONDS,CALLER_SECONDS=600,280,1500,2190
DEVICE_LIMIT=6<<30
POWER_CAP_MICROWATTS=235000000
WARMUP_CALLS=TIMING_ROUNDS=6
INPUT_PINS=json.loads((ROOT/'INPUT-PINS.json').read_bytes())
COMPILER_PINS=json.loads((ROOT/'COMPILER-PINS.json').read_bytes())
LAUNCHES=json.loads((ROOT/'LAUNCHES.json').read_bytes())
KINDS=('finite','zeros','subnormal','rounding','x-special','z-special','w-special',
       'x-all-0','x-all-1','x-all-2','z-all-0','z-all-1','z-all-2')
CASES=tuple(dict(name=f't{t}-{kind}',tokens=t,kind=kind,
                finite=kind in ('finite','zeros','subnormal','rounding')) for t in TOKENS for kind in KINDS)
TIMED=tuple(f't{t}-{mode}' for t in TOKENS for mode in ('boundary','inclusive'))
POISONS=({'byte':0xA5,'bf16':0x7FC1,'fp32':0x7FC00001},
         {'byte':0x5A,'bf16':0xFFC2,'fp32':0xFFC00002})

def bounds():
    return dict(cards=list(CARDS),tokens=list(TOKENS),cases=list(CASES),timed=list(TIMED),layers=LAYERS,
      worker_seconds=WORKER_SECONDS,card_seconds=CARD_SECONDS,owner_seconds=OWNER_SECONDS,caller_seconds=CALLER_SECONDS,
      allocated_cap=DEVICE_LIMIT,reserved_cap=DEVICE_LIMIT,device_api_count=32,rows_per_block=4,
      power_cap_microwatts=POWER_CAP_MICROWATTS,warmups=WARMUP_CALLS,timing_rounds=TIMING_ROUNDS,
      poisons=list(POISONS),numerics='exact bytes including exceptional outputs; finite-case veto',
      model_starts=0,http_requests=0,representative_activations=False)

def order(round_index):return ARMS if round_index%2==0 else ARMS[::-1]

def usefulness(samples):
    expected=[(case,r,p,a) for case in TIMED for r in range(TIMING_ROUNDS) for p,a in enumerate(order(r))]
    if [(v['case'],v['round'],v['position'],v['arm']) for v in samples]!=expected:
        raise ValueError('exact mirrored complete timing menu')
    if any(type(v['elapsed_ms']) not in (int,float) or not math.isfinite(v['elapsed_ms']) or v['elapsed_ms']<=0 for v in samples):
        raise ValueError('positive finite timing')
    rows=[]
    for case in TIMED:
        values={a:[v['elapsed_ms'] for v in samples if v['case']==case and v['arm']==a] for a in ARMS}
        med={a:statistics.median(v) for a,v in values.items()};spread={a:max(v)-min(v) for a,v in values.items()}
        gap=med['B']-med['C'];noise=max(spread.values());saving=gap/med['B']
        rows.append(dict(case=case,baseline_median_ms=med['B'],candidate_median_ms=med['C'],
         baseline_range_ms=spread['B'],candidate_range_ms=spread['C'],median_gap_ms=gap,
         saving=saving,noise_bound_ms=noise,resolved_gain=gap>noise,resolved_regression=-gap>noise,
         standalone_pass=saving>=0.1 and gap>noise))
    useful=all(v['standalone_pass'] for v in rows)
    retained=all(v['median_gap_ms']>0 and not v['resolved_regression'] for v in rows)
    return dict(rows=rows,useful=useful,cumulative='USEFUL' if useful else 'RETAINED_POSITIVE' if retained else 'REJECT')

def cumulative(cards):
    if len(cards)!=2 or any(c['candidate_status']!='CORRECT' for c in cards):return 'REJECT'
    values=[c['usefulness']['cumulative'] for c in cards]
    return 'REJECT' if 'REJECT' in values else 'USEFUL' if values==['USEFUL','USEFUL'] else 'RETAINED_POSITIVE'
