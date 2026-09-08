require 'yaml'
require 'json'

def check(ok, message)
  abort message unless ok
end

def gib(value)
  match = /\A([1-9][0-9]*)Gi\z/.match(value.to_s)
  abort("Expected GiB memory quantity, got #{value.inspect}") unless match
  match[1].to_i
end

root, baseline, candidate, dense_path = ARGV
base = YAML.load_stream(File.read(baseline)).compact
docs = YAML.load_stream(File.read(candidate)).compact
def named(docs, kind, name)
  docs.find { |d| d['kind'] == kind && d.dig('metadata', 'name') == name } || abort("Missing #{kind}/#{name}")
end
base.each do |original|
  next if original['kind'] == 'Deployment' && original.dig('metadata', 'name') == 'open-webui'
  check(named(docs, original['kind'], original.dig('metadata', 'name')) == original,
        "RAG changed unrelated #{original['kind']}/#{original.dig('metadata', 'name')}")
end
check(docs.none? { |d| d['kind'] == 'Secret' }, 'RAG must not render secrets')
check(docs.count { |d| d['kind'] == 'Deployment' } == base.count { |d| d['kind'] == 'Deployment' }, 'RAG added an unnecessary service')
deployment = named(docs, 'Deployment', 'open-webui')
check(deployment.dig('spec', 'replicas') == 0 && deployment.dig('spec', 'strategy', 'type') == 'Recreate', 'RAG must stay disabled and single-instance')
check(deployment.dig('metadata', 'annotations', 'ai-home-lab.local/qualification').start_with?('pending-rag-'), 'RAG qualification gate lost')
pod = deployment.dig('spec', 'template', 'spec')
check(pod['automountServiceAccountToken'] == false, 'RAG cannot receive cluster credentials')
check(pod['nodeSelector'] == {'kubernetes.io/arch' => 'amd64', 'kubernetes.io/os' => 'linux'}, 'Image platform is not explicit')
check(pod.dig('securityContext', 'runAsUser') == 1000 && pod.dig('securityContext', 'runAsGroup') == 1000 &&
      pod.dig('securityContext', 'runAsNonRoot') == true && pod.dig('securityContext', 'seccompProfile', 'type') == 'RuntimeDefault', 'RAG must stay non-root and sandboxed')
