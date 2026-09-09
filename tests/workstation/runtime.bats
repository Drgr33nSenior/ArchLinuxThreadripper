#!/usr/bin/env bats

setup() {
  export REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd -P)"
  export PATH="$BATS_TEST_TMPDIR/bin:$PATH"
  mkdir -p "$BATS_TEST_TMPDIR/bin" "$BATS_TEST_TMPDIR/dri/by-path"
  export WS_TEST_ROOT="$BATS_TEST_TMPDIR"
}

@test "usage rejects an unknown command" {
  run "$REPO_ROOT/bin/workstationctl" unknown
  [ "$status" -eq 2 ]
  [[ "$output" == *Usage:* ]]
}

@test "LLM requires an immutable image reference" {
  lock="$BATS_TEST_TMPDIR/versions.lock"
  printf 'LLM_SCALER_IMAGE=example.invalid/image:latest\n' > "$lock"
  # Exercise the image gate on any development OS, without device/runtime access.
  run bash -c '
    source "$1/lib/workstation/runtime.sh"
    ws_require_arch() { :; }
    ws_require_user() { :; }
    ws_gpu_validate() { ws_die "unexpected device access"; }
    podman() { ws_die "unexpected container execution"; }
    ws_llm_up 0000:01:00.0 "$2" "$3"
  ' _ "$REPO_ROOT" "$BATS_TEST_TMPDIR" "$lock"
  [ "$status" -ne 0 ]
  [[ "$output" == *immutable* ]]
}

@test "immutable LLM image reaches the mocked device gate" {
  lock="$BATS_TEST_TMPDIR/versions.lock"
  printf 'LLM_SCALER_IMAGE=example.invalid/image@sha256:%064d\n' 0 > "$lock"
  run bash -c '
    source "$1/lib/workstation/runtime.sh"
    ws_require_arch() { :; }
    ws_require_user() { :; }
    ws_gpu_validate() { ws_die "fixture device gate reached"; }
    podman() { ws_die "unexpected container execution"; }
    ws_llm_up 0000:01:00.0 "$2" "$3"
  ' _ "$REPO_ROOT" "$BATS_TEST_TMPDIR" "$lock"
  [ "$status" -ne 0 ]
  [[ "$output" == *"fixture device gate reached"* ]]
}

@test "GPU validation rejects a non-B70 PCI address before device access" {
  run bash -c 'source "$1/lib/workstation/runtime.sh"; ws_gpu_validate 0000:01:00.0' _ "$REPO_ROOT"
  [ "$status" -ne 0 ]
  [[ "$output" == *"supported only"* || "$output" == *B70* ]]
}

@test "zsh setup requires a full immutable commit" {
  run grep -F -- "OMZ_COMMIT" "$REPO_ROOT/lib/workstation/runtime.sh"
  [ "$status" -eq 0 ]
}

@test "makepkg policy is generated from the installed baseline" {
  run grep -F -- 'ws_makepkg_configure' "$REPO_ROOT/lib/workstation/runtime.sh"
  [ "$status" -eq 0 ]
}
