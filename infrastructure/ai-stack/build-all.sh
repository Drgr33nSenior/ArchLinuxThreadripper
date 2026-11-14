#!/usr/bin/env bash
# Build every AI-stack stage for the current stack.lock, in order. Stages whose
# pins are unchanged are reused, so after a refresh only changed stages rebuild.
# Run detached: systemd-run --user --unit=ai-stack-build --collect bash build-all.sh
set -euo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
for stage in rocm torch sglang image comfyui swarmui creative graphiti gaming webui dra; do
  printf '=== %s %s\n' "$stage" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  bash "$here/build-stage.sh" "$stage"
done
printf '=== done %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
