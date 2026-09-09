#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
if [[ -z ${HOME_LAB_PYTHON:-} ]]; then
  printf 'SKIP: set HOME_LAB_PYTHON for full quality invocation/parser fixtures\n'
  exit 0
fi
# shellcheck source=lib/common.sh
source "$root/lib/common.sh"
# shellcheck source=lib/workstation/runtime.sh
source "$root/lib/workstation/runtime.sh"
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
export WORKSTATION_PYTHON=$HOME_LAB_PYTHON PYTHONDONTWRITEBYTECODE=1
export QUALITY_ARG_LOG="$work/arguments.txt" QUALITY_CSV="$root/tests/fixtures/llama-quality/ops.csv"
EXPECTED_GPU_COUNT=2
ws_require_arch() { :; }
ws_require_user() { :; }
ws_rocm_unfiltered() { :; }
# Synthetic CLI fixtures have no ELF loader; real manifest regression is separate.
ws_llama_runtime_manifest() {
  if [[ $1 == capture ]]; then printf '{"fixture":true}\n' >"$3"; else jq -e '.fixture == true' "$3" >/dev/null; fi
}
ws_hardware_collect() {
  mkdir -p -- "$1"
  printf '11111111-2222-3333-4444-555555555555\n' >"$1/boot-id.txt"
  jq -n '{schema:1,status:"observed",os:"Linux",architecture:"x86_64",expected:{gpu_count:2},gpu_target:"gfx1201",
    pci_gpus:[{bdf:"0000:01:00.0",device_id:"0x1234",driver:"amdgpu"},{bdf:"0000:02:00.0",device_id:"0x1234",driver:"amdgpu"}],
    rocm_agents:[{agent:"1",gfx:"gfx1201",uuid:"GPU-111"},{agent:"2",gfx:"gfx1201",uuid:"GPU-222"}]}' >"$1/hardware.json"
}
ws_hardware_collect "$work/hardware"
printf 'fixture model\n' >"$work/model.gguf"
printf 'fixture corpus\n' >"$work/corpus.txt"
for backend in hip vulkan; do
  if [[ $backend == hip ]]; then QUALITY_BACKEND=ROCm; else QUALITY_BACKEND=Vulkan; fi
  export QUALITY_BACKEND
  candidate="$work/$backend"
  mkdir -p "$candidate/build/bin"
  for binary in llama-bench llama-perplexity test-backend-ops; do
    cp "$root/tests/fixtures/llama-quality/llama-tool" "$candidate/build/bin/$binary"
    chmod +x "$candidate/build/bin/$binary"
  done
  jq -n --arg backend "$backend" --arg commit "$(ws_read_lock ROCM_LLAMA_CPP_COMMIT)" \
    --arg hash "$(common::sha256_file "$candidate/build/bin/llama-bench")" \
    '{status:"built-not-qualified",backend:$backend,source:{commit:$commit,tree:"fixture-tree"},outputs:{llama_bench_sha256:$hash,llama_perplexity_sha256:$hash,backend_ops_sha256:$hash}}' \
    >"$candidate/build-result.json"
  ws_llama_runtime_seal "$candidate"
  # Real qualification function -> runner subprocess -> CLI stub -> CSV parser.
  for fixture in ops ops-with-skips; do
    export QUALITY_CSV="$root/tests/fixtures/llama-quality/$fixture.csv"
    output="$work/good-$backend-$fixture"
    unsupported=0
    [[ $fixture != ops-with-skips ]] || unsupported=4
    ws_rocm_qualify_llama "$work/hardware/hardware.json" "$candidate" "$work/model.gguf" "$work/corpus.txt" \
      "$output" "${QUALITY_BACKEND}0/${QUALITY_BACKEND}1"
    jq -e '.status == "numerical-checks-passed-model-review-required"' "$output/quality.json" >/dev/null
    for device in "${QUALITY_BACKEND}0" "${QUALITY_BACKEND}1"; do
      jq -e --argjson unsupported "$unsupported" '.supported_passes == 3 and .unsupported == $unsupported' \
        "$output/ops-$device/result.json" >/dev/null
    done
  done
  for split in row layer auto; do
    LLAMA_SPLIT_MODE=$split
    selection="${QUALITY_BACKEND}0/${QUALITY_BACKEND}1"
    expected=$split
    shares=1,1
    if [[ $split == auto ]]; then
      selection="${QUALITY_BACKEND}1"
      expected=none
      shares=1
    fi
    output="$work/split-$backend-$split"
    ws_rocm_qualify_llama "$work/hardware/hardware.json" "$candidate" "$work/model.gguf" "$work/corpus.txt" "$output" "$selection"
    jq -e --arg split "$expected" '.configuration.split_mode == $split and .configuration.threads == 12 and .quality_context_size == 2048' "$output/quality.json" >/dev/null
    tail -n 1 "$QUALITY_ARG_LOG" | grep -Fq -- "--split-mode $expected --tensor-split $shares --main-gpu 0"
  done
  LLAMA_SPLIT_MODE=auto
  # Binding to a paired experiment refuses changed settings/model/runtime.
  paired="$work/paired-$backend"
  mkdir "$paired"
  cp -R "$work/hardware" "$paired/hardware"
  ws_llama_effective_config "${QUALITY_BACKEND}0" >"$paired/$backend-effective-config.json"
  cp "$candidate/runtime-manifest.json" "$paired/$backend-runtime-manifest.json"
  jq -n --arg model "$(common::sha256_file "$work/model.gguf")" --arg commit "$(ws_read_lock ROCM_LLAMA_CPP_COMMIT)" \
    '{status:"measured-not-qualified",provenance:{model_sha256:$model,source_commit:$commit}}' >"$paired/benchmark-result.json"
  ws_rocm_qualify_llama "$work/hardware/hardware.json" "$candidate" "$work/model.gguf" "$work/corpus.txt" \
    "$work/bound-$backend" "${QUALITY_BACKEND}0" "$paired"
  for mismatch in settings model runtime; do
    case $mismatch in
      settings) LLAMA_THREADS=8 ;;
      model) printf 'changed model\n' >>"$work/model.gguf" ;;
      runtime) printf 'changed runtime\n' >"$paired/$backend-runtime-manifest.json" ;;
    esac
    if ws_rocm_qualify_llama "$work/hardware/hardware.json" "$candidate" "$work/model.gguf" "$work/corpus.txt" \
      "$work/mismatch-$backend-$mismatch" "${QUALITY_BACKEND}0" "$paired" >"$work/mismatch.log" 2>&1; then exit 1; fi
    [[ ! -e $work/mismatch-$backend-$mismatch/quality.json ]]
    LLAMA_THREADS=12
    printf 'fixture model\n' >"$work/model.gguf"
  done
  export QUALITY_CSV="$root/tests/fixtures/llama-quality/ops.csv"
  for failure in exit-failure malformed missing unsupported error ppl-failure ppl-malformed; do
    export QUALITY_CASE=$failure
    if ws_rocm_qualify_llama "$work/hardware/hardware.json" "$candidate" "$work/model.gguf" "$work/corpus.txt" \
      "$work/$backend-$failure" "${QUALITY_BACKEND}0" >"$work/rejected.txt" 2>&1; then
      printf 'quality failure accepted: %s/%s\n' "$backend" "$failure" >&2
      exit 1
    fi
    [[ ! -e $work/$backend-$failure/quality.json ]]
  done
  unset QUALITY_CASE
done
grep -Fq 'test-backend-ops test -b Vulkan1 -o MUL_MAT,RMS_NORM,SOFT_MAX --output csv' "$QUALITY_ARG_LOG"
grep -Fq -- '--device ROCm0,ROCm1' "$QUALITY_ARG_LOG"
printf 'HIP/Vulkan quality invocation, exit-status and pinned-CSV integration fixtures passed; no GPU execution\n'
