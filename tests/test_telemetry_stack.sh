#!/usr/bin/env bash
# Source-only contract checks: never contact or mutate a Kubernetes cluster.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
for tool in kubectl ruby; do
  command -v "$tool" >/dev/null || {
    printf 'BLOCKED: telemetry source validation requires %s\n' "$tool" >&2
    exit 1
  }
done
scratch=$(mktemp -d)
trap 'rm -rf -- "$scratch"' EXIT
kubectl kustomize "$root/infrastructure/observability" >"$scratch/rendered.yaml"
kubectl kustomize "$root/infrastructure/observability" >"$scratch/second.yaml"
cmp "$scratch/rendered.yaml" "$scratch/second.yaml"
cp -R "$root/infrastructure/observability" "$scratch/with-kubelet"
printf '\ncomponents: [kubelet]\n' >>"$scratch/with-kubelet/kustomization.yaml"
kubectl kustomize "$scratch/with-kubelet" >"$scratch/kubelet.yaml"
ruby -ryaml -rjson - "$scratch/rendered.yaml" "$root/infrastructure/observability" <<'RUBY'
documents = YAML.load_stream(File.read(ARGV[0])).compact
root = ARGV[1]
def check(condition, message)
  raise message unless condition
end
def object(documents, kind, name)
  documents.find { |d| d['kind'] == kind && d.dig('metadata', 'name') == name } || raise("missing #{kind}/#{name}")
end
images = JSON.parse(File.read("#{root}/versions.json"))['images'].map { |i| i['image'] }
workloads = documents.select { |d| %w[Deployment DaemonSet].include?(d['kind']) }
check(workloads.size == 7, 'expected seven explicit workloads')
total_memory = 0
workloads.each do |workload|
  check(workload.dig('spec', 'strategy', 'type') == 'Recreate', 'rollout surge exceeds bounded quota or overlaps RWO') if workload['kind'] == 'Deployment'
  pod = workload.dig('spec', 'template', 'spec')
  check(pod.dig('securityContext', 'runAsNonRoot') == true, 'root pod')
  check(pod.dig('securityContext', 'seccompProfile', 'type') == 'RuntimeDefault', 'missing seccomp')
  check(!pod['hostNetwork'] && !pod['hostPID'], 'unreviewed host namespace sharing')
  pod['containers'].each do |container|
    check(images.include?(container['image']), 'unlocked image')
    check(container['image'].match?(/@sha256:[0-9a-f]{64}$/), 'unpinned image')
    security = container['securityContext']
    check(security['allowPrivilegeEscalation'] == false && security['readOnlyRootFilesystem'] == true, 'container hardening')
    check(security.dig('capabilities', 'drop') == ['ALL'] && !security['privileged'], 'capability escalation')
    limits = container.dig('resources', 'limits')
    check(!limits.key?('amd.com/gpu'), 'telemetry must not reserve an inference GPU')
    check(limits['memory'].match?(/\A\d+Mi\z/), 'explicit bounded memory')
    total_memory += limits['memory'].to_i
  end
  next if workload.dig('metadata', 'name') == 'node-exporter'
  check((pod['volumes'] || []).none? { |v| v.key?('hostPath') }, 'hostPath outside dedicated exporter')
end
check(total_memory == 4736, "unexpected memory budget #{total_memory}")
check(total_memory <= 6144, 'stack exceeds 6Gi envelope')
check(documents.none? { |d| %w[Secret Ingress].include?(d['kind']) }, 'credential or public endpoint embedded')
documents.select { |d| d['kind'] == 'Service' }.each do |service|
  check(service.dig('spec', 'type') == 'ClusterIP', 'public service')
end
grafana = object(documents, 'Deployment', 'grafana')
env = grafana.dig('spec', 'template', 'spec', 'containers', 0, 'env')
%w[GF_SECURITY_ADMIN_USER GF_SECURITY_ADMIN_PASSWORD].each do |key|
  check(env.find { |e| e['name'] == key }.dig('valueFrom', 'secretKeyRef', 'name') == 'grafana-admin', 'missing owner auth')
end
%w[workstation-observability workstation-observability-host].each do |namespace|
  policy = documents.find { |d| d['kind'] == 'NetworkPolicy' && d.dig('metadata', 'namespace') == namespace && d.dig('metadata', 'name') == 'default-deny' }
  check(policy && policy.dig('spec', 'policyTypes').sort == %w[Egress Ingress], 'missing deny policy')
