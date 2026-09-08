"""Offline Jinja/TOML checks; never import inventory plugins or contact a host."""
import base64
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import tomllib

import jinja2
import yaml

root = Path(__file__).resolve().parents[2]
variables = yaml.safe_load((root / 'infrastructure/ansible/group_vars/ai_lab_baremetal.yml').read_text())
variables.update(
    inventory_hostname='synthetic-node', lab_node_ip='192.168.50.10',
    lab_management_cidr='192.168.50.0/24', lab_cluster_cidr='10.42.0.0/16',
    lab_service_cidr='10.43.0.0/16', lab_cluster_dns='10.43.0.10',
    lab_network_interface='synthetic0', lab_tls_sans=['synthetic.example.invalid'],
)
environment = jinja2.Environment(undefined=jinja2.StrictUndefined)
environment.filters['to_json'] = json.dumps
environment.filters['bool'] = lambda value: bool(value)
role = root / 'infrastructure/ansible/roles/k3s_baremetal'
config = yaml.safe_load(environment.from_string((role / 'templates/config.yaml.j2').read_text()).render(**variables))
assert config['write-kubeconfig-mode'] == '0600'
assert config['secrets-encryption'] is True
assert config['node-ip'] == variables['lab_node_ip']
assert config['data-dir'] == '/var/lib/rancher/k3s'
assert config['default-local-storage-path'] == '/var/lib/rancher/k3s/storage'
assert config['flannel-backend'] == 'vxlan'
assert config['flannel-iface'] == variables['lab_network_interface']
assert config['disable-network-policy'] is False
assert config['disable'] == ['servicelb']
assert config['nonroot-devices'] is True
assert not {'cni', 'ingress-controller', 'profile', 'cluster-init', 'datastore-endpoint'} & config.keys()
assert not any(key.startswith('etcd-') for key in config)
assert 'cgroup-driver=systemd' in config['kubelet-arg']
assert 'fail-swap-on=true' in config['kubelet-arg']
assert 'eviction-hard=memory.available<2Gi,nodefs.available<10%,imagefs.available<15%' in config['kubelet-arg']
for component in ('kubelet', 'kube-apiserver', 'kube-scheduler', 'kube-controller-manager'):
    assert 'feature-gates=DynamicResourceAllocation=true' in config[f'{component}-arg']
runtime = tomllib.loads((role / 'files/10-cdi.toml').read_text())['plugins']['io.containerd.cri.v1.runtime']
assert runtime['enable_cdi'] is True
assert runtime['cdi_spec_dirs'] == ['/etc/cdi', '/var/run/cdi']
assert runtime['containerd']['runtimes']['runc']['options']['SystemdCgroup'] is True

# CPUManager is an opt-in K3s v1.35 kubelet drop-in. The default leaves the
# pre-existing K3s kubelet configuration untouched.
assert variables['lab_cpu_manager_policy'] == 'none'
assert variables['lab_reserved_system_cpus'] == ''
cpu_template = role / 'templates/90-workstation-cpu.conf.j2'
cpu_vars = dict(variables, lab_cpu_manager_policy='static', lab_reserved_system_cpus='0,1,4,5,8,9', lab_cpu_manager_cache_alignment=True)
cpu_config = yaml.safe_load(environment.from_string(cpu_template.read_text()).render(**cpu_vars))
assert cpu_config['cpuManagerPolicy'] == 'static'
assert cpu_config['cpuManagerPolicyOptions'] == {
    'full-pcpus-only': 'true', 'strict-cpu-reservation': 'true',
    'prefer-align-cpus-by-uncorecache': 'true',
}
assert cpu_config['reservedSystemCPUs'] == '0,1,4,5,8,9'
assert cpu_config['topologyManagerPolicy'] == 'restricted'
assert cpu_config['topologyManagerScope'] == 'pod'
assert cpu_config['featureGates'] == {'CPUManagerPolicyBetaOptions': True}
cpu_config_without_alignment = yaml.safe_load(environment.from_string(cpu_template.read_text()).render(**dict(cpu_vars, lab_cpu_manager_cache_alignment=False)))
assert cpu_config_without_alignment['cpuManagerPolicyOptions'] == {'full-pcpus-only': 'true', 'strict-cpu-reservation': 'true'}
assert 'featureGates' not in cpu_config_without_alignment

cpu_topology = [
    {'cpu': 0, 'thread_siblings': [0, 1]}, {'cpu': 1, 'thread_siblings': [0, 1]},
    {'cpu': 4, 'thread_siblings': [4, 5]}, {'cpu': 5, 'thread_siblings': [4, 5]},
]
def is_full_smt_reservation(reserved):
    online = {item['cpu'] for item in cpu_topology}
    return bool(reserved) and reserved <= online and all(
        set(item['thread_siblings']) <= reserved for item in cpu_topology if item['cpu'] in reserved
    )
