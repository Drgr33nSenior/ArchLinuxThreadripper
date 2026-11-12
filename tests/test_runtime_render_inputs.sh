#!/usr/bin/env bash
# The installed runtime runs `workstationctl mode`, whose service render reads these
# repository files (recorded with a Python audit hook during a full shared render, plus
# everything kustomize can reach under apps/). Each must ship in the runtime package.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$root"
list=infrastructure/packages/bootstrap/bridge-runtime.files
missing=$(
  {
    git ls-files apps
    git ls-files 'lib/workstation/*.py'
    git ls-files 'infrastructure/ai-stack/serving/*.py' 'infrastructure/ai-stack/serving/*.json'
    printf '%s\n' infrastructure/ai-stack/k8s/models.yaml infrastructure/ai-stack/models/cache.py \
      infrastructure/ai-stack/models/fetch.py infrastructure/ai-stack/models/qwen3.8-27b-mxfp4.json \
      infrastructure/ai-stack/stack.lock infrastructure/service/traefik.yaml
  } | sort -u | comm -23 - <(sort "$list")
)
[[ -z $missing ]] || {
  printf 'FAIL: service-render inputs missing from %s:\n%s\n' "$list" "$missing" >&2
  exit 1
}
printf 'PASS: every service-render input ships in the runtime package\n'