end
check(object(documents, 'Namespace', 'workstation-observability').dig('metadata', 'labels', 'pod-security.kubernetes.io/enforce') == 'restricted', 'application admission weakened')
roles = documents.select { |d| d['kind'] == 'ClusterRole' }
roles.each do |role|
  role['rules'].each do |rule|
    check((rule['verbs'] - %w[list watch]).empty?, 'non-observation RBAC')
    check((rule['resources'] & %w[secrets configmaps nodes/proxy]).empty?, 'sensitive or proxy RBAC')
  end
end
pvcs = documents.select { |d| d['kind'] == 'PersistentVolumeClaim' }
check(pvcs.length == 5, 'missing persistent backend/cache state')
check(pvcs.all? { |p| p.dig('spec', 'storageClassName') == 'local-path' }, 'unexpected replication/storage backend')
check(pvcs.sum { |p| p.dig('spec', 'resources', 'requests', 'storage').to_i } == 64, 'storage budget drift')
alloy = File.read("#{root}/config/config.alloy")
check(!alloy.include?('loki.source.kubernetes') && !alloy.include?('loki.source.file'), 'arbitrary raw logs collection')
check(alloy.include?('set(body, "operational event")') && alloy.include?('keep_keys(attributes'), 'missing secret minimisation')
check(alloy.include?('set(name, "sglang.request")'), 'request IDs retained in SGLang span names')
check(alloy.include?('context = "scope"') && alloy.include?('keep_keys(attributes, [])'), 'scope attributes can escape telemetry')
check(alloy.include?('Len(span.links) > 0'), 'linked spans can export unreviewed link attributes')
check(alloy.include?('limit = "384MiB"') && alloy.include?('queue_size = 128'), 'unbounded collector memory/queue')
check(alloy.include?('max_keepalive_time = "2h"'), 'unbounded metrics WAL retention')
check(!alloy.include?('debug {') && !alloy.include?('otelcol.exporter.debug'), 'raw payload diagnostics enabled')
check(File.read("#{root}/config/loki.yaml").include?('retention_period: 168h'), 'missing log retention')
check(File.read("#{root}/config/tempo.yaml").include?('block_retention: 72h'), 'missing trace retention')
dashboard = JSON.parse(File.read("#{root}/dashboards/workstation.json"))
queue = dashboard['panels'].find { |p| p['title'] == 'SGLang queued requests' }
check(queue['targets'][0]['expr'] == 'sglang:num_queue_reqs{job="sglang",priority=""}', 'queue total double-counts priority breakdowns')
%w[prometheus.yaml alerts.yaml loki.yaml tempo.yaml datasources.yaml dashboards.yaml].each do |name|
  YAML.load_file("#{root}/config/#{name}")
