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
check(total_memory == 5248, "unexpected memory budget #{total_memory}")
check(total_memory <= 6656, 'stack exceeds 6.5Gi envelope')
check(documents.none? { |d| %w[Secret Ingress].include?(d['kind']) }, 'credential or public endpoint embedded')
documents.select { |d| d['kind'] == 'Service' }.each do |service|
  check(service.dig('spec', 'type') == 'ClusterIP', 'public service')
end
grafana = object(documents, 'Deployment', 'grafana')
env = grafana.dig('spec', 'template', 'spec', 'containers', 0, 'env')
container = grafana.dig('spec', 'template', 'spec', 'containers', 0)
check(container.dig('resources', 'requests', 'memory') == '1024Mi' &&
      container.dig('resources', 'limits', 'memory') == '1024Mi', 'Grafana memory reservation/limit drift')
check(env.find { |e| e['name'] == 'GOMEMLIMIT' }['value'] == '819MiB', 'Grafana Go memory headroom lost')
quota = object(documents, 'ResourceQuota', 'observability-budget')
namespace_memory = workloads.select { |w| w.dig('metadata', 'namespace') == 'workstation-observability' }
                            .sum { |w| w.dig('spec', 'template', 'spec', 'containers').sum { |c| c.dig('resources', 'limits', 'memory').to_i } }
check(quota.dig('spec', 'hard', 'limits.memory').to_i >= namespace_memory, 'namespace quota must admit the complete stack')
limits = JSON.parse(File.read("#{root}/versions.json"))
check(limits['runtime_limits_mib'] == total_memory && limits['runtime_limits_with_host_alloy_mib'] == total_memory + 512, 'runtime memory metadata drift')
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
metrics_alloy = File.read("#{root}/config/config.metrics.alloy")
check(!metrics_alloy.include?('logs =') && !metrics_alloy.include?('traces =') && !metrics_alloy.include?('loki.'), 'metrics profile retains log/trace collection')
check(metrics_alloy.include?('otelcol.receiver.otlp "local"') && metrics_alloy.include?('metrics ='), 'metrics profile lost Bridge metric intake')
metrics_host = File.read("#{root}/host/config.metrics.alloy")
check(!metrics_host.include?('loki.') && !metrics_host.include?('traces ='), 'metrics host profile retains journal/log/trace collection')
check(metrics_host.include?('prometheus.scrape "self"') && metrics_host.include?('otelcol.receiver.otlp "bridge"'), 'metrics host profile lost health or Bridge metrics')
check(File.read("#{root}/config/loki.yaml").include?('retention_period: 168h'), 'missing log retention')
check(File.read("#{root}/config/tempo.yaml").include?('block_retention: 72h'), 'missing trace retention')
dashboard = JSON.parse(File.read("#{root}/dashboards/workstation.json"))
serving = JSON.parse(File.read("#{root}/dashboards/workloads.json"))
power = JSON.parse(File.read("#{root}/dashboards/power.json"))
check(dashboard['uid'] == 'workstation-overview' && dashboard['panels'].length <= 12, 'overview bookmark or compact scope lost')
queue = serving['panels'].find { |p| p['title'] == 'SGLang queued requests' }
check(queue['targets'][0]['expr'] == 'sglang:num_queue_reqs{job="sglang",priority=""}', 'queue total double-counts priority breakdowns')
psu_panels = power['panels'].select { |p| (105..108).include?(p['id']) }
check(psu_panels.length == 4, 'missing PSU power, rails, energy or coverage panel')
psu_panels.each do |panel|
  expr = panel.fetch('targets')[0].fetch('expr')
  check(expr.include?('job="node"') && expr.include?('sample_timestamp_seconds') && expr.include?('<= 90'), 'PSU panel presents stale textfile data as live')
  check(expr.include?('- time() <= 10'), 'PSU panel accepts a future source clock')
  check(!expr.include?('sum(') && !expr.include?('or vector(0)'), 'PSU panel hides ambiguous or missing measurements')
