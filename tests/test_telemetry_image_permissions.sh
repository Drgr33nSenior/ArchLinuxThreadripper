#!/usr/bin/env bash
# Run the actual generators with intercepted Docker launches. No daemon is used;
# permission/argument assertions are not container runtime qualification.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
for tool in ruby jq; do
  command -v "$tool" >/dev/null || { printf 'BLOCKED: fixture permission test requires %s\n' "$tool" >&2; exit 1; }
done
umask 077
scratch=$(mktemp -d)
scratch=$(cd -- "$scratch" && pwd -P)
trap 'rm -rf -- "$scratch"' EXIT
export TELEMETRY_FIXTURE_ROOT="$scratch/project"
mkdir -p "$TELEMETRY_FIXTURE_ROOT/infrastructure/observability" "$TELEMETRY_FIXTURE_ROOT/tests/fixtures"
cp "$root/infrastructure/observability/validate-images.sh" "$TELEMETRY_FIXTURE_ROOT/infrastructure/observability/"
cp "$root/infrastructure/observability/versions.json" "$TELEMETRY_FIXTURE_ROOT/infrastructure/observability/"
cp -R "$root/infrastructure/observability/config" "$root/infrastructure/observability/host" "$TELEMETRY_FIXTURE_ROOT/infrastructure/observability/"
cp -R "$root/tests/fixtures/telemetry" "$TELEMETRY_FIXTURE_ROOT/tests/fixtures/"

# Invoked by the child validator at each launch, after shell log redirection.
# shellcheck disable=SC2329
docker() {
  if [[ $* == 'context inspect fixture-local --format {{.Endpoints.docker.Host}}' ]]; then
    printf 'unix:///synthetic/docker.sock\n'
    return
  fi
  [[ $1 == --context && $2 == fixture-local ]] || return 1
  shift 2
  case $1 in
    image)
      [[ $2 == inspect ]] || return 1
      printf 'sha256:synthetic linux/fixture\n'
      ;;
    run)
      ruby - "$TELEMETRY_FIXTURE_ROOT" "$@" <<'RUBY'
root = ARGV.shift
args = ARGV
outputs = Dir.glob("#{root}/test-results/telemetry-images.*")
raise 'ambiguous evidence directory' unless outputs.length == 1
output = outputs.first
def mode(path, expected)
  st = File.lstat(path)
  raise "symlink fixture: #{path}" if st.symlink?
  actual = st.mode & 07777
  raise format('%s mode %04o, expected %04o', path, actual, expected) unless actual == expected
  raise "fixture owner changed: #{path}" unless st.uid == Process.uid
end
mode(output, 0700)
public_files = %w[rules/alerts.yaml rules/alerts_test.yaml fixture/check.alloy fixture/installation.log fixture/kernel.log otlp/check.alloy otlp/traces.json cluster-full/config.alloy cluster-metrics/config.alloy host-full/config.alloy host-metrics/config.alloy]
Dir.glob("#{output}/**/*").each do |path|
  next unless File.file?(path)
  relative = path.delete_prefix("#{output}/")
  # Pending fixture inputs may still be private; require 0644 for the specific
  # consumer below. All result logs/IDs/inspection evidence must remain 0600.
  mode(path, 0600) unless public_files.include?(relative)
end
value = ->(flag) { index = args.index(flag); index && args[index + 1] }
%w[--rm --read-only].each { |flag| raise "missing #{flag}" unless args.include?(flag) }
{'--pull'=>'never', '--cap-drop'=>'ALL', '--security-opt'=>'no-new-privileges', '--memory'=>'512m', '--cpus'=>'1', '--pids-limit'=>'128'}.each do |flag, expected|
  raise "changed containment #{flag}" unless value.call(flag) == expected
end
raise 'privileged fixture' if args.include?('--privileged') || args.include?('--cap-add')
mounts = args.each_index.map { |i| args[i + 1] if args[i] == '--mount' }.compact
raise 'writable fixture mount' unless mounts.all? { |mount| mount.end_with?(',readonly') }
raise 'private evidence parent mounted' if mounts.any? { |mount| mount.include?("source=#{output},") }
network = value.call('--network')
raise 'unexpected network' unless network == 'none' || network == "arch-workstation-telemetry-check-#{output.split('.').last}-otlp"
kind, directory, files, user = if args.include?('/rules/alerts_test.yaml')
  ['rules', 'rules', %w[alerts.yaml alerts_test.yaml], '65534:65534']
