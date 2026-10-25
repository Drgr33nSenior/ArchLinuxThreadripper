#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
command -v timeout >/dev/null 2>&1 || {
  printf 'A coreutils timeout command is required for the disposable smoke test\n' >&2
  exit 2
}
[[ -z ${DOCKER_HOST:-} ]] || {
  printf 'Refuse an overridden Docker target\n' >&2
  exit 2
}
umask 077
mkdir -p "$root/test-results"
evidence=$(mktemp -d "$root/test-results/native-service.XXXXXX")
printf 'phase\texit_status\n' >"$evidence/summary.tsv"
printf 'service\tstate\thealth\texit_status\trestarts\n' >"$evidence/containers.tsv"
SMOKE_DIRECTORY=$(mktemp -d)
export SMOKE_DIRECTORY
project="ws-service-fixture-$$"
compose=(docker compose --parallel 2 -p "$project" -f "$root/tests/service/compose.yaml")
cleanup_needed=0
run_stage() {
  local phase=$1 duration=$2 status
  shift 2
  if timeout --kill-after=10s "$duration" "$@" >"$SMOKE_DIRECTORY/$phase.log" 2>&1; then
    status=0
  else
    status=$?
  fi
  printf '%s\t%s\n' "$phase" "$status" >>"$evidence/summary.tsv"
  if ((status != 0)); then
    printf 'FAIL: native smoke phase %s (exit %s); raw output retained privately\n' "$phase" "$status" >&2
    return "$status"
  fi
}
cleanup() {
  local status=$? service identifier metadata cleanup_status
  trap - EXIT
  set +e
  if ((cleanup_needed)); then
    for service in gateway-postgres sglang gateway; do
      identifier=$(timeout --kill-after=1s 5s "${compose[@]}" ps --all --quiet "$service" 2>/dev/null)
      metadata=
      if [[ $identifier =~ ^[a-f0-9]{12,64}$ ]]; then
        metadata=$(timeout --kill-after=1s 5s docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} {{.State.ExitCode}} {{.RestartCount}}' "$identifier" 2>/dev/null)
      fi
      if [[ $metadata =~ ^(created|running|paused|restarting|removing|exited|dead)\ (none|starting|healthy|unhealthy)\ [0-9]+\ [0-9]+$ ]]; then
        printf '%s\t%s\n' "$service" "${metadata// /$'\t'}" >>"$evidence/containers.tsv"
      else
        printf '%s\tunavailable\tunavailable\t-\t-\n' "$service" >>"$evidence/containers.tsv"
      fi
    done
    timeout --kill-after=5s 35s "${compose[@]}" down --volumes --timeout 10 >"$SMOKE_DIRECTORY/cleanup.log" 2>&1
    cleanup_status=$?
    printf 'cleanup\t%s\n' "$cleanup_status" >>"$evidence/summary.tsv"
    if ((status == 0 && cleanup_status != 0)); then status=$cleanup_status; fi
  fi
  chmod 700 "$SMOKE_DIRECTORY"
  printf 'result\t%s\n' "$status" >>"$evidence/summary.tsv"
  printf 'Sanitized smoke evidence: %s\n' "$evidence"
  printf 'Protected fixture evidence: %s\n' "$SMOKE_DIRECTORY"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
run_stage docker-context 15s docker context inspect --format '{{.Endpoints.docker.Host}}'
read -r docker_endpoint <"$SMOKE_DIRECTORY/docker-context.log"
[[ $docker_endpoint == unix://* ]] || {
  printf 'Local Docker socket required\n' >&2
  exit 2
}
# Only this disposable synthetic fixture is traversable by container UIDs.
# The client runs as the invoking UID, so its key-persistence directory stays 0700.
chmod 711 "$SMOKE_DIRECTORY"
mkdir -m 755 "$SMOKE_DIRECTORY/secrets"
mkdir -m 700 "$SMOKE_DIRECTORY/state"
for name in admin-password litellm-password master-key salt-key control-key; do
  openssl rand -hex 32 >"$SMOKE_DIRECTORY/secrets/$name"
done
sed 's/^/sk-/' "$SMOKE_DIRECTORY/secrets/master-key" >"$SMOKE_DIRECTORY/secrets/master-key.new"
mv "$SMOKE_DIRECTORY/secrets/master-key.new" "$SMOKE_DIRECTORY/secrets/master-key"
{
  printf 'postgresql://litellm:'
  tr -d '\n' <"$SMOKE_DIRECTORY/secrets/litellm-password"
  printf '@gateway-postgres:5432/litellm\n'
} >"$SMOKE_DIRECTORY/secrets/database-url"
printf '{"users":{}}\n' >"$SMOKE_DIRECTORY/secrets/mapping.json"
chmod 444 "$SMOKE_DIRECTORY/secrets/"*
run_stage compose-config 15s "${compose[@]}" config --quiet
run_stage image-pull 420s "${compose[@]}" pull --quiet
cleanup_needed=1
run_stage database-start 150s "${compose[@]}" up --wait --wait-timeout 120 --no-build --pull never gateway-postgres
run_stage migration 120s "${compose[@]}" run --rm --no-deps -T --entrypoint python gateway /runtime/migrate.py
run_stage gateway-start 45s "${compose[@]}" up -d --no-build --pull never sglang gateway
run_stage client-initial 210s "${compose[@]}" run --rm --no-deps -T --user "$(id -u):$(id -g)" client initial
run_stage service-restart 45s "${compose[@]}" restart --timeout 10 gateway-postgres gateway
run_stage client-restart 210s "${compose[@]}" run --rm --no-deps -T --user "$(id -u):$(id -g)" client restart
printf 'PASS: native gateway assertions and restart persistence\n'
