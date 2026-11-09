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
grep -Fq 'install -y --no-install-recommends libgomp1 libatomic1 gcc g++ libc6-dev' "$dir/image/Containerfile" ||
  fail 'runtime image lacks the GCC runtime libraries or the C/C++ compilers Triton and SGLang JIT need'
grep -Fq 'HIP_DEVICE_LIB_PATH=/usr/local/lib/python3.14/site-packages/_rocm_sdk_devel/amdgcn/bitcode' "$dir/image/Containerfile" ||
  fail 'runtime image does not point hipcc at the device bitcode (SGLang JIT kernels fail to build)'
grep -Fq 'HIP C++ JIT compile failed' "$dir/image/smoke.py" || fail 'image smoke test does not compile a HIP C++ JIT kernel'
# shellcheck disable=SC2016 # literal text in build-stage.sh
grep -Fq -- '-v "$here/image/smoke.py:/smoke.py:ro"' "$dir/build-stage.sh" ||
  fail 'image stage does not run the GPU smoke test before pushing'
grep -Fq -- '--read-only --tmpfs /tmp --device /dev/kfd --device /dev/dri' "$dir/build-stage.sh" ||
  fail 'image smoke test does not run with GPUs and a read-only root'
grep -Fq 'RUN rocm-sdk init' "$dir/image/Containerfile" || fail 'devel SDK is left to unpack at runtime'
grep -Fq 'ENV ROCM_HOME=/usr/local/lib/python3.14/site-packages/_rocm_sdk_devel' "$dir/image/Containerfile" ||
  fail 'image does not point ROCM_HOME at the packaged SDK'
abi=$(awk -F= '$1 == "PYTHON_ABI" {print $2}' "$lock")
[[ $abi == cp314-cp314 ]] || fail "Containerfile paths assume python3.14; update them for $abi"
grep -Fq '_use_gfx1201_mxfp4' "$dir/image/smoke.py" || fail 'image smoke test does not check the gfx1201 MXFP4 route'
python3 -I -m py_compile "$dir/image/smoke.py" || fail 'image smoke test does not compile'
grep -Eq '^GPU_TARGET=gfx[0-9a-f]+$' "$lock" || fail 'GPU target'
grep -Eq '^PYTHON_ABI=cp[0-9]+-cp[0-9]+t?$' "$lock" || fail 'Python ABI'

