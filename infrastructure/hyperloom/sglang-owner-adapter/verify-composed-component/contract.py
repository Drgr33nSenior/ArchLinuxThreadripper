"""Frozen four-arm prefetch/split composition; no execution authority."""
import math

MODE = 'isolated-gfx1201-verify-composed-component-v1'
PROVENANCE = 'seeded-bf16-verification-prefetch-split-factorial-two-cards-v1'
ARMS = ('B', 'P', 'S', 'C')
CAPS = {'B': 16, 'P': 16, 'S': 32, 'C': 32}
SOURCE_INDEX = {'B': 0, 'P': 1, 'S': 0, 'C': 1}
OUTPUTS = {'B': 'o_base', 'P': 'o_prefetch', 'S': 'o_split', 'C': 'o'}
POWER_CAP_MICROWATTS = 235000000
def arm_cap(arm):
    if type(arm) is not str or arm not in CAPS:raise ValueError('exact split arm')
    return CAPS[arm]
TORCH_VERSION = '2.14.0+git2e66fe81.rocm10.2.0'
HIP_VERSION = '7.17.26416'
BASELINE_SHA256 = '34ca0066e2716e1ffad048ec7cbb3a25af13eeb61e99034a663fed6277bf691a'
CARDS = (0, 1)
LAYERS, Q_HEADS, KV_HEADS, HEAD_DIM, CHUNK = 16, 12, 2, 256, 16
SM_SCALE = HEAD_DIM ** -0.5
BASELINE = (32, 16, 4, 1)
HIP_EXTRAS = dict(waves_per_eu=1, matrix_instr_nonkdim=16, kpack=2)
SEED = 20261031
SLOTS = 120833
KV_BYTES = LAYERS * 2 * SLOTS * KV_HEADS * HEAD_DIM * 2
EXTEND_BYTES = LAYERS * CHUNK * Q_HEADS * HEAD_DIM * 2
OUTPUT_BYTES = len(ARMS) * LAYERS * CHUNK * Q_HEADS * HEAD_DIM * 2
INDEX_CAPACITY = CHUNK * (131040 + 8)
INDEX_BYTES = INDEX_CAPACITY * 8 + (CHUNK + 1) * 4 + CHUNK * 8
PARTIAL_BYTES = CHUNK * Q_HEADS * sum(CAPS.values()) * (HEAD_DIM + 1) * 4
RESIDENT_BYTES = KV_BYTES + EXTEND_BYTES + OUTPUT_BYTES + len(ARMS) * INDEX_BYTES + PARTIAL_BYTES
REFERENCE_BLOCK = 1
REFERENCE_WORKSPACE_BYTES = (3 * REFERENCE_BLOCK * (SLOTS-1) * 4
                             + 2 * (SLOTS-1) * HEAD_DIM * 4
                             + 2 * (SLOTS-1) * KV_HEADS * HEAD_DIM * 2)
DEVICE_LIMIT = 6 << 30
ALLOCATION_SLACK = 64 << 20
WORKER_SECONDS, CARD_SECONDS = 600, 280
WARMUP_CALLS, TIMING_ROUNDS = 6, 6
TIMING_HEADROOM, CANDIDATE_COST_FACTOR, USEFUL_SAVING = 1.5, 2.0, 0.10
MEAN_ERROR_FACTOR = 1.02
MEASUREMENT = 'unprofiled device-event milliseconds for one per-row split-helper pass and 16 sequential verification layers including both stages; not serving latency'

CASES = ({'name': 'p119990-rows8-sequential', 'prefixes': [119990], 'queries': 8, 'indices': 'sequential'}, {'name': 'p119990-rows8-even-odd', 'prefixes': [119990], 'queries': 8, 'indices': 'even-odd'}, {'name': 'p59993-59980-rows16', 'prefixes': [59993, 59980], 'queries': 16, 'indices': 'sequential'}, {'name': 'p724-rows8', 'prefixes': [724], 'queries': 8, 'indices': 'sequential'})
PRIMARY = ('p119990-rows8-sequential',)
TIMED = tuple(row['name'] for row in CASES)


def bf16_ulp(value):
    if not math.isfinite(value) or value <= 0:
        raise ValueError('positive finite reference magnitude')
    return 2.0 ** (math.floor(math.log2(value)) - 7)


