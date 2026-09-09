#!/usr/bin/env bash
set -euo pipefail

repo_root=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=lib/common.sh
source "$repo_root/lib/common.sh"
# shellcheck source=lib/workstation/runtime.sh
source "$repo_root/lib/workstation/runtime.sh"

work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
boot_id=11111111-2222-3333-4444-555555555555

unset LLAMA_CONTEXT_SIZE LLAMA_BATCH_SIZE LLAMA_UBATCH_SIZE LLAMA_FLASH_ATTN
unset LLAMA_BENCH_PROMPT_TOKENS LLAMA_BENCH_GENERATION_TOKENS LLAMA_BENCH_REPETITIONS
ws_llama_runtime_config_validate
ws_llama_benchmark_config_validate
[[ $LLAMA_CONTEXT_SIZE == 2048 && $LLAMA_BATCH_SIZE == 2048 && $LLAMA_UBATCH_SIZE == 512 && $LLAMA_FLASH_ATTN == auto ]]
[[ $LLAMA_BENCH_PROMPT_TOKENS == 512 && $LLAMA_BENCH_GENERATION_TOKENS == 128 && $LLAMA_BENCH_REPETITIONS == 3 ]]

cat > "$work/legacy-workstation.conf" <<'CONFIG'
LLM_DATA_DIR=/srv/ai/vllm/models
LLM_LISTEN=127.0.0.1:8000
LLM_SHM_SIZE=8g
DEFAULT_TUNED_PROFILE=ai
MAKE_JOBS=24
MEMORY_HEAVY_JOBS=16
CONFIG
ws_load_config "$work/legacy-workstation.conf"
[[ $LLAMA_CONTEXT_SIZE == 2048 && $LLAMA_BATCH_SIZE == 2048 && $LLAMA_UBATCH_SIZE == 512 && $LLAMA_FLASH_ATTN == auto ]]
[[ $LLAMA_BENCH_PROMPT_TOKENS == 512 && $LLAMA_BENCH_GENERATION_TOKENS == 128 && $LLAMA_BENCH_REPETITIONS == 3 ]]

make_report() {
  local output=$1
  mkdir -p -- "$output"
  printf '%s\n' "$boot_id" > "$output/boot-id.txt"
  jq -n '{schema:1,status:"observed",os:"Linux",architecture:"x86_64",expected:{gpu_count:2},gpu_target:"gfx1201",
    pci_gpus:[{bdf:"0000:01:00.0",device_id:"0x1234",driver:"amdgpu"},{bdf:"0000:02:00.0",device_id:"0x1234",driver:"amdgpu"}],
    rocm_agents:[{agent:"1",gfx:"gfx1201",uuid:"GPU-111"},{agent:"2",gfx:"gfx1201",uuid:"GPU-222"}]}' > "$output/hardware.json"
}

make_candidate() {
  local backend=$1 output=$2 devices=$3
  mkdir -p -- "$output/build/bin"
  cat > "$output/build/bin/llama-bench" <<STUB
#!/usr/bin/env bash
if [[ \${1:-} == --list-devices ]]; then
  printf '%s:\\n' ${devices//\// }
  exit 0
fi
if [[ \${BROKEN_METRICS:-} == "$backend" ]]; then printf '[]\\n'; exit 0; fi
while ((\$#)); do
  if [[ \$1 == --device ]]; then
    IFS=/ read -r -a selected <<< "\$2"
    for device in "\${selected[@]}"; do printf '%s model buffer size = 1 MiB\\n' "\$device" >&2; done
    break
  fi
  shift
done
printf '[{"n_prompt":512,"n_gen":0,"avg_ts":42.5,"stddev_ts":1.25,"samples_ts":[42,43,42.5],"samples_ns":[1,2,3]},{"n_prompt":0,"n_gen":128,"avg_ts":20,"stddev_ts":0,"samples_ts":[20,20,20],"samples_ns":[1,2,3]}]\\n'
STUB
  chmod +x "$output/build/bin/llama-bench"
  jq -n --arg backend "$backend" --arg hash "$(common::sha256_file "$output/build/bin/llama-bench")" \
    '{status:"built-not-qualified",backend:$backend,source:{commit:"427291b5b34cd914a31b3fd3b61a68f6184f4b9f",tree:"tree-1"},outputs:{llama_bench_sha256:$hash}}' \
    > "$output/build-result.json"
}

make_report "$work/recorded"
make_candidate hip "$work/hip" ROCm0/ROCm1
make_candidate vulkan "$work/vulkan" Vulkan0/Vulkan1
mkdir -p -- "$work/inference-build"
cat > "$work/inference-build/llama-cli" <<'STUB'
#!/usr/bin/env bash
case ${1:-} in
  --version) printf 'llama.cpp 427291b5\n' ;;
  --list-devices) printf 'ROCm0:\nROCm1:\n' ;;
  *) printf '%s\n' "$*" >> "$INFERENCE_LOG"; printf 'ROCm0 model buffer size = 1\nROCm1 model buffer size = 1\n' ;;