# Catalog entries pin exact revisions and a published hash for every file.
for entry in "$dir"/models/*.json; do
  python3 -I - "$entry" <<'PY' || fail "catalog entry: $entry"
import json, pathlib, re, sys
e = json.loads(pathlib.Path(sys.argv[1]).read_text())
assert e["schema"] == 1 and re.fullmatch(r"[a-z0-9][a-z0-9.-]+", e["alias"])
# Serving settings belong to serving/*.json profiles, not the catalog.
assert "serving" not in e and "served_model_name" not in e
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

# The fetch Job pins its image by digest and mounts no service-account token.
entry=$dir/models/qwen3.8-27b-mxfp4.json
python3 -I "$dir/models/render-fetch.py" "$entry" "$lock" >"$work/fetch.json" || fail 'fetch render failed'
python3 -I - "$work/fetch.json" <<'PY' || fail 'fetch manifest'
import json, sys
job = json.load(open(sys.argv[1]))["items"][1]["spec"]["template"]["spec"]
assert "@sha256:" in job["containers"][0]["image"] and job["automountServiceAccountToken"] is False
PY

# Serving profiles: every setting records its reason, source and confidence;
# arms render the qualified flags, and the qualification Job is restricted-PSA,
# digest-pinned, read-only and on the verified ai-models volume.
render=$dir/serving/render.py
digest=registry.lan.thespry.family/local/sglang-gfx1201@sha256:$(printf '0%.0s' {1..64})
python3 -I -m py_compile "$render" "$dir/serving/qualify.py" || fail 'serving scripts do not compile'
for profile in "$dir"/serving/*.json; do
  for arm in A B; do
    python3 -I "$render" argv "$profile" --arm "$arm" >"$work/argv-$arm.json" || fail "argv render: $profile $arm"
    python3 -I "$render" job "$profile" "$digest" --arm "$arm" >"$work/job-$arm.json" || fail "job render: $profile $arm"
  done
  if python3 -I "$render" job "$profile" registry.lan.thespry.family/local/sglang-gfx1201:tag >/dev/null 2>&1; then
    fail 'qualification Job accepted an image without a digest'
  fi
  python3 -I - "$profile" "$work" "$dir" <<'PY' || fail "serving profile: $profile"
import json, pathlib, sys
profile, work, root = json.load(open(sys.argv[1])), pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])
assert profile["schema"] == 1 and profile["status"] in ("unqualified", "qualified")
assert (root / "models" / f"{profile['model']}.json").exists()
assert (root / "serving" / profile["research"]).exists()
for e in profile["args"]:
    assert e["flag"].startswith("--") and isinstance(e["value"], list), e
    assert e["reason"] and e["source"] and e["confidence"] in ("H", "M", "L") and isinstance(e["measure"], bool), e
for e in profile["env"]:
    assert e["reason"] and e["source"] and e["confidence"] in ("H", "M", "L"), e
assert sum(1 for arm in profile["arms"].values() if arm["default"]) == 1
flags = [e["flag"] for e in profile["args"]]
assert len(flags) == len(set(flags)), "duplicate flag"
unset = {e["name"] for e in profile["must_unset_env"]}
forbidden = {e["flag"] for e in profile["not_set"]}
argv = {arm: json.loads((work / f"argv-{arm}.json").read_text()) for arm in "AB"}
for arm, a in argv.items():
    assert not forbidden & set(a), (arm, forbidden & set(a))
    assert a[a.index("--attention-backend") + 1] == "triton" and "--disable-custom-all-reduce" in a
assert argv["A"][argv["A"].index("--mamba-radix-cache-strategy") + 1] == "no_buffer"
assert "--disable-overlap-schedule" in argv["A"]
assert argv["B"][argv["B"].index("--mamba-radix-cache-strategy") + 1] == "extra_buffer_lazy"
assert "--disable-overlap-schedule" not in argv["B"]
for arm in "AB":
    items = json.loads((work / f"job-{arm}.json").read_text())["items"]
    config, job = items
    pod = job["spec"]["template"]["spec"]
    c = pod["containers"][0]
    assert config["data"]["qualify.py"] == (root / "serving" / "qualify.py").read_text()
    assert c["args"] == argv[arm] and "@sha256:" in c["image"]
    assert pod["securityContext"]["runAsNonRoot"] and pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    assert pod["automountServiceAccountToken"] is False
    assert c["securityContext"] == {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]}}
    assert c["resources"]["requests"] == c["resources"]["limits"] and c["resources"]["limits"]["amd.com/gpu"] == "2"
    models = next(v for v in pod["volumes"] if v["name"] == "models")
    assert models["persistentVolumeClaim"] == {"claimName": "ai-models", "readOnly": True}
    names = {e["name"] for e in c["env"]}
    assert not unset & names, unset & names
    assert job["spec"]["backoffLimit"] == 0 and job["metadata"]["namespace"] == "ai-home-lab"
PY
done

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

# SGLang versions are valid PEP 440 for every shape upstream's describe prints.
eval "$(sed -n '/^sglang_version() {/,/^}/p' "$dir/sglang/build.sh")"
c=fcb3274646e43c16d70fd83806ebb3a5134d5e81
p=a6f33c893a34c8b4b9cbf2e3b70bb351cb24ec96b17ec7960312d8a31035a77d
[[ $(sglang_version v0.5.23-957-gfcb3274646 $c $p) == 0.5.23.post957+gfcb32746.pa6f33c89 ]] || fail 'sglang version from main'
[[ $(sglang_version v0.5.23 $c $p) == 0.5.23.post0+gfcb32746.pa6f33c89 ]] || fail 'sglang version on a release'
[[ $(sglang_version v0.5.6.post2-14260-gfcb3274646 $c $p) == 0.5.6.post2+n14260.gfcb32746.pa6f33c89 ]] ||
  fail 'sglang version after a post-release'
[[ $(sglang_version v0.5.24rc0-3-gfcb3274646 $c $p) == 0.5.24rc0.post3+gfcb32746.pa6f33c89 ]] || fail 'sglang version after an rc'
if sglang_version 'not-a-tag' $c $p >/dev/null; then fail 'malformed describe accepted'; fi
if python3 -c 'import packaging' 2>/dev/null; then
  for v in 0.5.23.post957+gfcb32746.pa6f33c89 0.5.6.post2+n14260.gfcb32746.pa6f33c89 0.5.24rc0.post3+gfcb32746.pa6f33c89; do
    python3 -c 'import sys; from packaging.version import Version; Version(sys.argv[1])' "$v" || fail "invalid version $v"
  done
fi

# Both build stages use the device extra the ROCm package declares,
# preferring the target-specific name, and skip it when there is none.
# shellcheck disable=SC2016 # awk program, expanded by awk.
pick='$1 == "Provides-Extra:" && ($2 == t || $2 == "device") {print $2}'
for stage in sglang torch; do
  grep -Fq -- "$pick" "$dir/$stage/build.sh" || fail "$stage stage does not detect the ROCm device extra"
done
pick_from() { printf '%s\n' "$@" | awk -v t=device-gfx1201 "$pick" | sort -r | head -n 1; }
[[ $(pick_from 'Provides-Extra: libraries' 'Provides-Extra: device') == device ]] || fail 'single-target device extra'
[[ $(pick_from 'Provides-Extra: device' 'Provides-Extra: device-gfx1201') == device-gfx1201 ]] ||
  fail 'target-specific device extra'
[[ -z $(pick_from 'Provides-Extra: libraries' 'Provides-Extra: device-gfx942') ]] || fail 'another target accepted'

# Pushed images are scanned before push, then signed, attested and verified
# offline (no transparency log) before the stage records image.digest.
for image in COSIGN_IMAGE TRIVY_IMAGE; do
  grep -Eq "^$image=ghcr\.io/[a-z0-9./_-]+:[A-Za-z0-9._-]+@sha256:[a-f0-9]{64}$" "$lock" || fail "$image is not pinned by digest"
done
python3 -I - "$dir/signing/signing-config.json" <<'PY' || fail 'signing config would upload to a transparency log or service'
import json, sys
c = json.load(open(sys.argv[1]))
assert c["mediaType"] == "application/vnd.dev.sigstore.signingconfig.v0.2+json"
for key in ("caUrls", "oidcUrls", "rekorTlogUrls", "tsaUrls"):
    assert not c.get(key), key
PY
stage=$dir/build-stage.sh
order() { grep -n -F -- "$1" "$stage" | head -n 1 | cut -d: -f1; }
# shellcheck disable=SC2016 # literal text in build-stage.sh
for step in 'trivy --format json' 'scan-policy.py' 'podman push --digestfile "$scan/pushed.digest"' \
  'cosign sign --key' 'cosign attest --key' 'signing/verify-image.sh' 'mv -- "$scan/pushed.digest"'; do
  [[ -n $(order "$step") ]] || fail "image stage lacks: $step"
done
(($(order 'scan-policy.py') < $(order 'podman push --digestfile'))) || fail 'image is pushed before the scan policy'
# shellcheck disable=SC2016 # literal text in build-stage.sh
(($(order 'cosign sign --key') < $(order 'mv -- "$scan/pushed.digest"'))) || fail 'image.digest is recorded before signing'
# shellcheck disable=SC2016 # literal text in build-stage.sh
(($(order 'signing/verify-image.sh') < $(order 'mv -- "$scan/pushed.digest"'))) || fail 'image.digest is recorded before verification'
[[ $(grep -c -- '--signing-config /signing/signing-config.json' "$stage") == 3 ]] || fail 'a cosign signing step lacks the local signing config'
! grep -q -- '--tlog-upload' "$stage" || fail 'cosign v3 has no --tlog-upload; use the signing config'
grep -Fq 'systemd-creds --user decrypt --name=cosign_password' "$stage" || fail 'signing passphrase is not read from the sealed credential'
grep -Fq "systemd-creds --user encrypt --name=cosign_password -" "$dir/signing/init-key.sh" || fail 'init-key.sh does not seal the passphrase via stdin'
pub=$dir/signing/cosign.pub
[[ $(head -n 1 "$pub") == "-----BEGIN PUBLIC KEY-----" && $(tail -n 1 "$pub") == "-----END PUBLIC KEY-----" ]] ||
  fail 'signing/cosign.pub is not a PEM public key'
! grep -q 'PRIVATE' "$pub" || fail 'signing/cosign.pub contains private key material'
grep -Fq -- '--insecure-ignore-tlog=true' "$dir/signing/verify-image.sh" || fail 'verify-image.sh would require a transparency log'
for type in cyclonedx vuln; do grep -Fq "$type" "$dir/signing/verify-image.sh" || fail "verify-image.sh skips the $type attestation"; done
# The scan policy blocks only fixable CRITICAL findings that are not allowlisted
# and unexpired; every finding is still counted.
policy=$dir/image/scan-policy.py
cat >"$work/trivy.json" <<'JSON'
{"Results": [{"Target": "t", "Vulnerabilities": [
  {"VulnerabilityID": "CVE-1", "PkgName": "a", "Severity": "CRITICAL", "FixedVersion": "2"},
  {"VulnerabilityID": "CVE-2", "PkgName": "b", "Severity": "CRITICAL"},
  {"VulnerabilityID": "CVE-3", "PkgName": "c", "Severity": "HIGH", "FixedVersion": "2"}]}]}
JSON
printf '[]\n' >"$work/allow-none.json"
if python3 -I "$policy" "$work/trivy.json" "$work/allow-none.json" 2026-11-09 >"$work/p1.json"; then
  fail 'a fixable CRITICAL finding did not block'
fi
python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); assert [b["id"] for b in d["blocking"]] == ["CVE-1"] and d["counts"] == {"CRITICAL": 2, "HIGH": 1}' "$work/p1.json" ||
  fail 'policy blocked the wrong findings or lost counts'
printf '[{"id": "CVE-1", "package": "a", "reason": "r", "reviewed": "2026-11-09", "expires": "2026-12-01"}]\n' >"$work/allow.json"
python3 -I "$policy" "$work/trivy.json" "$work/allow.json" 2026-11-09 >/dev/null || fail 'an allowlisted finding still blocked'
if python3 -I "$policy" "$work/trivy.json" "$work/allow.json" 2026-12-02 >/dev/null; then fail 'an expired allowlist entry still applied'; fi
printf '[{"id": "CVE-1"}]\n' >"$work/allow-bad.json"
if python3 -I "$policy" "$work/trivy.json" "$work/allow-bad.json" 2026-11-09 >/dev/null 2>&1; then
  fail 'an allowlist entry without reason, review and expiry was accepted'
fi
python3 -I -c 'import json,sys; assert json.load(open(sys.argv[1])) == []' "$dir/image/scan-allowlist.json" ||
  fail 'the committed allowlist must be reviewed; start empty'

# The driver rejects unknown stages before touching the host.
if bash "$dir/build-stage.sh" nonsense 2>/dev/null; then fail 'unknown stage accepted'; fi
for script in "$dir"/*.sh "$dir"/*/build.sh; do bash -n "$script" || fail "syntax: $script"; done
printf 'PASS: AI-stack lock, stage scripts, model catalog and fetcher\n'