def valid_errors(row):
    return (type(row) is dict and row.get('finite') is True
            and all(type(row.get(k)) in (int,float) and math.isfinite(row[k]) and row[k]>=0
                    for k in ('max_abs','mean_abs')))


def within_tolerance(candidate, baseline, reference_max):
    return (valid_errors(candidate) and valid_errors(baseline)
            and candidate['max_abs'] <= baseline['max_abs'] + bf16_ulp(reference_max)
            and candidate['mean_abs'] <= MEAN_ERROR_FACTOR * baseline['mean_abs'])


def timing_order(round_index):
    if type(round_index) is not int or not 0<=round_index<TIMING_ROUNDS:
        raise ValueError('frozen timing round')
    # Every pair occurs in each order three times. Rotate each mirrored pair.
    offset=round_index//2
    order=ARMS[offset:]+ARMS[:offset]
    return tuple(reversed(order)) if round_index%2 else order


def median(values):
    values=sorted(values);n=len(values)
    return values[n//2] if n%2 else (values[n//2-1]+values[n//2])/2


COMPARISONS = {'prefetch16': ('B','P'), 'split_stock': ('B','S'),
               'combined': ('B','C'), 'split_prefetch': ('P','C'),
               'prefetch32': ('S','C')}
COMBINED_VETOES = ('combined','split_prefetch','prefetch32')


def usefulness(timings):
    expected=[(case,r,pos,arm) for case in TIMED for r in range(TIMING_ROUNDS)
              for pos,arm in enumerate(timing_order(r))]
    if [(t['case'],t['round'],t['position'],t['arm']) for t in timings] != expected:
        raise ValueError('exact four-arm mirrored timing menu')
    if any(type(t['elapsed_ms']) not in (int,float) or not math.isfinite(t['elapsed_ms']) or t['elapsed_ms']<=0
           or t['measurement']!=MEASUREMENT for t in timings):
        raise ValueError('positive finite unprofiled event samples')
    comparisons={}
    for label,(control,candidate) in COMPARISONS.items():
        rows=[]
        for case in TIMED:
            b=[t['elapsed_ms'] for t in timings if t['case']==case and t['arm']==control]
            c=[t['elapsed_ms'] for t in timings if t['case']==case and t['arm']==candidate]
            mb,mc=median(b),median(c);br=max(b)-min(b);cr=max(c)-min(c);spread=max(br,cr)
            saving=1-mc/mb
            rows.append(dict(case=case,control=control,candidate=candidate,
                baseline_median_ms=mb,candidate_median_ms=mc,baseline_range_ms=br,candidate_range_ms=cr,
                median_saving=saving,median_gap_ms=mb-mc,noise_bound_ms=spread,primary=case in PRIMARY,
                passes=saving>=USEFUL_SAVING and mb-mc>spread,
                resolved_gain=mb-mc>spread,resolved_regression=mc-mb>spread))
        comparisons[label]=rows
    no_regression=not any(r['resolved_regression'] for key in COMBINED_VETOES for r in comparisons[key])
    primary=[r for r in comparisons['combined'] if r['primary']]
    positive=all(r['median_gap_ms']>0 for r in primary)
    standalone_useful=not any(r['resolved_regression'] for r in comparisons['combined']) and all(r['passes'] for r in primary)
    useful=no_regression and standalone_useful
    disposition='USEFUL' if useful else 'RETAINED_POSITIVE' if positive and no_regression else 'REJECT'
    return dict(comparisons=comparisons,rows=comparisons['combined'],useful=useful,
        standalone_10percent_screen=standalone_useful,
        no_regression=no_regression,primary_positive=positive,cumulative_disposition=disposition,
        cap32_incremental_primary_positive=all(r['median_gap_ms']>0 for r in comparisons['split_prefetch'] if r['primary']),
        cap32_incremental_primary_resolved=all(r['resolved_gain'] for r in comparisons['split_prefetch'] if r['primary']))


def cumulative(rows):
    if len(rows)!=len(CARDS) or [r['card'] for r in rows]!=list(CARDS):
        raise ValueError('both ordered cards before cumulative disposition')
    values=[r['usefulness']['cumulative_disposition'] for r in rows]
    if any(type(v) is not str or v not in ('USEFUL','RETAINED_POSITIVE','REJECT') for v in values):
        raise ValueError('closed cumulative verdict set')
    if any(v=='REJECT' for v in values):return 'REJECT'
    return 'USEFUL' if all(v=='USEFUL' for v in values) else 'RETAINED_POSITIVE'


def bounds():
    return dict(cards=list(CARDS),sequential=True,layers=LAYERS,q_heads=Q_HEADS,kv_heads=KV_HEADS,
                head_dim=HEAD_DIM,max_queries=CHUNK,slots=SLOTS,sm_scale=SM_SCALE,
                cases=list(CASES),primary=list(PRIMARY),timed=list(TIMED),seed=SEED,
                baseline=list(BASELINE),candidate=list(BASELINE),hip_extras=HIP_EXTRAS,
                sole_variable='four fixed combinations of stock/prefetch grouped stage1 and cap16/cap32 helper, buffers, grid and reduction',caps=CAPS,
                arms=list(ARMS),source_index=SOURCE_INDEX,outputs=OUTPUTS,
                power_cap_microwatts=POWER_CAP_MICROWATTS,power_observer='read-only owner admission and release; no power mutation',
                cumulative_vetoes=list(COMBINED_VETOES),comparisons={k:list(v) for k,v in COMPARISONS.items()},
                cumulative_rule='On both cards, positive combined primary gaps with no resolved regression versus B/P/S: USEFUL if both exceed 10 percent and larger range; otherwise RETAINED_POSITIVE. Any hard failure or nonpositive primary or resolved regression: REJECT.',
                numerical_layers=LAYERS,mean_error_factor=MEAN_ERROR_FACTOR,
                allocated_cap=DEVICE_LIMIT,reserved_cap=DEVICE_LIMIT,
                allocation_slack=ALLOCATION_SLACK,resident_bytes=RESIDENT_BYTES,
                reference_workspace_bytes=REFERENCE_WORKSPACE_BYTES,
                worker_seconds=WORKER_SECONDS,card_seconds=CARD_SECONDS,owner_seconds=1500,caller_seconds=2190,
                warmups=WARMUP_CALLS,timing_rounds=TIMING_ROUNDS,timing_headroom=TIMING_HEADROOM,
                useful_saving=USEFUL_SAVING,profiling=False,model_starts=0,http_requests=0)

CANDIDATE_SHA256 = 'ed26c67b69da358944999fdf9f769fafc4c2aef821335fbb777679b966e9e4e3'
COMPILER_PINS = {arm: ({'amdgcn': 'e1de5895628d1f4447d27904b1d0476e0397e1a7a1f085ea8c7f81831a8e7b09', 'hsaco': '617bee9affff4921b8005c7d5f39587f3ab1197c58a2c11788391240251c88ed'} if SOURCE_INDEX[arm]==0 else {'amdgcn':'29d9cfd899c7d1ede164c571b18192a5ca818f1705bde8d77ebde6115f136561','hsaco':'f9a52c0865a383f6a8fa08c2ab7633973d100f4e7cd1cdc9383bd277c4429309'}) for arm in ARMS}

METADATA_SHA256 = '10d4f1ac194e1fd999b58bbfcb2d1ad09bc25df289128925d2945108173afdc0'
STAGE2_PINS = {'B': {'amdgcn': 'fe96140167abf51096370590978070c2059f47badf735f1656bebc1740a28e4a', 'hsaco': 'd8ae9f5fd81f36c6d95f258d5e5b711f8ee5903444e63ff86303c4fa7d7160ff'},
               'C': {'amdgcn': '1324d1edf67ba134cf144da6a2d84ec852ede0c4ffa525d32691eeaacd85acbc', 'hsaco': '17b4cbf6a1bfde242fed416043c5ae1575f9b22aab28569272db25805d5d224e'}}
STAGE2_PINS['P']=dict(STAGE2_PINS['B'])
STAGE2_PINS['S']=dict(STAGE2_PINS['C'])
DYNAMIC_LDS_BYTES = 16384
STAGE2_DYNAMIC_LDS_BYTES = 0
