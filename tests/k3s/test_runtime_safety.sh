#!/usr/bin/env bash
# Offline fixtures only. No guest, firewall, credential or Restic operation runs.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
source "$root/lib/k3s/common.sh"
source "$root/lib/k3s/lifecycle.sh"
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT

fingerprint=1111111111111111111111111111111111111111
valid_records="pub:-:4096:1:TEST:0:0::-:::sc::::::23::0:
fpr:::::::::$fingerprint:
sub:-:4096:1:SUBKEY:0:0:::::e:
fpr:::::::::2222222222222222222222222222222222222222:"
k3s_gpg_primary_matches "$fingerprint" <<<"$valid_records"
if k3s_gpg_primary_matches 3333333333333333333333333333333333333333 <<<"$valid_records"; then exit 1; fi
if k3s_gpg_primary_matches 2222222222222222222222222222222222222222 <<<"$valid_records"; then exit 1; fi
if k3s_gpg_primary_matches "$fingerprint" <<<"$valid_records
pub:-:4096:1:FOREIGN:0:0::-:::sc:
fpr:::::::::3333333333333333333333333333333333333333:"; then exit 1; fi
if k3s_gpg_primary_matches "$fingerprint" <<<"pub:::::::::
:fpr:::::::::$fingerprint:"; then exit 1; fi

[[ $(k3s_service_dns 10.43.0.0/16) == 10.43.0.10 ]]
[[ $(k3s_service_dns 10.80.16.0/20) == 10.80.16.10 ]]
if k3s_service_dns 10.80.16.0/29 >/dev/null; then exit 1; fi
if k3s_service_dns 999.80.16.0/20 >/dev/null; then exit 1; fi

# Check Ansible ordering and immutable metadata using the repository's existing
# Ruby YAML parser. This never loads an inventory or an Ansible connection plugin.
if command -v ruby >/dev/null; then
  ruby -ryaml - "$root" <<'RUBY'
root = ARGV.fetch(0)
tasks = YAML.load_file("#{root}/ansible/roles/k3s/tasks/main.yml")
index = ->(name) { tasks.index { |t| t['name'] == name } || abort("missing task: #{name}") }
key = index.call('Assert locked K3s RPM signing fingerprint')
apply = index.call('Apply the owned firewall table atomically before disabling firewalld')
verify = index.call('Verify the replacement firewall exists before disabling firewalld')
stop = index.call('Stop and disable installed firewalld after the replacement is active')
server = index.call('Install pinned K3s server')
abort 'unsafe firewall handover ordering' unless key < apply && apply < verify && verify < stop && stop < server
abort 'unexpected global firewall restart' if File.read("#{root}/ansible/roles/k3s/handlers/main.yml").include?('Restart nftables')
abort 'global firewall config must not be changed' if File.read("#{root}/ansible/roles/k3s/tasks/main.yml").include?('/etc/sysconfig/nftables.conf')
unit = File.read("#{root}/ansible/roles/k3s/files/k3s-lab-firewall.service")
abort 'stopping the owned unit must retain rules' if unit.lines.any? { |line| line.start_with?('ExecStop=') }
abort 'unit must load only its own table' unless unit.include?('ExecStart=/usr/sbin/nft -f /etc/nftables/k3s-lab.nft')
dependency = tasks.find { |task| task['name'] == 'Require the firewall before network configuration and SSH startup' }
abort 'network and SSH must fail closed when the boot firewall fails' unless dependency && dependency['loop'] == ['NetworkManager', 'sshd']
%w[Requires After].each do |directive|
  abort 'missing firewall dependency' unless dependency['ansible.builtin.copy']['content'].include?("#{directive}=k3s-lab-firewall.service\n")
