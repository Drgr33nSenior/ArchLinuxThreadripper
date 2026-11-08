#!/usr/bin/env bash
# Offline checks for the AI-stack pipeline inputs; never builds or contacts a host.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
dir=$root/infrastructure/ai-stack
lock=$dir/stack.lock
fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

# Every non-comment line is KEY=value, and no key repeats.
! grep -v -E '^(#.*|[A-Z][A-Z0-9_]*=.+)$' "$lock" | grep -q . || fail 'stack.lock has a malformed line'
[[ -z $(grep -v '^#' "$lock" | cut -d= -f1 | sort | uniq -d) ]] || fail 'stack.lock repeats a key'

# Sources are HTTPS repositories pinned to full commits; the image to a digest.
for name in THEROCK PYTORCH TORCHVISION TRITON SGLANG; do
  grep -Eq "^${name}_REPOSITORY=https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git$" "$lock" || fail "$name repository"
  grep -Eq "^${name}_COMMIT=[a-f0-9]{40}$" "$lock" || fail "$name commit is not a full SHA"
done
for image in THEROCK_BUILD_IMAGE RUNTIME_BASE_IMAGE; do
  grep -Eq "^$image=[a-z0-9./_-]+@sha256:[a-f0-9]{64}$" "$lock" || fail "$image is not pinned by digest"
done
grep -Eq '^SGLANG_PATCHSET_SHA256=[a-f0-9]{64}$' "$lock" || fail 'patch series hash'
grep -Eq '^IMAGE_REPOSITORY=registry\.lan\.thespry\.family/local/[a-z0-9-]+$' "$lock" || fail 'image repository'

# The patch-series hash is order-independent of the filesystem and content-sensitive.
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
mkdir "$work/a" "$work/b"
printf 'one\n' >"$work/a/0001-x.patch"
printf 'two\n' >"$work/a/0002-y.patch"
printf 'two\n' >"$work/b/0002-y.patch"
printf 'one\n' >"$work/b/0001-x.patch"
[[ $(bash "$dir/patchset-hash.sh" "$work/a") == $(bash "$dir/patchset-hash.sh" "$work/b") ]] || fail 'hash depends on creation order'
printf 'changed\n' >"$work/b/0002-y.patch"
[[ $(bash "$dir/patchset-hash.sh" "$work/a") != $(bash "$dir/patchset-hash.sh" "$work/b") ]] || fail 'hash ignores content'
mkdir "$work/empty"
if bash "$dir/patchset-hash.sh" "$work/empty" 2>/dev/null; then fail 'empty series accepted'; fi

# The runtime image installs only the resolved wheelhouse, offline, and checks it.
grep -Fq -- '--no-index --find-links /wheelhouse /wheelhouse/*.whl' "$dir/image/Containerfile" || fail 'runtime install is not offline'
grep -Fq 'pip check' "$dir/image/Containerfile" || fail 'runtime install is not dependency-checked'
grep -Eq '^GPU_TARGET=gfx[0-9a-f]+$' "$lock" || fail 'GPU target'
grep -Eq '^PYTHON_ABI=cp[0-9]+-cp[0-9]+t?$' "$lock" || fail 'Python ABI'

