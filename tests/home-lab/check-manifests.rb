require 'yaml'

def require_check(ok, message)
  abort message unless ok
end

def documents(directory, overlay)
  YAML.load_stream(File.read(File.join(directory, "#{overlay}.yaml"))).compact
end

def named(docs, kind, name)
  docs.find { |d| d['kind'] == kind && d.dig('metadata', 'name') == name } || abort("Missing #{kind}/#{name}")
end

def guaranteed_resources(container)
  resources = container.fetch('resources')
  %w[cpu memory].each do |resource|
    require_check(resources.dig('requests', resource) == resources.dig('limits', resource),
                  "#{container['name']} is not Guaranteed for #{resource}")
  end
end

def guaranteed_pod(pod)
  (pod.fetch('initContainers', []) + pod.fetch('containers')).each { |container| guaranteed_resources(container) }
end

directory = ARGV.fetch(0)
family = documents(directory, 'family')
deployments = family.select { |d| d['kind'] == 'Deployment' }
require_check(deployments.length == 5, 'Family overlay lost a workload')
require_check(family.none? { |d| d['kind'] == 'Secret' }, 'Never render credential values')
deployments.each do |d|
  require_check(d.dig('spec', 'replicas') == 0, 'Unqualified workloads must stay disabled')
  pod = d.dig('spec', 'template', 'spec')
  require_check(pod['automountServiceAccountToken'] == false, 'Workloads must not receive API credentials')
  require_check(!pod['hostNetwork'] && !pod['hostPID'] && !pod['hostIPC'], 'Host namespaces are not allowed')
  require_check(pod.fetch('volumes', []).none? { |v| v['hostPath'] }, 'Do not bypass device allocation with hostPath')
  pod['containers'].each do |c|
    require_check(c.dig('securityContext', 'privileged') != true, 'Privileged workload found')
    require_check(c.fetch('env', []).none? { |e| e['name'] == 'HIP_VISIBLE_DEVICES' }, 'GPU ordinal is not physical allocation')
    next unless c.dig('resources', 'limits', 'amd.com/gpu')
    require_check(c.dig('resources', 'requests', 'amd.com/gpu') == c.dig('resources', 'limits', 'amd.com/gpu'), 'GPU request/limit mismatch')
  end
end

single = documents(directory, 'single-gpu')
dual = documents(directory, 'dual-gpu')
lock_path = File.expand_path('../../versions.lock', __dir__)
lock = File.readlines(lock_path).reject { |line| line.start_with?('#') || line.strip.empty? }
           .map { |line| line.strip.split('=', 2) }.to_h
image_entries = File.readlines(lock_path).select { |line| line.start_with?('SGLANG_ROCM_IMAGE=') }
require_check(image_entries.length == 1, 'Expected one SGLang image lock')
locked_image = image_entries[0].strip.split('=', 2)[1]
require_check(locked_image.match?(/\Adocker\.io\/rocm\/sgl-dev:v0\.5\.15\.post1-ubuntu24\.04-py3\.14-rocm10\.0\.0@sha256:[a-f0-9]{64}\z/),
              'SGLang needs the reviewed AMD ROCm 10 release and an immutable digest')
