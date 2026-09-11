#!/usr/bin/env bash
# Explicit local image checks. Never pulls, deploys, accesses a journal or GPU,
# publishes a port, or uses a live kubeconfig. Retains owned test evidence.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
context=${1:?usage: validate-images.sh REVIEWED_LOCAL_DOCKER_CONTEXT}
for tool in docker ruby jq; do
  command -v "$tool" >/dev/null || {
    printf 'BLOCKED: image validation requires %s\n' "$tool" >&2
    exit 1
  }
done
endpoint=$(docker context inspect "$context" --format '{{.Endpoints.docker.Host}}')
[[ $endpoint == unix://* ]] || {
  printf 'BLOCKED: this validator requires a reviewed local Unix-socket Docker context\n' >&2
  exit 1
}
umask 077
mkdir -p "$root/test-results"
output=$(mktemp -d "$root/test-results/telemetry-images.XXXXXX")
name="arch-workstation-telemetry-check-${output##*.}"
running=0
cleanup() {
  local result=$?
  trap - EXIT
  if ((running)); then
    docker --context "$context" stop --timeout 15 "$name" >"$output/stop.log" 2>&1 || {
      printf 'WARNING: owned fixture container cleanup failed: %s\n' "$name" >&2
      ((result != 0)) || result=1
    }
  fi
  printf 'Telemetry image validation evidence: %s\n' "$output"
  exit "$result"
}
trap cleanup EXIT
image() {
  if [[ $1 == host-alloy ]]; then
    jq -er '.host_provider.validation_image' "$root/infrastructure/observability/versions.json"
  else
    jq -er --arg component "$1" '.images[] | select(.component == $component) | .image' "$root/infrastructure/observability/versions.json"
  fi
}
common=(--rm --pull never --network none --read-only --cap-drop ALL
  --security-opt no-new-privileges --memory 512m --cpus 1 --pids-limit 128)
config=(--mount "type=bind,source=$root/infrastructure/observability/config,target=/config,readonly")
for component in prometheus alloy host-alloy loki tempo; do
  docker --context "$context" image inspect "$(image "$component")" --format '{{.Id}} {{.Os}}/{{.Architecture}}' >>"$output/images.txt"
done
docker --context "$context" run "${common[@]}" --user 65534:65534 "${config[@]}" --entrypoint /bin/promtool "$(image prometheus)" check config --syntax-only /config/prometheus.yaml >"$output/prometheus.log" 2>&1
docker --context "$context" run "${common[@]}" --user 65534:65534 "${config[@]}" --entrypoint /bin/promtool "$(image prometheus)" check rules /config/alerts.yaml >>"$output/prometheus.log" 2>&1
docker --context "$context" run "${common[@]}" --user 10001:10001 "${config[@]}" "$(image loki)" -config.file=/config/loki.yaml -verify-config=true >"$output/loki.log" 2>&1
docker --context "$context" run "${common[@]}" --user 10001:10001 "${config[@]}" "$(image tempo)" -config.file=/config/tempo.yaml -config.verify=true >"$output/tempo.log" 2>&1
docker --context "$context" run "${common[@]}" --user 473:473 "${config[@]}" "$(image alloy)" validate /config >"$output/alloy.log" 2>&1
docker --context "$context" run "${common[@]}" --user 473:473 --mount "type=bind,source=$root/infrastructure/observability/host,target=/config,readonly" "$(image host-alloy)" validate /config >>"$output/alloy.log" 2>&1

# Execute the actual canonical Loki processors with synthetic files instead of
# the host journal. Echo is a fixture-only sink; production never enables it.
mkdir -m 755 "$output/fixture"
ruby -rjson - "$root/infrastructure/observability/host/config.alloy" "$output/fixture" <<'RUBY'
source = File.read(ARGV[0])
directory = ARGV[1]
processors = %w[installation kernel].map do |name|
  block = source[/^loki\.process "#{name}" \{.*?^\}/m] || raise("missing canonical processor")
  block.sub('loki.write.local.receiver', 'loki.echo.fixture.receiver')
end.join("\n")
inputs = %w[installation kernel].map do |name|
  %(loki.source.file "#{name}" {\n  targets = [{"__path__" = "/fixture/#{name}.log", "job" = "fixture-input"}]\n  forward_to = [loki.process.#{name}.receiver]\n}\n)
end.join("\n")
File.write("#{directory}/check.alloy", %(logging { level = "info" }\n#{inputs}\n#{processors}\nloki.echo "fixture" {}\n))
event = {'schema'=>1,'run_id'=>'20260911T120000Z','configuration_sha256'=>'a'*64,'stage'=>'preflight','mode'=>'dry-run','outcome'=>'succeeded','exit_code'=>0,'timestamp'=>'2026-09-11T12:00:00Z'}
sentinel = 'DUMMY_TELEMETRY_SECRET_SENTINEL'
File.write("#{directory}/installation.log", [
  JSON.generate(event),
  JSON.generate(event.merge('password'=>sentinel)),
  JSON.generate(event.merge('stage'=>sentinel)),
  JSON.generate(event.merge('stage'=>'dummy-telemetry-secret-sentinel')),
  "raw console #{sentinel}",
  JSON.generate(event.merge('schema'=>2)),
].join("\n")+"\n")
File.write("#{directory}/kernel.log", "amdgpu: error private_parameter=#{sentinel}\nXFS: warning private_path=#{sentinel}\nunrelated #{sentinel}\n")
RUBY
# Only the generated dummy inputs are readable by the container UID on Linux;
# retained result logs stay private in the 0700 parent directory.
chmod 644 "$output/fixture/check.alloy" "$output/fixture/installation.log" "$output/fixture/kernel.log"
docker --context "$context" run -d "${common[@]}" --name "$name" --label io.arch-workstation.scope=dev --label io.arch-workstation.purpose=telemetry-configcheck --user 473:473 --tmpfs /tmp:rw,noexec,nosuid,nodev,size=32m --mount "type=bind,source=$output/fixture,target=/fixture,readonly" "$(image host-alloy)" run --disable-reporting --storage.path=/tmp/state --server.http.enable-pprof=false /fixture/check.alloy >"$output/container-id"
running=1
for _ in {1..20}; do
  docker --context "$context" logs "$name" >"$output/processor.log" 2>&1
  if grep -q 'inspect the local journal' "$output/processor.log" && grep -q 'configuration_sha256' "$output/processor.log"; then break; fi
  sleep 1
done
ruby - "$output/processor.log" <<'RUBY'
text = File.read(ARGV[0])
raise 'dummy secret escaped a canonical processor' if text.include?('DUMMY_TELEMETRY_SECRET_SENTINEL')
raise 'lowercase dummy secret escaped a canonical processor' if text.include?('dummy-telemetry-secret-sentinel')
raise 'valid installation event missing or duplicated' unless text.scan('configuration_sha256').length == 1
raise 'kernel summaries missing' unless text.include?('amdgpu warning/error; inspect the local journal') && text.include?('XFS warning/error; inspect the local journal')
raise 'raw kernel message escaped' if text.include?('private_parameter') || text.include?('private_path')
puts 'Pinned image parsers and actual Alloy processor secret-sentinel fixture passed.'
RUBY
