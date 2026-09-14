#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
runtime_fixture=$(mktemp -d)
trap 'rm -rf -- "$runtime_fixture"' EXIT
ln -s "$root" "$runtime_fixture/linked checkout"
(
  cd "$runtime_fixture"
  dirname() {
    printf 'dirname\n' >>"$runtime_fixture/discovery"
    command dirname "$@"
  }
  # Exercise a relative path through a directory symlink, including spaces.
  # shellcheck source=lib/workstation/runtime.sh
  source 'linked checkout/lib/workstation/runtime.sh'
  (($(wc -l <"$runtime_fixture/discovery") == 1)) || {
    printf 'runtime repeated its module-directory discovery\n' >&2
    exit 1
  }
  for action in ws_build_jobs ws_hardware_collect ws_rocm_validate ws_package_snapshot \
    ws_resources_plan ws_session_switch ws_sunshine_args ws_rag_corpus \
    ws_agent_config_validate ws_measure_command ws_telemetry; do
    declare -F "$action" >/dev/null
  done
  [[ $(ws_repo_root) == "$root" ]]
)
printf 'Runtime module loading and canonical source directory passed\n'