%w[default single-gpu dual-gpu rdna4-compat family].each do |overlay|
  rendered = documents(directory, overlay)
  deployment = named(rendered, 'Deployment', 'sglang')
  require_check(deployment.dig('spec', 'replicas') == 0 &&
                deployment.dig('metadata', 'annotations', 'ai-home-lab.local/qualification') == 'pending-rocm10-target-and-model-validation' &&
                deployment.dig('metadata', 'annotations', 'workstation.ai/qualification') != 'qualified',
                'Pinning the AMD image must not qualify or enable SGLang')
  spec = deployment.dig('spec', 'template', 'spec')
  require_check(spec['nodeSelector'] == {'ai-home-lab.local/gfx' => 'gfx1201', 'kubernetes.io/arch' => 'amd64', 'kubernetes.io/os' => 'linux'},
                'SGLang image platform and GPU selectors changed')
  require_check(spec.dig('securityContext', 'runAsNonRoot') == true && spec.dig('securityContext', 'runAsUser') == 1000 &&
                spec.dig('securityContext', 'seccompProfile', 'type') == 'RuntimeDefault', 'Do not inherit the vendor image root user or unconfined seccomp')
  candidate = spec['containers'][0]
  require_check(deployment.dig('spec', 'progressDeadlineSeconds') == 2100 &&
                candidate.dig('startupProbe', 'periodSeconds') * candidate.dig('startupProbe', 'failureThreshold') == 1800 &&
                spec['terminationGracePeriodSeconds'] == 120,
                'AI startup allowance must stay separate from unchanged termination grace')
  require_check(candidate['image'] == locked_image, 'Rendered SGLang image differs from versions.lock')
  require_check(candidate['workingDir'] == '/tmp' && candidate.dig('securityContext', 'allowPrivilegeEscalation') == false &&
                candidate.dig('securityContext', 'capabilities', 'drop') == ['ALL'] &&
                candidate.dig('securityContext', 'capabilities', 'add').nil?, 'SGLang must retain a writable working directory without elevated capabilities')
  require_check(candidate['args'].each_cons(2).include?(['--attention-backend', '$(ATTENTION_BACKEND)']), 'Attention backend must use the existing profile ConfigMap')
  settings = rendered.find { |d| d['kind'] == 'ConfigMap' && d.dig('metadata', 'name').start_with?('sglang-profile-') }
  mode = %w[default dual-gpu].include?(overlay) ? 'DUAL' : 'SINGLE'
  model = lock.fetch("SGLANG_#{mode}_MODEL_REPOSITORY")
  revision = lock.fetch("SGLANG_#{mode}_MODEL_REVISION")
  model_name = model.split('/').last
  require_check(model == (mode == 'DUAL' ? 'Qwen/Qwen3.8-27B-FP8' : 'Qwen/Qwen3.5-9B'),
                "Unexpected reviewed model for #{overlay}")
  require_check(revision.match?(/\A[a-f0-9]{40}\z/), 'Model revision must be immutable')
  {'MODEL_REPOSITORY' => model, 'MODEL_REVISION' => revision,
   'MODEL_PATH' => "/models/#{model_name}/#{revision}", 'SERVED_MODEL_NAME' => model_name,
   'MODEL_DTYPE' => 'bfloat16', 'REASONING_PARSER' => 'qwen3', 'TOOL_CALL_PARSER' => 'qwen3_coder',
   'TENSOR_PARALLEL' => (mode == 'DUAL' ? '2' : '1'),
   'CONTEXT_LENGTH' => (mode == 'DUAL' ? '32768' : '4096')}.each do |key, value|
    require_check(settings.dig('data', key) == value, "Model profile/lock drift: #{overlay}/#{key}")
    require_check(candidate.fetch('env', []).none? { |e| e['name'] == key }, "Inline environment shadows model setting #{key}")
  end
  {'--revision' => 'MODEL_REVISION', '--served-model-name' => 'SERVED_MODEL_NAME',
   '--dtype' => 'MODEL_DTYPE', '--reasoning-parser' => 'REASONING_PARSER',
   '--tool-call-parser' => 'TOOL_CALL_PARSER'}.each do |flag, key|
    require_check(candidate['args'].each_cons(2).include?([flag, "$(#{key})"]), "Model flag missing: #{flag}")
  end
  require_check(!candidate['args'].include?('--quantization') && !settings.fetch('data').key?('QUANTIZATION'),
                'Read quantization from the checkpoint; never force the old AWQ format')
  candidate['args'].join(' ').scan(/\$\(([A-Z_]+)\)/).flatten.each do |key|
    require_check(settings.fetch('data').key?(key), "Unresolved model argument #{key}")
  end
  {'ATTENTION_BACKEND' => 'triton', 'SGLANG_USE_AITER' => 'false', 'SGLANG_USE_AITER_AR' => 'false', 'SGLANG_ROCM_FUSED_DECODE_MLA' => 'false'}.each do |key, value|
    require_check(settings.dig('data', key) == value, "Missing AMD Radeon setting #{key} in #{overlay}")
    require_check(candidate.fetch('env', []).none? { |e| e['name'] == key }, "Inline environment shadows Radeon profile setting #{key}")
  end
  {'XDG_CACHE_HOME' => '/cache/xdg', 'TRITON_CACHE_DIR' => '/cache/triton', 'TORCHINDUCTOR_CACHE_DIR' => '/cache/torchinductor'}.each do |key, value|
    require_check(candidate.fetch('env').include?({'name' => key, 'value' => value}), "JIT cache #{key} must stay on the existing persistent cache volume")
  end
