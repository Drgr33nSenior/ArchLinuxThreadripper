#!/usr/bin/env bash
# GPU allocation through DRA: the DeviceConfig enables exactly one allocator,
# pins the DRA driver (with a rollback release), and the pilot claims/probe stay well-formed.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
ruby -ryaml - "$root" <<'RUBY'
root = ARGV.fetch(0)
def check(ok, message)
  abort("FAIL: #{message}") unless ok
end
op = "#{root}/infrastructure/gpu-operator"
spec = YAML.load_stream(File.read("#{op}/deviceconfig.yaml")).first.fetch('spec')
plugin = spec.dig('devicePlugin', 'enableDevicePlugin') == true
dra = spec.dig('draDriver', 'enable') == true
check(plugin ^ dra, 'enable exactly one of the device plugin and the DRA driver (the operator rejects both)')
values = YAML.load_file("#{op}/values.yaml")
check(values.dig('draDriver', 'deviceClass', 'create') == dra, 'the gpu.amd.com DeviceClass must exist exactly when DRA is enabled')
# An upstream release must be v1.0.1+ (it publishes GPUs when the in-kernel amdgpu module
# reports no version). A pipeline build (the dra stage) is tagged with its stage key.
release = ->(ref) { ref.match(%r{\Adocker\.io/rocm/k8s-gpu-dra-driver:v(\d+)\.(\d+)\.(\d+)@sha256:[a-f0-9]{64}\z}) }
release_ok = ->(m) { m && (m[1..3].map(&:to_i) <=> [1, 0, 1]) >= 0 }
if dra
  image = spec.dig('draDriver', 'image').to_s
  lock = File.read("#{root}/infrastructure/ai-stack/stack.lock")
  repository = lock[/^DRA_IMAGE_REPOSITORY=(\S+)$/, 1]
  check(repository, 'stack.lock names no DRA_IMAGE_REPOSITORY')
  pipeline = image.match?(%r{\A#{Regexp.escape(repository)}:[a-f0-9]{12}@sha256:[a-f0-9]{64}\z})
  check(pipeline || release_ok.(release.(image)),
        'DRA driver image must be the dra stage build or an upstream release v1.0.1+, pinned by digest (operator default is :latest)')
  if pipeline
    rollback = File.read("#{op}/deviceconfig.yaml")[/^ *# Rollback image: (\S+)$/, 1].to_s
    check(release_ok.(release.(rollback)), 'a pipeline-built DRA driver needs a documented upstream release v1.0.1+ to roll back to')
  end
end
claims = YAML.load_stream(File.read("#{op}/claims.yaml")).compact
check(claims.map { |c| c.dig('metadata', 'name') } == %w[gpu-0 gpu-1], 'expected claims gpu-0 and gpu-1')
buses = claims.map do |c|
  check(c['apiVersion'] == 'resource.k8s.io/v1' && c['kind'] == 'ResourceClaim', 'claims use resource.k8s.io/v1')
  check(c.dig('metadata', 'namespace') == 'ai-home-lab', 'claims live with their workloads in ai-home-lab')
  request = c.dig('spec', 'devices', 'requests').fetch(0).fetch('exactly')
  check(request['deviceClassName'] == 'gpu.amd.com' && request['count'] == 1, 'one gpu.amd.com device per claim')
  expr = request.fetch('selectors').fetch(0).dig('cel', 'expression')
  bus = expr[/pciBusID == "([0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7])"\z/, 1]
  check(bus, 'each claim selects one card by its PCI address')
  bus
end
check(buses.uniq.length == 2, 'the two claims must select different cards')
probe = YAML.load_file("#{op}/dra-pilot/probe.yaml")
pod = probe.fetch('spec'); ctr = pod.fetch('containers').fetch(0)
check(pod.dig('securityContext', 'runAsNonRoot') == true && pod.dig('securityContext', 'seccompProfile', 'type') == 'RuntimeDefault', 'probe must satisfy restricted Pod Security')
check(ctr.dig('securityContext', 'allowPrivilegeEscalation') == false && ctr.dig('securityContext', 'capabilities', 'drop') == ['ALL'], 'probe container must drop privileges')
check(ctr['image'].match?(/@sha256:[a-f0-9]{64}\z/), 'probe image must be pinned by digest')
check(pod['resourceClaims'] == [{'name' => 'gpu', 'resourceClaimName' => 'gpu-1'}] && ctr.dig('resources', 'claims') == [{'name' => 'gpu'}], 'probe shares the gpu-1 claim')
check(!ctr.dig('resources', 'limits').key?('amd.com/gpu'), 'probe must not request the device-plugin resource')
RUBY
echo "GPU DRA allocator, pinned driver and pilot claim/probe checks passed"
