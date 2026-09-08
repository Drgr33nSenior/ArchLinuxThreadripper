#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
source "$root/lib/common.sh"
source "$root/lib/k3s/common.sh"

# New model lock keys must not break the independent legacy K3s parser.
for key in SGLANG_DUAL_MODEL_REPOSITORY SGLANG_DUAL_MODEL_REVISION SGLANG_SINGLE_MODEL_REPOSITORY SGLANG_SINGLE_MODEL_REVISION; do
  k3s_allowed_key "$key"
  k3s_validate_value "$key" "$(common::lock_get "$root/versions.lock" "$key")"
  if (k3s_validate_value "$key" unreviewed) >/dev/null 2>&1; then exit 1; fi
done
if k3s_allowed_key SGLANG_UNREVIEWED_MODEL; then exit 1; fi
if (k3s_validate_value SGLANG_DUAL_MODEL_REPOSITORY Qwen/Qwen3.8-27B) >/dev/null 2>&1; then exit 1; fi
if (k3s_validate_value SGLANG_SINGLE_MODEL_REPOSITORY Qwen/Qwen2.5-Coder-32B-Instruct-AWQ) >/dev/null 2>&1; then exit 1; fi
printf 'Model default lock validation passed; serving remains target-qualified separately\n'
