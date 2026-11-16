# shellcheck shell=bash
# Shared helpers for the owner tools under tools/ (tools/README.md). Sourced, never run.
#
# Site values (addresses, SSH destinations, local paths) never live in the repository. A tool
# reads them from the environment first, then from the site file
#   ${WORKSTATION_SITE_ENV:-${XDG_CONFIG_HOME:-$HOME/.config}/workstation/site.env}
# which holds strict KEY=VALUE lines (lib/common.sh common::load_config: never evaluated as
# shell, unknown or repeated keys refused). A missing value stops the tool with its name.

tools_lib_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
tools_repo_root=$(cd -- "$tools_lib_dir/../.." && pwd -P)
# shellcheck source=lib/common.sh
source "$tools_repo_root/lib/common.sh"

# Every key the site file may hold (tools/README.md, "Site values").
TOOLS_SITE_KEYS=(
  WORKSTATION_SRC_DIR WORKSTATION_SSH WORKSTATION_LAN_ADDRESS
  NAS_ADDRESS NAS_SSH NAS_SANDBOX_DATASET NAS_REPO NAS_COMMISSIONING_DIR NAS_OWNER_PROFILE NAS_ANSIBLE_VENV
  PROMETHEUS_URL GATEWAY_KEY_ALIASES CHAT_PROBE
)
tools_site_file=${WORKSTATION_SITE_ENV:-${XDG_CONFIG_HOME:-$HOME/.config}/workstation/site.env}

# tools::usage SCRIPT: print the script's header comment (the lines after the shebang).
tools::usage() {
  awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$1"
}

# tools::help_requested ARGS...: true when the first argument asks for the usage.
tools::help_requested() {
  [[ ${1:-} == -h || ${1:-} == --help ]]
}

# tools::load_site: fill unset site values from the site file. The environment wins.
tools::load_site() {
  local key saved=() pair
  for key in "${TOOLS_SITE_KEYS[@]}"; do
    [[ -z ${!key:-} ]] || saved+=("$key=${!key}")
  done
  if [[ -f $tools_site_file ]]; then
    common::load_config "$tools_site_file" "${TOOLS_SITE_KEYS[@]}"
  fi
  for pair in ${saved[@]+"${saved[@]}"}; do
    printf -v "${pair%%=*}" '%s' "${pair#*=}"
  done
}

# tools::require_site KEY...: stop unless every named site value is set.
tools::require_site() {
  local key
  for key in "$@"; do
    [[ -n ${!key:-} ]] || {
      printf 'STOP: set %s in the environment or in %s (tools/README.md, "Site values")\n' "$key" "$tools_site_file" >&2
      exit 1
    }
  done
}

# tools::source_tree SHA12: the shipped source tree for that commit, under WORKSTATION_SRC_DIR.
tools::source_tree() {
  [[ $1 =~ ^[0-9a-f]{7,40}$ ]] || {
    printf 'STOP: %s is not a commit id (7-40 lowercase hex digits)\n' "$1" >&2
    exit 1
  }
  printf '%s/archlinuxthreadripper-%s\n' "${WORKSTATION_SRC_DIR:-$HOME/src}" "$1"
}