elsif args.any? { |arg| arg == '--post-file=/fixture/traces.json' }
  ['otlp-client', 'otlp', %w[traces.json], '65534:65534']
elsif args.include?('/fixture/check.alloy')
  network == 'none' ? ['host', 'fixture', %w[check.alloy installation.log kernel.log], '473:473'] : ['otlp-server', 'otlp', %w[check.alloy], '473:473']
else
  raise 'unexpected container user' unless %w[65534:65534 10001:10001 473:473].include?(value.call('--user'))
  if args.include?('validate') && args.include?('/config')
    mount = mounts.fetch(0)
    profile = %w[cluster-full cluster-metrics host-full host-metrics].find { |name| mount == "type=bind,source=#{output}/#{name},target=/config,readonly" }
    raise 'unexpected Alloy profile fixture mount' unless profile && mounts.length == 1
    mode("#{output}/#{profile}", 0755)
    mode("#{output}/#{profile}/config.alloy", 0644)
    File.open("#{root}/launches", 'a') { |file| file.puts(profile) }
  end
  exit
end
raise "wrong #{kind} reader" unless value.call('--user') == user
target = kind == 'rules' ? '/rules' : '/fixture'
raise "wrong #{kind} mount" unless mounts == ["type=bind,source=#{output}/#{directory},target=#{target},readonly"]
mode("#{output}/#{directory}", 0755)
files.each { |file| mode("#{output}/#{directory}/#{file}", 0644) }
File.open("#{root}/launches", 'a') { |file| file.puts(kind) }
RUBY
      ;;
    logs)
      if [[ $2 == *-otlp ]]; then
        printf 'fixture-scope-sanitized\n'
      else
        printf 'configuration_sha256\namdgpu warning/error; inspect the local journal\nXFS warning/error; inspect the local journal\n'
      fi
      ;;
    network)
      case $2 in
        create) [[ $3 == --internal ]] || return 1 ;;
        rm) [[ $3 == arch-workstation-telemetry-check-*-otlp ]] || return 1 ;;
        *) return 1 ;;
      esac
      ;;
    stop) [[ $* == 'stop --timeout 15 arch-workstation-telemetry-check-'* ]] || return 1 ;;
    *) return 1 ;;
  esac
}
# No wall-clock waits are needed for the synthetic log/client responses.
# shellcheck disable=SC2329
sleep() { [[ $1 == 1 || $1 == 2 ]]; }
export -f docker sleep
if bash "$TELEMETRY_FIXTURE_ROOT/infrastructure/observability/validate-images.sh" fixture-local >"$scratch/validator.log" 2>&1; then
  :
else
  # These bounded diagnostics contain only our synthetic launch probes.
  sed -n '1,40p' "$scratch/validator.log" >&2
  for log in "$TELEMETRY_FIXTURE_ROOT"/test-results/telemetry-images.*/*.log; do
    [[ ! -f $log ]] || sed -n '1,40p' "$log" >&2
  done
  exit 1
fi
ruby - "$TELEMETRY_FIXTURE_ROOT" <<'RUBY'
root = ARGV[0]
raise 'missing or duplicate consumer launches' unless File.readlines("#{root}/launches", chomp: true) == %w[rules cluster-full cluster-metrics host-full host-metrics host otlp-server otlp-client]
output = Dir.glob("#{root}/test-results/telemetry-images.*").fetch(0)
raise 'evidence parent lost privacy' unless File.stat(output).mode & 07777 == 0700
public_files = %w[rules/alerts.yaml rules/alerts_test.yaml fixture/check.alloy fixture/installation.log fixture/kernel.log otlp/check.alloy otlp/traces.json cluster-full/config.alloy cluster-metrics/config.alloy host-full/config.alloy host-metrics/config.alloy]
Dir.glob("#{output}/**/*").each do |path|
  raise 'unexpected symlink' if File.symlink?(path)
  next unless File.file?(path)
  expected = public_files.include?(path.delete_prefix("#{output}/")) ? 0644 : 0600
  raise "retained permissions changed: #{path}" unless File.stat(path).mode & 07777 == expected
end
%w[prometheus.log alloy.log loki.log tempo.log processor.log otlp.log otlp-client.log stop.log otlp-stop.log otlp-network-remove.log images.txt].each do |name|
  raise "missing retained evidence: #{name}" unless File.file?("#{output}/#{name}")
end
puts 'Telemetry fixture launch-time permissions and retained-log privacy passed (synthetic Docker only).'
RUBY