lock = File.readlines(File.join(root, 'versions.lock')).reject { |l| l.start_with?('#') || l.strip.empty? }.map { |l| l.strip.split('=', 2) }.to_h
image = lock.fetch('OPEN_WEBUI_IMAGE')
check(image.match?(/\Aghcr\.io\/open-webui\/open-webui:v#{Regexp.escape(lock.fetch('OPEN_WEBUI_VERSION'))}@sha256:[a-f0-9]{64}\z/), 'RAG image is not locked')
check(lock.fetch('OPEN_WEBUI_COMMIT').match?(/\A[a-f0-9]{40}\z/) && lock.fetch('RAG_EMBEDDING_REVISION').match?(/\A[a-f0-9]{40}\z/), 'Missing source/model revisions')
containers = pod.fetch('initContainers') + pod.fetch('containers')
containers.each do |c|
  check(c['image'] == image, 'RAG init/runtime images must match versions.lock')
  check(c.dig('securityContext', 'allowPrivilegeEscalation') == false && c.dig('securityContext', 'capabilities', 'drop') == ['ALL'], 'RAG gained privileges')
  check(!c.dig('resources', 'limits', 'amd.com/gpu'), 'CPU RAG must not consume GPU allocations')
  check(c.fetch('volumeMounts').any? { |m| m['name'] == 'rag-models' && m['readOnly'] == true }, 'RAG model inputs must be read-only')
  check(c.dig('envFrom', 0, 'configMapRef', 'name').start_with?('webui-rag-profile-'), 'Hashed profile reference not rewritten')
end
app = pod.fetch('containers')[0]
check(app['resources'] == {'requests' => {'cpu' => '2', 'memory' => '4Gi'}, 'limits' => {'cpu' => '2', 'memory' => '6Gi'}}, 'RAG memory/CPU envelope changed')
sglang = named(docs, 'Deployment', 'sglang').dig('spec', 'template', 'spec', 'containers').fetch(0)
sglang_limit = sglang.dig('resources', 'limits', 'memory')
check(sglang_limit == '38Gi', 'RAG requires the reviewed 38 GiB dual-GPU serving limit')
check(gib(sglang_limit) + gib(app.dig('resources', 'limits', 'memory')) <= 44,
      'RAG and serving limits exceed the 44 GiB workload budget after other pods')
env = app.fetch('env').to_h { |e| [e['name'], e['value']] }
{'WEBUI_AUTH' => 'true', 'ENABLE_SIGNUP' => 'false', 'OFFLINE_MODE' => 'true', 'HF_HUB_OFFLINE' => '1',
 'OPENAI_API_BASE_URL' => 'http://sglang:30000/v1'}.each { |key, value| check(env[key] == value, "Lost #{key}") }
profile = named(docs, 'ConfigMap', app.dig('envFrom', 0, 'configMapRef', 'name')).fetch('data')
query_prefix = "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:"
check((env.keys & profile.keys).empty?, 'Inline environment shadows the RAG profile')
containers.each do |container|
  inline = container.fetch('env').to_h { |entry| [entry['name'], entry['value']] }
  check(inline['RAG_EMBEDDING_QUERY_PREFIX'] == query_prefix, 'Qwen query prefix is not a native newline value')
end
check(profile['RAG_TEXT_SPLITTER'] == 'token_transformers' && profile['CHUNK_SIZE'] == '192' && profile['CHUNK_OVERLAP'] == '32', 'RAG tokenizer/chunk baseline changed')
check(profile['RAG_EMBEDDING_MODEL'] == '/rag-models/Qwen3-Embedding-0.6B' && profile['RAG_TOKENIZER_MODEL'] == profile['RAG_EMBEDDING_MODEL'] && profile['RAG_EMBEDDING_BATCH_SIZE'] == '1' && !profile.key?('RAG_EMBEDDING_CONTENT_PREFIX') && profile['RAG_RERANKING_MODEL'] == '', 'Qwen embedding profile changed')
check(profile['RAG_TOP_K'] == '3' && profile['RAG_TOP_K_RERANKER'] == '3', 'Both per-query and merged retrieval must retain the three-hit limit')
volumes = pod.fetch('volumes').to_h { |v| [v['name'], v] }
check(volumes['data'].dig('persistentVolumeClaim', 'claimName') == 'webui-rag-qwen3-data', 'Qwen RAG must not reuse MiniLM data')
check(volumes['rag-models'].dig('persistentVolumeClaim', 'claimName') == 'webui-rag-qwen3-models', 'Qwen model PVC is missing')
check(volumes.values.none? { |v| v['hostPath'] || v.dig('emptyDir', 'medium') == 'Memory' }, 'RAG must not gain host paths or RAM-backed scratch')
check(volumes['webui-static']['emptyDir'] == {'sizeLimit' => '64Mi'} && volumes['webui-tiktoken']['emptyDir'] == {'sizeLimit' => '4Mi'}, 'Offline startup assets must be bounded')
inputs = named(docs, 'ConfigMap', volumes['rag-inputs'].dig('configMap', 'name')).fetch('data')
%w[verify-models.sh validate-profile.sh SHA256SUMS].each do |file|
  check(inputs[file] == File.read(File.join(root, 'apps/overlays/rag', file)), "Generated #{file} differs from canonical source")
end
{'webui-rag-qwen3-data' => '20Gi', 'webui-rag-qwen3-models' => '3Gi'}.each do |name, size|
  pvc = named(docs, 'PersistentVolumeClaim', name)
  check(pvc.dig('spec', 'storageClassName') == 'local-path' && pvc.dig('spec', 'resources', 'requests', 'storage') == size, 'RAG storage budget changed')
  check(pvc.dig('metadata', 'annotations', 'ai-home-lab.local/capacity-note') == 'advisory-request-not-local-path-filesystem-quota', 'Do not promise local-path quotas')
end
questions = JSON.parse(File.read(File.join(root, 'tests/fixtures/rag/questions.json')))
check(questions.map { |q| q.fetch('id') }.uniq.length == questions.length, 'Duplicate evaluation IDs')
questions.each { |q| q.fetch('sources').each { |p| check(File.file?(File.join(root, p)), "Missing evaluation source #{p}") } }
dense = YAML.load_stream(File.read(dense_path)).compact
dense_pod = named(dense, 'Deployment', 'open-webui').dig('spec', 'template', 'spec')
dense_profile_name = dense_pod.dig('containers', 0, 'envFrom', 0, 'configMapRef', 'name')
dense_profile = named(dense, 'ConfigMap', dense_profile_name).fetch('data')
check(dense_profile == profile.merge('ENABLE_RAG_HYBRID_SEARCH' => 'false'), 'Dense comparison changed more than retrieval mode')
# Normalize only the generated profile reference; everything else must match.
dense_pod['initContainers'][0]['envFrom'] = pod['initContainers'][0]['envFrom']
dense_pod['containers'][0]['envFrom'] = pod['containers'][0]['envFrom']
check(dense_pod == pod, 'Dense comparison changed runtime resources, models or storage')
puts 'RAG rendered isolation, locks, resource limits and evaluation fixtures passed; no runtime qualification'