end
abort 'firewall must not wait for network startup' if unit.lines.any? { |line| line.start_with?('After=') && line.match?(/network|NetworkManager|sshd/) }
# Structural regression: replacing only rules retains named-set elements. Both
# first install and CIDR changes must delete/recreate the complete owned table
# inside the same nft -f batch; never issue a separate delete or global flush.
policy = File.read("#{root}/ansible/roles/k3s/templates/k3s-lab.nft.j2")
commands = policy.lines.map(&:strip).reject { |line| line.empty? || line.start_with?('#') }
expected_prefix = ['add table inet k3s_lab_guard', 'delete table inet k3s_lab_guard', 'table inet k3s_lab_guard {']
abort 'owned named sets must be replaced atomically' unless commands.take(3) == expected_prefix
abort 'global flush is forbidden' if commands.any? { |line| line.start_with?('flush ruleset') }
%w[192.168.124.0/24 192.168.125.0/24].each do |cidr|
  rendered = policy.gsub('{{ k3s_management_cidr }}', cidr)
  elements = rendered.scan(/set management_v4 \{[^\n]*elements = \{ ([^}]+) \}/).flatten
  abort 'management set must contain exactly the desired CIDR after replacement' unless elements == [cidr]
end
snapshot = tasks.find { |t| t['name'] == 'Save compressed etcd snapshot for lifecycle operation' }
abort 'K3s snapshot executable mismatch' unless snapshot['ansible.builtin.command']['cmd'].start_with?('/usr/local/bin/k3s ')
restore = YAML.load_file("#{root}/ansible/roles/k3s/tasks/restore.yml")
reset = restore.find { |t| t['name'] == 'Run bounded K3s cluster reset restore on detached clone' }
abort 'K3s restore executable mismatch' unless reset['ansible.builtin.command']['argv'][0] == '/usr/local/bin/k3s'
condition = restore[0]['ansible.builtin.assert']['that'].last
abort 'incorrect Python regex in restore gate' unless condition == "k3s_restore_snapshot is match('^/\\\\S+\\\\Z')"
%w[cert-manager local-path].each do |component|
  metadata = YAML.load_file("#{root}/kubernetes/#{component}/install-metadata.yaml")
  lock = metadata.fetch('data')
  expected_suffix = "#{lock.fetch('version').tr('.', '-')}-#{lock.fetch('manifest-sha256')[0,12]}"
  abort 'metadata name is not versioned' unless metadata['metadata']['name'].end_with?(expected_suffix)
  abort 'metadata must remain immutable' unless metadata['immutable'] == true
end
RUBY
fi

# Mocks serve synthetic qcow2 inventories; no qemu-img binary is executed.
VM_POOL_DIR="$work/pool"
LAB_NAME=synthetic-k3s
MGMT_NETWORK_NAME=synthetic-mgmt
K3S_LOCK_SOURCE="$root/versions.lock"
mkdir -p "$VM_POOL_DIR/base" "$VM_POOL_DIR/cloud-init" "$VM_POOL_DIR/generated"
base_name=AlmaLinux-9-GenericCloud-synthetic.x86_64.qcow2
for file in system.qcow2 data.qcow2 cloud-init.iso create.conf ansible-vars.yml inventory.ini "base/$base_name"; do
  printf 'synthetic fixture\n' >"$VM_POOL_DIR/$file"
done
# The macOS sha256sum alias need not implement GNU long flags. This adapter
# still computes and verifies real fixture hashes, never a canned digest.
sha256sum() {
  if [[ ${1:-} == --check ]]; then shasum -a 256 --check --status; else shasum -a 256 "$@"; fi
}
base_hash=$(sha256sum "$VM_POOL_DIR/base/$base_name" | awk '{print $1}')
printf 'ALMALINUX_IMAGE_NAME=%s\nALMALINUX_IMAGE_SHA256=%s\n' "$base_name" "$base_hash" >"$VM_POOL_DIR/create.lock"
qemu-img() {
  [[ ${1:-} != --version ]] || {
    printf 'synthetic qemu\n'
    return
  }
  [[ $1 == info && $2 == --backing-chain && $3 == --output=json ]] || return 90
  local disk=$4 base="$VM_POOL_DIR/base/$base_name"
  [[ ${chain_case:-valid} != foreign ]] || base=/unapproved/backing.qcow2
  if [[ $disk == "$VM_POOL_DIR/system.qcow2" ]]; then
    jq -n --arg disk "$disk" --arg base "$base" '[{filename:$disk,format:"qcow2","backing-filename":$base,"full-backing-filename":$base},{filename:$base,format:"qcow2"}]'
  else
    jq -n --arg disk "$disk" '[{filename:$disk,format:"qcow2"}]'
  fi
}
k3s_backup_check_chain "$VM_POOL_DIR/system.qcow2" "$VM_POOL_DIR/base/$base_name"
k3s_backup_check_chain "$VM_POOL_DIR/data.qcow2"
if (
  chain_case=foreign
  k3s_backup_check_chain "$VM_POOL_DIR/system.qcow2" "$VM_POOL_DIR/base/$base_name"
) 2>/dev/null; then exit 1; fi