assert is_full_smt_reservation({0, 1, 4, 5})
assert not is_full_smt_reservation({0, 4, 5})
assert not is_full_smt_reservation({0, 1, 9})

# The separate VM keeps etcd snapshots and an explicitly configured PSA policy.
vm_role = root / 'ansible/roles/k3s'
vm_vars = yaml.safe_load((vm_role / 'defaults/main.yml').read_text())
vm_vars.update(
    k3s_bind_address='192.168.124.10', k3s_advertise_address='192.168.124.10',
    k3s_tls_sans=['synthetic.example.invalid'], k3s_cluster_dns='10.43.0.10',
)
vm_config = yaml.safe_load(environment.from_string((vm_role / 'templates/config.yaml.j2').read_text()).render(**vm_vars))
assert vm_config['cluster-init'] is True
assert vm_config['selinux'] is True and vm_config['secrets-encryption'] is True
assert vm_config['write-kubeconfig-mode'] == '0600'
assert vm_config['disable'] == ['local-storage']
assert vm_config['disable-network-policy'] is False
assert vm_config['flannel-iface'] == 'mgmt0'
assert vm_config['etcd-snapshot-retention'] == 14
assert not {'cni', 'ingress-controller', 'profile'} & vm_config.keys()
assert 'admission-control-config-file=/etc/rancher/k3s/pod-security.yaml' in vm_config['kube-apiserver-arg']
admission = yaml.safe_load((vm_role / 'files/pod-security.yaml').read_text())
policy = admission['plugins'][0]['configuration']
assert policy['defaults']['enforce'] == 'restricted'
assert policy['exemptions'] == {'usernames': [], 'runtimeClasses': [], 'namespaces': ['kube-system']}

for role_path, register in ((role, 'lab_previous_rke2'), (vm_role, 'k3s_previous_rke2')):
    role_tasks = yaml.safe_load((role_path / 'tasks/main.yml').read_text())
    gate = next(task for task in role_tasks if task['name'].startswith('Refuse to convert'))
    expression = gate['ansible.builtin.assert']['that'][0]
    condition = environment.compile_expression(expression)
    assert condition(**{register: {'stat': {'exists': False}}})
    assert not condition(**{register: {'stat': {'exists': True}}})

cpu_tasks = yaml.safe_load((role / 'tasks/main.yml').read_text())
cpu_dropin_tasks = [task for task in cpu_tasks if 'CPUManager' in task['name'] and 'kubelet drop-in' in task['name']]
assert cpu_dropin_tasks and all(task['when'] == 'lab_cpu_manager_static_enabled | bool' for task in cpu_dropin_tasks)
cpu_reservations = next(task for task in cpu_tasks if task['name'] == 'Require CPU and memory reservations to match the static CPU reservation')
assert cpu_reservations['ansible.builtin.assert']['fail_msg'].startswith('The resource plan must reserve')
cpu_gate = next(task for task in cpu_tasks if task['name'] == 'Refuse implicit static CPUManager migration')
environment.filters['b64decode'] = lambda value: base64.b64decode(value).decode()
cpu_conditions = [environment.compile_expression(expression) for expression in cpu_gate['ansible.builtin.assert']['that']]
cpu_evidence = dict(
    lab_cpu_manager_dropin={'stat': {'exists': True}},
    lab_cpu_manager_checkpoint={'stat': {'exists': True}},
    lab_cpu_manager_existing={'content': base64.b64encode(environment.from_string(cpu_template.read_text()).render(**cpu_vars).encode()).decode()},
    lab_cpu_manager_desired_config=environment.from_string(cpu_template.read_text()).render(**cpu_vars),
    lab_cpu_manager_k3s_active={'rc': 0},
)
assert all(condition(**cpu_evidence) for condition in cpu_conditions)
for invalid_case in ('changed-policy-with-state', 'running-without-managed-config'):
    invalid = copy.deepcopy(cpu_evidence)
    if invalid_case == 'changed-policy-with-state':
        invalid['lab_cpu_manager_existing']['content'] = base64.b64encode(b'cpuManagerPolicy: none\n').decode()
    else:
        invalid['lab_cpu_manager_dropin']['stat']['exists'] = False
    assert not all(condition(**invalid) for condition in cpu_conditions), invalid_case

none_gate = next(task for task in cpu_tasks if task['name'] == 'Refuse to silently retain a managed static CPUManager configuration')
assert none_gate['when'] == 'not (lab_cpu_manager_static_enabled | bool)'
none_condition = environment.compile_expression(none_gate['ansible.builtin.assert']['that'][0])
assert none_condition(lab_cpu_manager_dropin={'stat': {'exists': False}})
assert not none_condition(lab_cpu_manager_dropin={'stat': {'exists': True}})