esac
STUB
chmod +x "$work/inference-build/llama-cli"
printf 'local test model\n' > "$work/model.gguf"

LLAMA_CONTEXT_SIZE=2048
LLAMA_BATCH_SIZE=2048
LLAMA_UBATCH_SIZE=512
LLAMA_FLASH_ATTN=auto
LLAMA_BENCH_PROMPT_TOKENS=512
LLAMA_BENCH_GENERATION_TOKENS=128
LLAMA_BENCH_REPETITIONS=3
EXPECTED_GPU_COUNT=2
ws_require_arch() { :; }
ws_require_user() { :; }
ws_rocm_unfiltered() { :; }
ws_build_config_validate() { :; }
ws_hardware_collect() { make_report "$1"; }
ws_read_lock() { printf '427291b5b34cd914a31b3fd3b61a68f6184f4b9f\n'; }
timeout() { shift; "$@"; }
ws_measure_command() {
  local output=$1
  shift 2
  mkdir "$output"
  "$@" > "$output/stdout.txt" 2> "$output/stderr.txt"
}
export INFERENCE_LOG="$work/inference.log"

ws_rocm_inference "$work/inference-build/llama-cli" "$work/model.gguf" "$work/inference-output" >/dev/null
grep -Fq -- '--ctx-size 2048' "$INFERENCE_LOG"
grep -Fq -- '--batch-size 2048' "$INFERENCE_LOG"
grep -Fq -- '--ubatch-size 512' "$INFERENCE_LOG"
grep -Fq -- '--flash-attn auto' "$INFERENCE_LOG"

ws_rocm_benchmark_llama "$work/recorded/hardware.json" \
  "$work/hip/build/bin/llama-bench" "$work/vulkan/build/bin/llama-bench" "$work/model.gguf" "$work/output" \
  ROCm0/ROCm1 Vulkan0/Vulkan1 >/dev/null

jq -e '(.status == "measured-not-qualified" and .configuration.repetitions == 3 and
  .configuration.context_size.scope == "recorded only; pinned llama-bench has no context option" and
  .configuration.cache_policy == "warm compute throughput; cold file-cache state uncontrolled" and
  .configuration.warmup == "native llama-bench warmup retained" and
  (.device_mapping.assertion | contains("operator-supplied")))' "$work/output/benchmark-result.json" >/dev/null
grep -Fq -- '--batch-size 2048' "$work/output/hip-command.txt"
grep -Fq -- '--repetitions 3' "$work/output/vulkan-command.txt"
if grep -Fq -- '--no-warmup' "$work/output/hip-command.txt"; then
  printf 'paired benchmark disabled native llama-bench warmup\n' >&2
  exit 1
fi
[[ $(tr '\n' ',' < "$work/output/order.txt") == '1 hip,1 vulkan,2 vulkan,2 hip,' ]]
ws_rocm_benchmark_llama "$work/recorded/hardware.json" \
  "$work/hip/build/bin/llama-bench" "$work/vulkan/build/bin/llama-bench" "$work/model.gguf" "$work/one-card" \
  ROCm0 Vulkan1 >/dev/null
grep -Fq -- '--split-mode none' "$work/one-card/hip-command.txt"
grep -Fq -- '--threads 12' "$work/one-card/hip-command.txt"

for broken in hip vulkan; do
  export BROKEN_METRICS=$broken
  if ws_rocm_benchmark_llama "$work/recorded/hardware.json" \
    "$work/hip/build/bin/llama-bench" "$work/vulkan/build/bin/llama-bench" "$work/model.gguf" "$work/broken-$broken" \
    ROCm0 Vulkan0 >/dev/null 2>&1; then
    printf 'unusable backend metrics produced benchmark success\n' >&2; exit 1
  fi
  [[ ! -e $work/broken-$broken/benchmark-result.json ]]
done
unset BROKEN_METRICS

if ws_rocm_benchmark_llama "$work/recorded/hardware.json" \
  "$work/hip/build/bin/llama-bench" "$work/vulkan/build/bin/llama-bench" "$work/model.gguf" "$work/bad-mapping" \
  ROCm0 Vulkan0/Vulkan1 >/dev/null 2>&1; then
  printf 'single-device mapping was accepted for a dual-GPU benchmark\n' >&2
  exit 1