end
check(psu_panels[0]['title'].include?('not wall power'), 'DC output represented as AC input')
check(psu_panels[2]['targets'][0]['expr'].include?('/ 3600000'), 'incorrect joule to kWh conversion')
check(psu_panels[2]['description'].include?('NOT energy for the selected dashboard range'), 'cumulative energy scope lost')
check(psu_panels[3]['targets'][0]['expr'].include?('energy_covered_seconds_total'), 'energy coverage omitted')
%w[prometheus.yaml alerts.yaml loki.yaml tempo.yaml datasources.yaml datasources.metrics.yaml dashboards.yaml].each do |name|
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
[host, metrics_host].each do |profile|
  check(profile.include?('sys.env("WORKSTATION_SGLANG_METRICS_ADDRESS")'), 'native SGLang target must be explicit and opt-in')
  check(profile.include?('targets = discovery.relabel.sglang_local.output'), 'native scrape bypasses loopback admission')
  pattern = profile[/discovery\.relabel "sglang_local" \{.*?regex = "([^"]+)"/m, 1] || raise('native loopback guard absent')
  guard = Regexp.new("\\A(?:#{pattern})\\z")
  %w[127.0.0.1:1 127.0.0.1:18088 127.0.0.1:65535].each { |target| check(guard.match?(target), 'valid loopback target rejected') }
  ['', '127.0.0.1:0', '127.0.0.1:65536', '127.0.0.1:018088', '192.168.2.15:18088', 'http://127.0.0.1:18088', 'user:pass@127.0.0.1:18088'].each do |target|
    check(!guard.match?(target), 'invalid or external native target admitted')
  end
  check(profile.include?('regex = "(up|scrape_.*|sglang:.*)"') && profile.include?('regex = "(request_id|session_id|turn_id|user_id|prompt|completion|token|authorization|cookie)"'), 'native scrape lost metric/privacy bounds')
end
# The actual SGLang endpoint emits colon names to plain curl but underscore
# names under Alloy's negotiated Accept header. Exercise both formats and
# unrelated jobs using the normalizer from each generated collector profile.
[host, metrics_host, alloy, metrics_alloy].each do |profile|
  normalizer = profile[/rule \{\s*action = "replace"\s*source_labels = \["job", "__name__"\]\s*regex = "([^"]+)"\s*target_label = "__name__"\s*replacement = "([^"]+)"\s*\}/m]
  check(normalizer, 'SGLang negotiated metric normalization absent')
  pattern = normalizer[/regex = "([^"]+)"/, 1]
  replacement = normalizer[/replacement = "([^"]+)"/, 1]
  check(replacement == 'sglang:$1', 'unexpected SGLang replacement contract')
  rename = lambda do |job, name|
    matched = Regexp.new("\\A(?:#{pattern})\\z").match("#{job};#{name}")
    matched ? replacement.sub('$1', matched[1]) : name
  end
  %w[num_running_reqs num_queue_reqs generation_tokens_total time_to_first_token_seconds_bucket inter_token_latency_seconds_count].each do |suffix|
    check(rename.call('sglang', "sglang_#{suffix}") == "sglang:#{suffix}", 'negotiated SGLang family lost')
    check(rename.call('sglang', "sglang:#{suffix}") == "sglang:#{suffix}", 'legacy SGLang family changed')
    check(rename.call('node', "sglang_#{suffix}") == "sglang_#{suffix}", 'unrelated job renamed')
  end
  %w[up scrape_samples_scraped python_info unrelated_metric sglangx_num_queue_reqs].each do |name|
    check(rename.call('sglang', name) == name, 'unrelated metric renamed')
  end
  if profile.include?('prometheus.relabel "sglang_local"')
    block = profile.split('prometheus.relabel "sglang_local" {', 2).last.split("\n}", 2).first
    check(block.index(normalizer) < block.index('action = "keep"'), 'normalization occurs after family drop')
  end
end
check(File.read("#{root}/../../templates/workstation/telemetry/workstation-alloy.service").include?('EnvironmentFile=-/etc/workstation-telemetry/sglang-metrics.env'), 'native SGLang opt-in not packaged')
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