# Catalog entries pin exact revisions and a published hash for every file.
for entry in "$dir"/models/*.json; do
  python3 -I - "$entry" <<'PY' || fail "catalog entry: $entry"
import json, pathlib, re, sys
e = json.loads(pathlib.Path(sys.argv[1]).read_text())
assert e["schema"] == 1 and re.fullmatch(r"[a-z0-9][a-z0-9.-]+", e["alias"])
assert isinstance(e["serving"]["tp_size"], int) and e["serving"]["context_length"] > 0
for key in ("model", "draft"):
    if key not in e:
        continue
    spec = e[key]
    assert re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", spec["repository"])
    assert re.fullmatch(r"[a-f0-9]{40}", spec["revision"])
    assert spec["files"]
    for f in spec["files"]:
        path = pathlib.PurePosixPath(f["path"])
        assert not path.is_absolute() and ".." not in path.parts
        assert isinstance(f["size"], int) and f["size"] >= 0
        hashes = [k for k in ("sha256", "git_sha1") if k in f]
        assert hashes == ["sha256"] and re.fullmatch(r"[a-f0-9]{64}", f["sha256"]) or \
               hashes == ["git_sha1"] and re.fullmatch(r"[a-f0-9]{40}", f["git_sha1"])
PY
done

# fetch.py accepts files that already verify (git blob and SHA-256) without
# any network access, marks the revision complete, and refuses unsafe paths.
rev=0123456789abcdef0123456789abcdef01234567
mkdir -p "$work/models/o/m/$rev/sub"
printf 'small\n' >"$work/models/o/m/$rev/config.json"
printf 'large\n' >"$work/models/o/m/$rev/sub/w.bin"
python3 -I - "$work" "$rev" "$(git hash-object "$work/models/o/m/$rev/config.json")" \
  "$(shasum -a 256 "$work/models/o/m/$rev/sub/w.bin" | cut -d' ' -f1)" <<'PY'
import json, sys
work, rev, sha1, sha256 = sys.argv[1:]
spec = {"repository": "o/m", "revision": rev, "files": [
    {"path": "config.json", "size": 6, "git_sha1": sha1},
    {"path": "sub/w.bin", "size": 6, "sha256": sha256}]}
json.dump({"model": spec}, open(f"{work}/entry.json", "w"))
spec = dict(spec, files=[{"path": "../escape", "size": 1, "sha256": "0" * 64}])
json.dump({"model": spec}, open(f"{work}/unsafe.json", "w"))
PY
python3 -I "$dir/models/fetch.py" "$work/entry.json" "$work/models" >/dev/null || fail 'verified files were not accepted offline'
[[ -s $work/models/o/m/$rev/VERIFIED.json ]] || fail 'VERIFIED.json missing after a complete fetch'
if python3 -I "$dir/models/fetch.py" "$work/unsafe.json" "$work/models" 2>/dev/null; then fail 'unsafe path accepted'; fi
[[ ! -e $work/models/o/escape ]] || fail 'unsafe path escaped the revision directory'

# Rendered manifests pin images by digest, keep secrets out of the output and
# map every catalog serving setting to a server flag.
entry=$dir/models/qwen3.8-27b-mxfp4.json
python3 -I "$dir/models/render-fetch.py" "$entry" "$lock" >"$work/fetch.json" || fail 'fetch render failed'
python3 -I "$dir/models/render-serve.py" "$entry" registry.lan.thespry.family/local/sglang-gfx1201:abc >"$work/serve.json" ||
  fail 'serve render failed'
if python3 -I "$dir/models/render-serve.py" "$entry" docker.io/library/sglang:latest >/dev/null 2>&1; then
  fail 'non-lab image accepted'
fi
python3 -I - "$entry" "$work/fetch.json" "$work/serve.json" <<'PY' || fail 'rendered manifests'
import json, sys
entry, fetch, serve = (json.load(open(p)) for p in sys.argv[1:])
job = fetch["items"][1]["spec"]["template"]["spec"]
assert "@sha256:" in job["containers"][0]["image"] and job["automountServiceAccountToken"] is False
deploy = serve["items"][0]
pod = deploy["spec"]["template"]["spec"]
args = pod["containers"][0]["args"]
assert deploy["spec"]["strategy"]["type"] == "Recreate"
assert pod["securityContext"]["runAsNonRoot"] and pod["containers"][0]["securityContext"]["readOnlyRootFilesystem"]
for key, value in entry["serving"].items():
    flag = "--" + key.replace("_", "-")
    assert flag in args and args[args.index(flag) + 1] == str(value), flag
assert args[args.index("--api-key") + 1] == "$(SGLANG_API_KEY)" and "--enable-metrics" in args
service = serve["items"][1]
assert service["metadata"]["name"] == "sglang" and service["spec"]["ports"][0]["port"] == 30000
pod_labels = deploy["spec"]["template"]["metadata"]["labels"]
assert service["spec"]["selector"].items() <= pod_labels.items()
assert args[args.index("--model-path") + 1].endswith(entry["model"]["revision"])
assert args[args.index("--speculative-draft-model-path") + 1].endswith(entry["draft"]["revision"])
gpus = pod["containers"][0]["resources"]["limits"]["amd.com/gpu"]
assert gpus == str(entry["serving"]["tp_size"])
PY

# Stage metrics: start/finish states, failure, ccache ratio, and a missing
# directory never failing the build.
metrics=$work/metrics
mkdir "$metrics"
printf 'direct_cache_hit\t6\npreprocessed_cache_hit\t2\ncache_miss\t2\n' >"$work/ccache.tsv"
python3 -I "$dir/stage-metrics.py" "$metrics" rocm start
grep -q '^workstation_ai_stack_stage_running{stage="rocm"} 1$' "$metrics/ai-stack.prom" || fail 'running stage not reported'
python3 -I "$dir/stage-metrics.py" "$metrics" rocm finish --status 0 --ccache-stats "$work/ccache.tsv"
grep -q '^workstation_ai_stack_stage_last_result{stage="rocm"} 1$' "$metrics/ai-stack.prom" || fail 'success not reported'
grep -q '^workstation_ai_stack_ccache_hit_ratio{stage="rocm"} 0.8$' "$metrics/ai-stack.prom" || fail 'ccache ratio wrong'
grep -q '^workstation_ai_stack_stage_running{stage="rocm"} 0$' "$metrics/ai-stack.prom" || fail 'finished stage still running'
python3 -I "$dir/stage-metrics.py" "$metrics" torch start
python3 -I "$dir/stage-metrics.py" "$metrics" torch finish --status 2
grep -q '^workstation_ai_stack_stage_last_result{stage="torch"} 0$' "$metrics/ai-stack.prom" || fail 'failure not reported'
grep -q '^workstation_ai_stack_stage_last_result{stage="rocm"} 1$' "$metrics/ai-stack.prom" || fail 'other stage state lost'
! grep -v -E '^workstation_ai_stack_[a-z_]+(\{stage="(rocm|torch|sglang|image)"\})? [0-9.]+$' "$metrics/ai-stack.prom" | grep -q . ||
  fail 'unexpected textfile line'
python3 -I "$dir/stage-metrics.py" "$work/absent" rocm start 2>/dev/null || fail 'missing metrics directory failed the build'
# The driver's EXIT trap records the real exit status of a failing stage.
bash -c 'here=$1; metrics_dir=$2; stage=sglang
  eval "$(sed -n "/^track() {/,/^}/p" "$here/build-stage.sh")"
  track /dev/null; false' _ "$dir" "$metrics" && fail 'trap fixture should fail'
grep -q '^workstation_ai_stack_stage_last_result{stage="sglang"} 0$' "$metrics/ai-stack.prom" || fail 'trap did not record failure'

# The ROCm build compiles only for the GPU target, including RCCL and tests.
for list in THEROCK_AMDGPU_TARGETS THEROCK_DIST_AMDGPU_TARGETS THEROCK_TEST_AMDGPU_TARGETS; do
  grep -Fq -- "-D$list=\"\$GPU_TARGET\"" "$dir/rocm/build.sh" || fail "ROCm build does not limit $list to the GPU target"
done
# The ROCm build bounds sub-build, Flang, link and background parallelism.
for setting in 'CMAKE_BUILD_PARALLEL_LEVEL=' '-DTHEROCK_BACKGROUND_BUILD_JOBS=1' '-DFLANG_PARALLEL_COMPILE_JOBS=' '-DLLVM_PARALLEL_LINK_JOBS=2'; do
  grep -Fq -- "$setting" "$dir/rocm/build.sh" || fail "ROCm build does not bound parallelism: $setting"
done

# The driver rejects unknown stages before touching the host.
if bash "$dir/build-stage.sh" nonsense 2>/dev/null; then fail 'unknown stage accepted'; fi
for script in "$dir"/*.sh "$dir"/*/build.sh; do bash -n "$script" || fail "syntax: $script"; done
printf 'PASS: AI-stack lock, stage scripts, model catalog and fetcher\n'