fi

for candidate in hip vulkan; do
  jq '.source = {commit:"foreign-commit",tree:"same-foreign-tree"}' "$work/$candidate/build-result.json" > "$work/$candidate/foreign.json"
  mv -- "$work/$candidate/foreign.json" "$work/$candidate/build-result.json"
done
if ws_rocm_benchmark_llama "$work/recorded/hardware.json" \
  "$work/hip/build/bin/llama-bench" "$work/vulkan/build/bin/llama-bench" "$work/model.gguf" "$work/bad-lock" \
  ROCm0/ROCm1 Vulkan0/Vulkan1 >/dev/null 2>&1; then
  printf 'paired foreign source commit was accepted\n' >&2
  exit 1
fi

jq '.source = {commit:"427291b5b34cd914a31b3fd3b61a68f6184f4b9f",tree:"tree-1"}' "$work/hip/build-result.json" > "$work/hip/restore.json"
mv -- "$work/hip/restore.json" "$work/hip/build-result.json"
jq '.source = {commit:"427291b5b34cd914a31b3fd3b61a68f6184f4b9f",tree:"different-tree"}' "$work/vulkan/build-result.json" > "$work/vulkan/mismatch.json"

mv -- "$work/vulkan/mismatch.json" "$work/vulkan/build-result.json"
if ws_rocm_benchmark_llama "$work/recorded/hardware.json" \
  "$work/hip/build/bin/llama-bench" "$work/vulkan/build/bin/llama-bench" "$work/model.gguf" "$work/bad-source" \
  ROCm0/ROCm1 Vulkan0/Vulkan1 >/dev/null 2>&1; then
  printf 'mismatched source provenance was accepted\n' >&2
  exit 1
fi

# Each invalid document must fail independently of ordering. Include truncated
# output, multiple JSON documents, missing/duplicate/wrong workload cases.
cp "$work/output/1-hip/stdout.txt" "$work/good.json"
for invalid in '' '[]' '{}' '[' 'null' '[] []' \
  '[{"n_prompt":512,"n_gen":0,"avg_ts":42,"stddev_ts":1}]' \
  '[{"n_prompt":512,"n_gen":0,"avg_ts":0,"stddev_ts":1},{"n_prompt":0,"n_gen":128,"avg_ts":20,"stddev_ts":0}]' \
  '[{"n_prompt":512,"n_gen":0,"avg_ts":42,"stddev_ts":-1},{"n_prompt":0,"n_gen":128,"avg_ts":20,"stddev_ts":0}]' \
  '[{"n_prompt":512,"n_gen":0,"avg_ts":42,"stddev_ts":1},{"n_prompt":512,"n_gen":0,"avg_ts":20,"stddev_ts":0}]'; do
  printf '%s\n' "$invalid" > "$work/invalid.json"
  if (ws_llama_metrics_validate "$work/invalid.json" "$work/good.json") >/dev/null 2>&1 \
    || (ws_llama_metrics_validate "$work/good.json" "$work/invalid.json") >/dev/null 2>&1; then
    printf 'invalid paired measurement was accepted\n' >&2
    exit 1
  fi
done
ws_llama_metrics_validate "$work/good.json" "$work/good.json"
for mutation in '.[0].avg_ts=0' '.[0].stddev_ts=-1' '.[0].samples_ts=[]' \
  '.[0].samples_ns=[1,2]' '.[1]=.[0]' 'del(.[1])' '.[1].n_gen=64'; do
  jq "$mutation" "$work/good.json" > "$work/invalid.json"
  if (ws_llama_metrics_validate "$work/invalid.json" "$work/good.json") >/dev/null 2>&1 \
    || (ws_llama_metrics_validate "$work/good.json" "$work/invalid.json") >/dev/null 2>&1; then
    printf 'invalid measurement mutation was accepted: %s\n' "$mutation" >&2; exit 1
  fi
done
if (ws_llama_metrics_validate "$work/missing.json" "$work/good.json") >/dev/null 2>&1; then
  printf 'missing metrics file was accepted\n' >&2; exit 1
fi

LLAMA_FLASH_ATTN=invalid
if (ws_llama_benchmark_config_validate) >/dev/null 2>&1; then
  printf 'invalid flash attention setting was accepted\n' >&2
  exit 1
fi

printf 'ROCm paired llama.cpp benchmark tests passed\n'