if command -v xmllint >/dev/null; then
  # Firmware paths are metadata fixtures. Only this file-validation mock sees
  # them; no host firmware/NVRAM is opened or copied by the test.
  k3s_backup_regular_file() {
    case $1 in
      /usr/share/edk2/x64/OVMF_CODE.fd | /usr/share/edk2/x64/OVMF_VARS.fd | /var/lib/libvirt/qemu/nvram/synthetic-k3s_VARS.fd) return 0 ;;
      *) [[ -f $1 && ! -L $1 ]] || k3s_die 'missing synthetic backup file' ;;
    esac
  }
  readlink() {
    [[ $1 == -f && $2 == -- && $3 == /usr/share/edk2/* ]] || return 91
    printf '%s\n' "$3"
  }
  virsh() {
    case " $* " in
      *' --version '*) printf 'synthetic libvirt\n' ;;
      *' dumpxml --inactive synthetic-k3s '*)
        printf '<domain><name>synthetic-k3s</name><os><loader>/usr/share/edk2/x64/OVMF_CODE.fd</loader><nvram template="/usr/share/edk2/x64/OVMF_VARS.fd">/var/lib/libvirt/qemu/nvram/synthetic-k3s_VARS.fd</nvram></os><devices>'
        printf '<disk type="file" device="disk"><source file="%s/system.qcow2"/></disk><disk type="file" device="disk"><source file="%s/data.qcow2"/></disk>' "$VM_POOL_DIR" "$VM_POOL_DIR"
        [[ ${seed_attached:-yes} != yes ]] || printf '<disk type="file" device="cdrom"><source file="%s/cloud-init.iso"/></disk>' "$VM_POOL_DIR"
        [[ ${foreign_device:-no} != yes ]] || printf '<hostdev/>'
        printf '</devices></domain>\n'
        ;;
      *' net-dumpxml synthetic-mgmt '*) printf '<network><name>synthetic-mgmt</name></network>\n' ;;
      *) return 92 ;;
    esac
  }
  k3s_backup_prepare
  printf '%s\n' "${K3S_BACKUP_INPUTS[@]}" | grep -Fxq "$VM_POOL_DIR/base/$base_name"
  printf '%s\n' "${K3S_BACKUP_INPUTS[@]}" | grep -Fxq '/var/lib/libvirt/qemu/nvram/synthetic-k3s_VARS.fd'
  printf '%s\n' "${K3S_BACKUP_INPUTS[@]}" | grep -Fxq '/usr/share/edk2/x64/OVMF_CODE.fd'
  printf '%s\n' "${K3S_BACKUP_INPUTS[@]}" | grep -Fxq "$VM_POOL_DIR/cloud-init.iso"
  seed_attached=no
  k3s_backup_prepare
  if printf '%s\n' "${K3S_BACKUP_INPUTS[@]}" | grep -Fxq "$VM_POOL_DIR/cloud-init.iso"; then exit 1; fi
  if (
    foreign_device=yes
    k3s_backup_prepare
  ) 2>/dev/null; then exit 1; fi
  printf 'changed backing fixture\n' >"$VM_POOL_DIR/base/$base_name"
  if (k3s_backup_prepare) 2>/dev/null; then exit 1; fi
else
  echo 'SKIP: full synthetic backup XML checks require xmllint (libxml2)'
fi
echo 'K3s fingerprint, DNS, firewall ordering, runtime paths and self-contained backup tests passed'