end
check(JSON.parse(File.read("#{root}/versions.json"))['deferred_amd_exporter']['status'] == 'deferred-not-deployed', 'AMD qualification silently promoted')
host = File.read("#{root}/host/config.alloy")
check(host.include?('matches = "SYSLOG_IDENTIFIER=workstation-install"'), 'unbounded journal collection')
check(host.include?('stage.static_labels'), 'journal source overwrites job before schema filtering')
check(host.include?('endpoint = "127.0.0.1:4318"'), 'public host receiver')
check(host.include?('prometheus.scrape "self"') && host.include?('job" = "workstation-host-alloy"'), 'host Alloy does not self-scrape with a stable identity')
check(host.include?('prometheus.remote_write "self"') && host.include?('http://192.0.2.5:9090/api/v1/write'), 'host Alloy health has no bounded central path')
check(host.include?('capacity = 64') && host.include?('max_keepalive_time = "30m"'), 'host Alloy self-monitoring is unbounded')
check(host.include?('loki_(source_journal_.*|process_dropped_lines_total|write_.*)'), 'host journal-drop and write counters are not forwarded')
alerts = File.read("#{root}/config/alerts.yaml")
check(alerts.include?('processor(_memory_limiter)?_refused_(spans|log_records|metric_points)_total'), 'refused telemetry alert misses a pinned collector family or metric points')
check(alerts.include?('otelcol_exporter_send_failed_metric_points_total'), 'metrics-only host export failure is not alerted')
check(alerts.include?('WorkstationHostAlloyHeartbeatMissing') && alerts.include?('absent_over_time(up{job="workstation-host-alloy"}[5m])'), 'host Alloy forwarding loss is not alerted centrally')
check(File.file?("#{root}/../../tests/fixtures/telemetry/alerts_test.yaml"), 'missing executable Prometheus rule fixture')
host_write = object(documents, 'NetworkPolicy', 'prometheus-from-workstation-9090')
host_ingress = host_write.dig('spec', 'ingress')
check(host_ingress == [{'from' => [{'ipBlock' => {'cidr' => '192.0.2.2/32'}}], 'ports' => [{'protocol' => 'TCP', 'port' => 9090}]}], 'host self-monitoring Prometheus ingress is broader than the configured workstation identity')
pattern = host[/expression = `([^\n]+)`/, 1] || raise('missing fixed event pattern')
pattern = Regexp.new(pattern.gsub('(?P<', '(?<'))
event = {'schema'=>1,'run_id'=>'20260911T120000Z','configuration_sha256'=>'a'*64,'stage'=>'preflight','mode'=>'dry-run','outcome'=>'succeeded','exit_code'=>0,'timestamp'=>'2026-09-11T12:00:00Z'}
check(pattern.match?(JSON.generate(event)), 'canonical install event rejected')
check(!pattern.match?(JSON.generate(event.merge('password'=>'DUMMY_TELEMETRY_SECRET_SENTINEL'))), 'extra secret-bearing field accepted')
check(!pattern.match?(JSON.generate(event.merge('stage'=>'DUMMY_TELEMETRY_SECRET_SENTINEL'))), 'unreviewed stage accepted')
check(!pattern.match?(JSON.generate(event.merge('stage'=>'dummy-telemetry-secret-sentinel'))), 'lowercase secret-bearing stage accepted')
%w[event-sink preflight install verify safe-target owner-confirmation partitions storage target-mount header-backup base-packages target-config system-config boot-package bridge-package firmware-entries].each do |stage|
  check(pattern.match?(JSON.generate(event.merge('stage'=>stage))), 'documented installer stage rejected')
end
check(!pattern.match?('raw console DUMMY_TELEMETRY_SECRET_SENTINEL'), 'raw console accepted')
check(host.include?('source = "safe_message"'), 'raw kernel messages retained')
puts 'Telemetry render, pinning, budgets, permissions, network, retention and privacy contracts passed; no target deployment.'
RUBY
(
  # The optional image validator must refuse a remote context before any run.
  # Invoked by the child Bash validator through the exported function.
  # shellcheck disable=SC2329
  docker() { printf 'tcp://unreviewed.invalid:2376\n'; }
  export -f docker
  if bash "$root/infrastructure/observability/validate-images.sh" unreviewed >"$scratch/remote.log" 2>&1; then
    printf 'Image validator accepted a remote context\n' >&2
    exit 1
  fi
  grep -q 'requires a reviewed local Unix-socket Docker context' "$scratch/remote.log"
)
ruby -ryaml - "$scratch/kubelet.yaml" <<'RUBY'
documents = YAML.load_stream(File.read(ARGV[0])).compact
role = documents.find { |d| d['kind'] == 'ClusterRole' && d.dig('metadata', 'name') == 'workstation-alloy-kubelet' }
raise 'missing least-privilege kubelet role' unless role['rules'] == [
  {'apiGroups' => [''], 'resources' => ['nodes/metrics'], 'resourceNames' => ['review-required-node'], 'verbs' => ['get']}
]
alloy = documents.find { |d| d['kind'] == 'Deployment' && d.dig('metadata', 'name') == 'alloy' }
raise 'missing projected identity' unless alloy.dig('spec', 'template', 'spec', 'serviceAccountName') == 'workstation-alloy-kubelet'
config = documents.find { |d| d['kind'] == 'ConfigMap' && d['data'].key?('kubelet.alloy') }
raise 'lost main config' unless config['data'].key?('config.alloy')
raise 'insecure kubelet TLS' if config['data']['kubelet.alloy'].include?('insecure_skip_verify =')
raise 'missing kubelet CA' unless config['data']['kubelet.alloy'].include?('ca_file =')
puts 'Optional kubelet component rendering and least-privilege TLS contracts passed; actual node access untested.'
RUBY