cpu_validation = next(task for task in cpu_tasks if task['name'] == 'Validate static CPU reservation against observed full SMT sibling groups')
cpu_validation_argv = cpu_validation['ansible.builtin.command']['argv']
assert cpu_validation_argv[:3] == ['bash', '-c', cpu_validation_argv[2]]
assert cpu_validation_argv[3] == '_' and cpu_validation_argv[4] == '{{ lab_hardware_report }}'
cpu_report = {
    'cpu_topology': {
        'online_cpus': [0, 1, 4, 5],
        'cpus': [
            {'cpu': 0, 'core_id': 0, 'socket_id': 0, 'thread_siblings': [0, 1]},
            {'cpu': 1, 'core_id': 0, 'socket_id': 0, 'thread_siblings': [0, 1]},
            {'cpu': 4, 'core_id': 1, 'socket_id': 0, 'thread_siblings': [4, 5]},
            {'cpu': 5, 'core_id': 1, 'socket_id': 0, 'thread_siblings': [4, 5]},
        ],
    },
}
with tempfile.TemporaryDirectory() as temporary_directory:
    cpu_report_path = Path(temporary_directory) / 'hardware.json'
    cpu_report_path.write_text(json.dumps(cpu_report))
    def validate_reservation(cpuset, report=cpu_report_path):
        return subprocess.run(
            [*cpu_validation_argv[:3], '_', str(report), cpuset],
            text=True, capture_output=True, check=False,
        )
    assert validate_reservation('0,1').returncode == 0
    assert validate_reservation('0,1,4,4').returncode != 0
    assert validate_reservation('0,1,4,5').returncode != 0
    asymmetric_report = copy.deepcopy(cpu_report)
    asymmetric_report['cpu_topology']['cpus'][0]['thread_siblings'] = [0]
    asymmetric_path = Path(temporary_directory) / 'asymmetric.json'
    asymmetric_path.write_text(json.dumps(asymmetric_report))
    assert validate_reservation('0,1', asymmetric_path).returncode != 0
    missing_cpu_report = copy.deepcopy(cpu_report)
    missing_cpu_report['cpu_topology']['online_cpus'].append(6)
    missing_cpu_path = Path(temporary_directory) / 'missing.json'
    missing_cpu_path.write_text(json.dumps(missing_cpu_report))
    assert validate_reservation('0,1', missing_cpu_path).returncode != 0

# Exercise the gate's actual expressions without importing Ansible or inventory.
tasks = yaml.safe_load((root / 'infrastructure/ansible/roles/arch_host/tasks/main.yml').read_text())
gate = next(task for task in tasks if task['name'] == 'Require both detected amdgpu devices and the expected ROCm target')
environment.filters['b64decode'] = lambda value: base64.b64decode(value).decode()
conditions = [environment.compile_expression(expression) for expression in gate['ansible.builtin.assert']['that']]
report = {
    'schema': 1, 'status': 'observed', 'os': 'Linux', 'architecture': 'x86_64',
    'gpu_target': 'gfx1201',
    'pci_gpus': [
        {'bdf': '0000:01:00.0', 'device_id': 'synthetic', 'driver': 'amdgpu'},
        {'bdf': '0000:02:00.0', 'device_id': 'synthetic', 'driver': 'amdgpu'},
    ],
    'rocm_agents': [{'agent': '1', 'gfx': 'gfx1201'}, {'agent': '2', 'gfx': 'gfx1201'}],
}
evidence = dict(
    variables, report=report,
    lab_observation_boot={'content': base64.b64encode(b'synthetic-boot\n').decode()},
    lab_current_boot={'stdout': 'synthetic-boot'},
)
assert all(condition(**evidence) for condition in conditions)
for invalid_case in ('stale-boot', 'one-gpu', 'duplicate-gpu', 'wrong-gfx'):
    invalid = copy.deepcopy(evidence)
    if invalid_case == 'stale-boot':
        invalid['lab_current_boot']['stdout'] = 'different-boot'
    elif invalid_case == 'one-gpu':
        invalid['report']['pci_gpus'].pop()
    elif invalid_case == 'duplicate-gpu':
        invalid['report']['pci_gpus'][1]['bdf'] = '0000:01:00.0'
    else:
        invalid['report']['rocm_agents'][1]['gfx'] = 'gfx0000'
    assert not all(condition(**invalid) for condition in conditions), invalid_case
print('K3s SQLite/etcd configuration, Pod Security, migration refusal, CDI and hardware checks passed')