end
sglang = named(dual, 'Deployment', 'sglang')
pod = sglang.dig('spec', 'template', 'spec')
require_check(pod['priorityClassName'] == 'heavy-ai-priority', 'Dual GPU needs preempting priority')
require_check(pod['containers'][0].dig('resources', 'limits', 'amd.com/gpu') == '2', 'Dual GPU must reserve both cards')
require_check(pod['containers'][0].dig('resources', 'limits') == {'cpu' => '24', 'memory' => '38Gi', 'amd.com/gpu' => '2'}, 'Dual GPU SGLang must leave room for CPU RAG and system pods')
guaranteed_pod(pod)
require_check(pod['volumes'].any? { |v| v['name'] == 'shm' && v['emptyDir'] == {'medium' => 'Memory', 'sizeLimit' => '16Gi'} }, 'Missing bounded shared memory')
require_check(pod['volumes'].any? { |v| v['name'] == 'cache' && v.dig('persistentVolumeClaim', 'claimName') == 'sglang-hf-cache' }, 'SGLang HF cache PVC is missing')
container = pod['containers'][0]
require_check(container['volumeMounts'].any? { |mount| mount['name'] == 'cache' && mount['mountPath'] == '/cache' }, 'SGLang cache PVC is not mounted at /cache')
require_check(container['volumeMounts'].any? { |mount| mount['name'] == 'models' && mount['mountPath'] == '/models' && mount['readOnly'] == true }, 'SGLang model PVC must stay read-only')
profile = dual.find { |d| d['kind'] == 'ConfigMap' && d.dig('metadata', 'name').start_with?('sglang-profile-') }
require_check(profile.dig('data', 'TENSOR_PARALLEL') == '2', 'Tensor parallelism did not update')
require_check(profile.dig('data', 'MEM_FRACTION_STATIC') == '0.80', 'SGLang static memory fraction is not configured')
require_check(profile.dig('data', 'MAX_RUNNING_REQUESTS') == '2', 'SGLang request concurrency is not configured')
require_check(container['args'].each_cons(2).include?(['--mem-fraction-static', '$(MEM_FRACTION_STATIC)']), 'SGLang static memory flag is not ConfigMap-backed')
require_check(container['args'].each_cons(2).include?(['--max-running-requests', '$(MAX_RUNNING_REQUESTS)']), 'SGLang request limit flag is not ConfigMap-backed')
require_check(container['args'].none? { |arg| arg.start_with?('--cpu-offload') }, 'Unqualified SGLang CPU-offload flag was added')
cache = named(dual, 'PersistentVolumeClaim', 'sglang-hf-cache')
require_check(cache.dig('spec', 'storageClassName') == 'local-path' && cache.dig('spec', 'resources', 'requests', 'storage') == '16Gi', 'SGLang HF cache PVC must request 16Gi local-path storage')
require_check(cache.dig('metadata', 'annotations', 'ai-home-lab.local/capacity-note') == 'advisory-request-not-local-path-filesystem-quota', 'SGLang cache local-path capacity caveat is missing')
require_check(named(single, 'Deployment', 'sglang').dig('spec', 'template', 'spec', 'containers')[0]['env'].none? { |e| e['name'].start_with?('HSA_') }, 'Overrides leaked into normal profile')
single_sglang = named(single, 'Deployment', 'sglang').dig('spec', 'template', 'spec')
require_check(single_sglang['containers'][0].dig('resources', 'limits') == {'cpu' => '20', 'memory' => '32Gi', 'amd.com/gpu' => '1'}, 'Single GPU SGLang resource envelope changed')
guaranteed_pod(single_sglang)
swarmui = named(single, 'Deployment', 'swarmui').dig('spec', 'template', 'spec')
require_check(swarmui['containers'][0].dig('resources', 'limits') == {'cpu' => '12', 'memory' => '12Gi', 'amd.com/gpu' => '1'}, 'SwarmUI resource envelope changed')
require_check(swarmui['initContainers'][0].dig('resources', 'limits') == {'cpu' => '2', 'memory' => '64Mi'}, 'SwarmUI init resource envelope changed')
guaranteed_pod(swarmui)
compat = documents(directory, 'rdna4-compat')
env = named(compat, 'Deployment', 'sglang').dig('spec', 'template', 'spec', 'containers')[0]['env']
require_check(env.any? { |e| e == {'name' => 'HSA_OVERRIDE_GFX_VERSION', 'value' => '12.0.1'} }, 'Compatibility overlay missing explicit override')

%w[parent kids].each do |owner|
  deployment = named(family, 'Deployment', "#{owner}-steam-headless")
  service = named(family, 'Service', "#{owner}-steam-headless")
  labels = deployment.dig('spec', 'template', 'metadata', 'labels')
  require_check(service.dig('spec', 'selector').all? { |key, value| labels[key] == value }, 'Gaming service cross-selects another profile')
  require_check(deployment.dig('spec', 'template', 'spec', 'volumes')[0].dig('persistentVolumeClaim', 'claimName') == "#{owner}-gaming-home", 'Gaming profiles share user PVCs')
  require_check(deployment.dig('spec', 'template', 'spec', 'containers')[0].dig('resources', 'limits') == {'cpu' => '12', 'memory' => '12Gi', 'amd.com/gpu' => '1'}, 'Steam resource envelope changed')
  guaranteed_pod(deployment.dig('spec', 'template', 'spec'))
  game = deployment.dig('spec', 'template', 'spec', 'containers')[0]
  pod_security = deployment.dig('spec', 'template', 'spec', 'securityContext')
  require_check(pod_security['runAsNonRoot'] == true && pod_security['runAsUser'] == 1000 && pod_security['runAsGroup'] == 1000 && pod_security['fsGroup'].nil? &&
                pod_security.dig('seccompProfile', 'type') == 'RuntimeDefault', 'Wayland gaming session must stay non-root with RuntimeDefault seccomp')
  require_check(game.dig('securityContext', 'allowPrivilegeEscalation') == false &&
                game.dig('securityContext', 'capabilities', 'drop') == ['ALL'] &&
                game.dig('securityContext', 'capabilities', 'add').nil?, 'Wayland gaming session must not require elevated Linux capabilities')
  require_check(game['image'] == 'localhost/workstation/steam-headless:UNQUALIFIED', 'Gaming must require promotion of the locally built recipe')
  require_check(deployment.dig('metadata', 'annotations', 'workstation.ai/qualification') != 'qualified', 'Streaming settings cannot qualify gaming')
  require_check(game['volumeMounts'].include?({'name' => 'user', 'mountPath' => '/home/default'}), 'Persistent home must match the maintained image')
  require_check(game['env'].include?({'name' => 'XDG_CACHE_HOME', 'value' => '/home/default/.cache'}) &&
                game['env'].include?({'name' => 'MESA_SHADER_CACHE_MAX_SIZE', 'value' => '2G'}), 'Shader cache must remain bounded and persistent')
  reference = game.fetch('envFrom')[0].fetch('configMapRef').fetch('name')
  require_check(reference.start_with?("#{owner}-sunshine-profile-"), 'Gaming profile ConfigMaps must remain independent')
  settings = named(family, 'ConfigMap', reference).fetch('data')
  require_check(settings == {'SUNSHINE_ENCODER' => 'vulkan', 'SUNSHINE_CAPTURE' => 'kwin', 'SUNSHINE_OUTPUT_NAME' => '',
                             'SUNSHINE_HEVC_MODE' => '0', 'SUNSHINE_AV1_MODE' => '0', 'SUNSHINE_VK_TUNE' => '2',
                             'SUNSHINE_VAAPI_STRICT_RC_BUFFER' => 'disabled', 'GAMING_WIDTH' => '1920', 'GAMING_HEIGHT' => '1080',
                             'GAMING_REFRESH_HZ' => '60'}, 'Streaming baseline changed without review')
  require_check(game['env'].none? { |env| settings.key?(env['name']) || env['name'] == 'AMD_DEBUG' }, 'Do not shadow profile settings or duplicate upstream lowlatencyenc')
end
kids_env = named(family, 'Deployment', 'kids-steam-headless').dig('spec', 'template', 'spec', 'containers')[0]['env']
require_check(kids_env.any? { |e| e['name'] == 'STEAM_ARGS' && e['value'] == '-tenfoot' }, 'Kids profile lost Big Picture mode')
require_check(named(family, 'Namespace', 'ai-home-lab').dig('metadata', 'labels', 'pod-security.kubernetes.io/enforce') == 'baseline', 'No implicit namespace-wide gaming exception')
if File.file?(File.join(directory, 'operator.yaml'))
  operator = documents(directory, 'operator')
  require_check(operator.none? { |d| d.dig('metadata', 'annotations', 'helm.sh/hook') }, 'Helm lifecycle hooks must not become ordinary manifest resources')
  require_check(operator.none? { |d| %w[Job DeviceConfig].include?(d['kind']) }, 'Do not render cleanup Jobs or a default DeviceConfig')
  controllers = operator.select { |d| %w[Deployment DaemonSet].include?(d['kind']) }
  require_check(controllers.none? { |d| d.dig('metadata', 'name').match?(/kmm|module-management/) }, 'Host driver must not be managed by KMM')
  require_check(controllers.any? { |d| d.dig('metadata', 'name').end_with?('node-feature-discovery-worker') }, 'NFD worker is missing')
  manager = controllers.find { |d| d.dig('metadata', 'name').end_with?('controller-manager') }
  require_check(!manager.nil?, 'GPU operator controller is missing')
  containers = manager.dig('spec', 'template', 'spec', 'containers')
  require_check(containers.any? { |c| c.fetch('env', []).include?({'name' => 'KMM_WATCH_ENABLED', 'value' => 'false'}) }, 'KMM watch must remain disabled')
  puts "#{operator.length} operator objects passed lifecycle and host-driver boundary checks"
end
puts 'Home-lab rendered workload semantics passed; runtime qualification remains pending'
