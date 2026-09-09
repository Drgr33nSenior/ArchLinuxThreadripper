#!/usr/bin/env bash
set -euo pipefail

repo_root=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
temp_dir=$(mktemp -d)
trap 'rm -rf -- "$temp_dir"' EXIT

private_key="$temp_dir/id_ed25519"
host_key="$temp_dir/ssh_host_ed25519_key"
known_hosts="$temp_dir/known_hosts.k3s"
ssh-keygen -q -t ed25519 -N '' -C developer@test -f "$private_key" </dev/null
ssh-keygen -q -t ed25519 -N '' -C host@test -f "$host_key" </dev/null
key_file="$private_key.pub"
awk -v host=192.168.124.10 '{ print host, $1, $2 }' "$host_key.pub" >"$known_hosts"
config_file="$temp_dir/k3s.conf"
cat >"$config_file" <<EOF
LAB_NAME=k3s-dev-01
ADMIN_USER=developer
SSH_PUBLIC_KEY_FILE=$key_file
SSH_PRIVATE_KEY_FILE=$private_key
SSH_KNOWN_HOSTS_FILE=$known_hosts
VM_POOL_DIR=/var/lib/libvirt/images/k3s-dev-01
MGMT_NETWORK_NAME=k3s-mgmt
MGMT_BRIDGE=virbr124
MGMT_CIDR=192.168.124.0/24
MGMT_GATEWAY=192.168.124.1
MGMT_IP=192.168.124.10
MGMT_FQDN=k3s-dev-01.mgmt.example.test
MGMT_MAC=52:54:00:00:01:10
DMZ_PARENT=enp1s0
DMZ_VLAN_ID=100
DMZ_VLAN_INTERFACE=k3sdmzvlan
DMZ_BRIDGE=br-k3s-dmz
DMZ_IPV4=198.51.100.10/24
DMZ_IPV4_GATEWAY=198.51.100.1
DMZ_IPV6=2001:db8:100::10/64
DMZ_IPV6_GATEWAY=2001:db8:100::1
DMZ_MAC=52:54:00:00:02:10
MGMT_INTERFACE=mgmt0
DMZ_INTERFACE=dmz0
CLUSTER_CIDR=10.42.0.0/16
SERVICE_CIDR=10.43.0.0/16
DOMAIN=lab.example.test
ACME_EMAIL=admin@example.test
ROUTE53_ZONE_ID=Z0123456789ABCDEF
AWS_REGION=eu-west-2
RESTIC_BUCKET=k3s-backup-example
EOF

bash -n "$repo_root/bin/k3s-lab"
bash -n "$repo_root/lib/k3s/common.sh"
bash -n "$repo_root/lib/k3s/lifecycle.sh"
bash -n "$repo_root/kubernetes/install-pinned-addons.sh"

# shellcheck source=/dev/null
source "$repo_root/lib/k3s/common.sh"
# shellcheck source=/dev/null
source "$repo_root/lib/k3s/lifecycle.sh"
k3s_load_config "$config_file" "$repo_root/versions.lock"
[ "$KWIN_DEBIAN_VERSION" = '4:6.3.6-1' ]
# Repeated loads clear the new lock key; environment values cannot shadow it.
KWIN_DEBIAN_VERSION=invalid
k3s_load_config "$config_file" "$repo_root/versions.lock"
[ "$KWIN_DEBIAN_VERSION" = '4:6.3.6-1' ]
[ "$K3S_VERSION" = 'v1.35.8+k3s1' ]
[ "$K3S_SELINUX_RPM_VERSION" = '1.6-1.el9' ]
[ "$VM_VCPUS" = 8 ]
k3s_require_ansible_access
saved_known_hosts=$SSH_KNOWN_HOSTS_FILE
SSH_KNOWN_HOSTS_FILE="$temp_dir/missing-known-hosts"
if (k3s_require_ansible_access) >/dev/null 2>&1; then
  echo 'missing dedicated known-hosts file was accepted' >&2
  exit 1
fi
SSH_KNOWN_HOSTS_FILE=$saved_known_hosts
if (k3s_down unexpected) >/dev/null 2>&1; then
  echo 'real down function accepted an unexpected argument' >&2
  exit 1
fi

render_dir="$temp_dir/rendered"
mkdir -p "$render_dir"
for template in \
  "$repo_root/cloud-init/user-data.yaml.tmpl" \
  "$repo_root/cloud-init/meta-data.tmpl" \
  "$repo_root/cloud-init/network-config.yaml.tmpl" \
  "$repo_root/kubernetes/cert-manager/templates/route53-cluster-issuer.yaml.tmpl"; do
  output="$render_dir/$(basename "${template%.tmpl}")"
  k3s_render_template "$template" "$output"
  if grep -Eq '__[A-Z][A-Z0-9_]*__' "$output"; then
    echo "unrendered placeholder remained in $output" >&2
    exit 1
  fi
done
k3s_write_ansible_vars "$render_dir/ansible-vars.yml"
k3s_write_inventory "$render_dir/inventory.ini"
grep -Fq 'hostedZoneID: "Z0123456789ABCDEF"' "$render_dir/route53-cluster-issuer.yaml"
grep -Fq 'k3s_admin_user: "developer"' "$render_dir/ansible-vars.yml"
grep -Fq 'k3s_cluster_dns: "10.43.0.10"' "$render_dir/ansible-vars.yml"
grep -Fq 'ansible_host=192.168.124.10' "$render_dir/inventory.ini"
grep -Fq 'ansible_ssh_private_key_file=' "$render_dir/inventory.ini"
grep -Fq 'StrictHostKeyChecking=yes' "$render_dir/inventory.ini"
if command -v ruby >/dev/null 2>&1; then
  ruby -ryaml -e 'ARGV.each { |path| YAML.load_stream(File.read(path)) }' \
    "$render_dir/user-data.yaml" "$render_dir/meta-data" \
    "$render_dir/network-config.yaml" "$render_dir/route53-cluster-issuer.yaml" \
    "$render_dir/ansible-vars.yml"
fi

# Check mode must remain genuinely offline. In particular, it must not invoke
# kubectl apply, because even client dry-run can perform REST discovery through
# an ambient kubeconfig.
offline_bin="$temp_dir/offline-bin"
offline_log="$temp_dir/kubectl.log"
mkdir -p "$offline_bin"
cat >"$offline_bin/kubectl" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$OFFLINE_KUBECTL_LOG"
[[ ${1:-} == kustomize ]] || exit 90
printf '%s\n' 'apiVersion: networking.k8s.io/v1' 'kind: NetworkPolicy' \
  'reclaimPolicy: Retain' \
  'image: docker.io/library/busybox:1.37.0@sha256:9db7b59979c38555a39def84a31fb98b5296952f9e3afd4f6f11f05b07adfab0'
EOF
cat >"$offline_bin/curl" <<'EOF'
#!/usr/bin/env bash
output=''; url=''
while (($#)); do
  case "$1" in
    -o) output=$2; shift 2 ;;
    http*) url=$1; shift ;;
    *) shift ;;
  esac
done
case "$url" in
  *cert-manager*) printf '%s\n' 'kind: CustomResourceDefinition' 'name: cert-manager' > "$output" ;;
  *local-path*) printf '%s\n' 'kind: Deployment' 'name: local-path-provisioner' > "$output" ;;
  *) exit 91 ;;
esac
EOF
cat >"$offline_bin/sha256sum" <<'EOF'
#!/usr/bin/env bash
case "$1" in
  *cert-manager*) printf '%s  %s\n' '5f6a499b8c1857d57f560f536e0dcc830914b45c420899fe7ad0692c8624e408' "$1" ;;
  *local-path*) printf '%s  %s\n' '9781b39c24f3f651bd6d6e41b561e04e4904bbdb6d4f8c7a6009df3a702dcd65' "$1" ;;
  *) exit 92 ;;
esac
EOF
chmod 0755 "$offline_bin/kubectl" "$offline_bin/curl" "$offline_bin/sha256sum"
OFFLINE_KUBECTL_LOG="$offline_log" PATH="$offline_bin:/usr/bin:/bin" \
  "$repo_root/kubernetes/install-pinned-addons.sh" check >/dev/null
grep -q '^kustomize ' "$offline_log"
if grep -q 'apply\|config\|cluster' "$offline_log"; then
  echo 'offline add-on check attempted a cluster-facing kubectl operation' >&2
  exit 1
fi

# Apply mode is also exercised with a fully local kubectl stub. The privileged
# system namespace must be the first apply. All local-path resources use one
# authoritative field manager and one merged desired manifest so a second
# apply cannot conflict with an override manager.
cat >"$offline_bin/kubectl" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$OFFLINE_KUBECTL_LOG"
case " $* " in
  *' config current-context '*) printf '%s\n' k3s-test ;;
  *' kustomize '*) printf '%s\n' 'apiVersion: networking.k8s.io/v1' 'kind: NetworkPolicy' 'reclaimPolicy: Retain' 'image: docker.io/library/busybox:1.37.0@sha256:9db7b59979c38555a39def84a31fb98b5296952f9e3afd4f6f11f05b07adfab0' ;;
  *' apply '*) : ;;
  *) exit 93 ;;
esac
EOF
chmod 0755 "$offline_bin/kubectl"
printf '%s\n' 'apiVersion: v1' >"$temp_dir/empty-kubeconfig"
: >"$offline_log"
OFFLINE_KUBECTL_LOG="$offline_log" PATH="$offline_bin:/usr/bin:/bin" \
  "$repo_root/kubernetes/install-pinned-addons.sh" apply \
  --kubeconfig "$temp_dir/empty-kubeconfig" --context k3s-test \
  --confirm-context k3s-test >/dev/null
OFFLINE_KUBECTL_LOG="$offline_log" PATH="$offline_bin:/usr/bin:/bin" \
  "$repo_root/kubernetes/install-pinned-addons.sh" apply \
  --kubeconfig "$temp_dir/empty-kubeconfig" --context k3s-test \
  --confirm-context k3s-test >/dev/null
first_apply=$(grep ' apply ' "$offline_log" | head -n1)
[[ "$first_apply" == *'field-manager=k3s-lab-local-path'*'/local-path/namespace.yaml'* ]] ||
  {
    echo 'local-path privileged namespace was not the first apply' >&2
    exit 1
  }
if grep -q 'field-manager=k3s-lab-local-path-override' "$offline_log"; then
  echo 'local-path apply still uses a competing override field manager' >&2
  exit 1
fi
[ "$(grep -c 'field-manager=k3s-lab-local-path ' "$offline_log")" -eq 6 ]
[ "$(grep -c 'field-manager=k3s-lab-local-path .*local-path-merged.yaml' "$offline_log")" -eq 2 ]

bad_config="$temp_dir/bad-pool.conf"
sed 's|^VM_POOL_DIR=.*|VM_POOL_DIR=/tmp/wrong-target|' "$config_file" >"$bad_config"
if (k3s_load_config "$bad_config" "$repo_root/versions.lock") >/dev/null 2>&1; then
  echo 'unsafe VM pool was accepted' >&2
  exit 1
fi

overlap_config="$temp_dir/overlap.conf"
sed 's|^DMZ_IPV4=.*|DMZ_IPV4=192.168.124.20/24|; s|^DMZ_IPV4_GATEWAY=.*|DMZ_IPV4_GATEWAY=192.168.124.2|' "$config_file" >"$overlap_config"
if (k3s_load_config "$overlap_config" "$repo_root/versions.lock") >/dev/null 2>&1; then
  echo 'overlapping management and DMZ CIDRs were accepted' >&2
  exit 1
fi

dhcp_config="$temp_dir/dhcp-collision.conf"
sed 's|^MGMT_IP=.*|MGMT_IP=192.168.124.150|' "$config_file" >"$dhcp_config"
if (k3s_load_config "$dhcp_config" "$repo_root/versions.lock") >/dev/null 2>&1; then
  echo 'management IP inside the libvirt DHCP range was accepted' >&2
  exit 1
fi

unsafe_lock="$temp_dir/unsafe-image.lock"
sed 's|^ALMALINUX_IMAGE_NAME=.*|ALMALINUX_IMAGE_NAME=../../escape.qcow2|' "$repo_root/versions.lock" >"$unsafe_lock"
if (k3s_load_config "$config_file" "$unsafe_lock") >/dev/null 2>&1; then
  echo 'unsafe AlmaLinux image filename was accepted' >&2
  exit 1
fi
"$repo_root/bin/k3s-lab" --config "$config_file" --dry-run upgrade "$K3S_VERSION" | grep -q 'snapshot, cold-backup, and upgrade'
"$repo_root/bin/k3s-lab" --config "$config_file" --dry-run status | grep -q 'development VM k3s-dev-01 only'
"$repo_root/bin/k3s-lab" --config "$config_file" --dry-run down --skip-etcd-snapshot | grep -q 'without a new etcd snapshot'
if "$repo_root/bin/k3s-lab" --config "$config_file" --dry-run status --skip-etcd-snapshot >/dev/null 2>&1; then
  echo 'down-only skip-etcd-snapshot flag was accepted by status' >&2
  exit 1
fi
for zero_argument_operation in network-create create provision backup-init; do
  if "$repo_root/bin/k3s-lab" --config "$config_file" --dry-run "$zero_argument_operation" unexpected >/dev/null 2>&1; then
    echo "dry-run $zero_argument_operation accepted an unexpected argument" >&2
    exit 1
  fi
done
if "$repo_root/bin/k3s-lab" --config "$config_file" --dry-run restore-test relative-snapshot >/dev/null 2>&1; then
  echo 'dry-run restore-test accepted a relative snapshot path' >&2
  exit 1
fi

grep -q 'qemu:///system' "$repo_root/lib/k3s/lifecycle.sh"
grep -Fq -- '--graphics none --console pty,target_type=serial' "$repo_root/lib/k3s/lifecycle.sh"
if grep -q -- '--graphics spice' "$repo_root/lib/k3s/lifecycle.sh"; then exit 1; fi
grep -q -- '--disable' "$repo_root/lib/k3s/lifecycle.sh"
if grep -q 'net-autostart' "$repo_root/lib/k3s/lifecycle.sh"; then
  echo 'management network must not autostart' >&2
  exit 1
fi
grep -q 'flannel-backend: vxlan' "$repo_root/ansible/roles/k3s/templates/config.yaml.j2"
grep -q 'cluster-init: true' "$repo_root/ansible/roles/k3s/templates/config.yaml.j2"
grep -q 'disable-network-policy: false' "$repo_root/ansible/roles/k3s/templates/config.yaml.j2"
grep -q 'enforce: restricted' "$repo_root/ansible/roles/k3s/files/pod-security.yaml"
grep -q 'checksum: "sha256:{{ k3s_binary_sha256 }}"' "$repo_root/ansible/roles/k3s/tasks/main.yml"
grep -q 'secrets-encryption: true' "$repo_root/ansible/roles/k3s/templates/config.yaml.j2"
grep -q 'disable_gpg_check: false' "$repo_root/ansible/roles/k3s/tasks/main.yml"
grep -q 'checksum: "sha256:{{ k3s_selinux_rpm_sha256 }}"' "$repo_root/ansible/roles/k3s/tasks/main.yml"
grep -q 'virtio-k3s-data' "$repo_root/ansible/roles/k3s/tasks/main.yml"
grep -q 'UUID={{ k3s_data_disk_uuid.stdout }}' "$repo_root/ansible/roles/k3s/tasks/main.yml"
source_assert_line=$(rg -n '^\- name: Assert local-path uses the expected dedicated virtio data disk' "$repo_root/ansible/roles/k3s/tasks/main.yml" | cut -d: -f1)
mode_line=$(rg -n '^\- name: Reapply restrictive local-path mountpoint mode after source verification' "$repo_root/ansible/roles/k3s/tasks/main.yml" | cut -d: -f1)
recursive_line=$(rg -n '^\- name: Recursively apply SELinux context only to fresh or empty first mount' "$repo_root/ansible/roles/k3s/tasks/main.yml" | cut -d: -f1)
((source_assert_line < mode_line && mode_line < recursive_line)) || {
  echo 'storage source must be verified before chmod/relabel' >&2
  exit 1
}
grep -q 'restorecon -RFv {{ k3s_storage_path }}' "$repo_root/ansible/roles/k3s/tasks/main.yml"
grep -q 'restorecon -Fv {{ k3s_storage_path }}' "$repo_root/ansible/roles/k3s/tasks/main.yml"
grep -q 'k3s_storage_first_entry' "$repo_root/ansible/roles/k3s/tasks/main.yml"
grep -q 'findmnt --noheadings --mountpoint' "$repo_root/ansible/roles/k3s/tasks/main.yml"
grep -q 'wipefs --noheadings --output TYPE' "$repo_root/ansible/roles/k3s/tasks/main.yml"
grep -q 'lsblk --noheadings --paths --output NAME' "$repo_root/ansible/roles/k3s/tasks/main.yml"
grep -q 'nft -c -f /etc/nftables/k3s-lab.nft' "$repo_root/ansible/roles/k3s/tasks/main.yml"
grep -q 'hook prerouting priority -150' "$repo_root/ansible/roles/k3s/templates/k3s-lab.nft.j2"
grep -q 'ct state established,related accept' "$repo_root/ansible/roles/k3s/templates/k3s-lab.nft.j2"
grep -q 'iifname "{{ k3s_management_interface }}" drop' "$repo_root/ansible/roles/k3s/templates/k3s-lab.nft.j2"
grep -q 'k3s_take_lifecycle_snapshot' "$repo_root/ansible/roles/k3s/tasks/main.yml"
grep -q -- '--cluster-reset-restore-path=' "$repo_root/ansible/roles/k3s/tasks/restore.yml"
grep -q 'k3s_primary_node_name' "$repo_root/ansible/roles/k3s/tasks/restore.yml"
grep -q 'K3S_RESTORE_TEST_NAME' "$repo_root/lib/k3s/lifecycle.sh"
grep -q 'repository="s3:s3.' "$repo_root/lib/k3s/lifecycle.sh"
grep -q 'systemd-creds decrypt --name=restic_password' "$repo_root/lib/k3s/lifecycle.sh"
grep -q 'backup-init' "$repo_root/bin/k3s-lab"
grep -q 'route53-cluster-issuer.yaml' "$repo_root/lib/k3s/lifecycle.sh"
grep -q 'k3s_refresh_ansible_vars' "$repo_root/lib/k3s/lifecycle.sh"
# shellcheck disable=SC2016
grep -q 'install -d -m 0711 "$VM_POOL_DIR" "$base_dir"' "$repo_root/lib/k3s/lifecycle.sh"
grep -q -- '--ask-become-pass' "$repo_root/lib/k3s/lifecycle.sh"
grep -q 'gather_facts: false' "$repo_root/ansible/k3s.yml"
grep -q 'ansible.builtin.wait_for_connection:' "$repo_root/ansible/k3s.yml"
grep -q 'ansible.builtin.setup:' "$repo_root/ansible/k3s.yml"
grep -q 'gather_facts: false' "$repo_root/ansible/k3s-restore-test.yml"
grep -q '80-k3s-bootstrap' "$repo_root/ansible/roles/k3s/tasks/main.yml"
grep -q 'password-required administrator sudo policy' "$repo_root/ansible/roles/k3s/tasks/main.yml"
password_assert_line=$(grep -n 'Refuse provisioning before a local sudo password exists' "$repo_root/ansible/roles/k3s/tasks/main.yml" | cut -d: -f1)
bootstrap_remove_line=$(grep -n 'Remove one-time passwordless bootstrap sudo policy' "$repo_root/ansible/roles/k3s/tasks/main.yml" | cut -d: -f1)
prerequisite_line=$(grep -n 'Install prerequisites' "$repo_root/ansible/roles/k3s/tasks/main.yml" | cut -d: -f1)
((password_assert_line < bootstrap_remove_line && bootstrap_remove_line < prerequisite_line))
grep -q 'route53-dns01-credentials' "$repo_root/kubernetes/cert-manager/templates/route53-cluster-issuer.yaml.tmpl"
grep -q 'upgrade version must exactly equal K3S_VERSION' "$repo_root/lib/k3s/lifecycle.sh"
grep -q 'net-dumpxml' "$repo_root/lib/k3s/lifecycle.sh"
grep -q 'existing management network must have autostart disabled' "$repo_root/lib/k3s/lifecycle.sh"
[[ ! -e $repo_root/ansible/roles/k3s/templates/k3s-common.repo.j2 ]]
grep -q 'Assert the installed K3s SELinux policy is the locked release' "$repo_root/ansible/roles/k3s/tasks/main.yml"
grep -q 'node-name: "{{ k3s_node_name }}"' "$repo_root/ansible/roles/k3s/templates/config.yaml.j2"
if grep -Eq '^ *- servicelb$' "$repo_root/ansible/roles/k3s/templates/config.yaml.j2"; then
  echo 'ServiceLB must remain enabled for the VM lab' >&2
  exit 1
fi
grep -q 'nmcli general reload' "$repo_root/ansible/roles/k3s/handlers/main.yml"
if grep -q 'name: NetworkManager' "$repo_root/ansible/roles/k3s/handlers/main.yml"; then
  echo 'NetworkManager must be reloaded rather than restarted over SSH' >&2
  exit 1
fi
grep -q 'Requires=k3s-lab-firewall.service' "$repo_root/ansible/roles/k3s/tasks/main.yml"
grep -q 'iptables' "$repo_root/ansible/roles/k3s/tasks/main.yml"
grep -q 'default-deny-ingress-and-egress' "$repo_root/kubernetes/base/default-deny-public.yaml"
grep -q 'allow-dns-egress' "$repo_root/kubernetes/base/allow-dns-public.yaml"
grep -q 'allow-traefik-ingress' "$repo_root/kubernetes/base/allow-traefik-public.yaml"
grep -q 'reclaimPolicy: Retain' "$repo_root/kubernetes/local-path/storageclass.yaml"
grep -q 'pod-security.kubernetes.io/enforce: privileged' "$repo_root/kubernetes/local-path/namespace.yaml"
grep -q 'k3s/storage/local-path' "$repo_root/kubernetes/local-path/configmap.yaml"
helper_image=$(awk -F= '$1 == "LOCAL_PATH_HELPER_IMAGE" { print $2 }' "$repo_root/versions.lock")
[[ "$helper_image" == *@sha256:* ]]
grep -Fq "image: $helper_image" "$repo_root/kubernetes/local-path/configmap.yaml"
grep -q 'mode=render' "$repo_root/kubernetes/install-pinned-addons.sh"
grep -q -- '--confirm-context' "$repo_root/kubernetes/install-pinned-addons.sh"
if grep -q 'field-manager=k3s-lab-local-path-override' "$repo_root/kubernetes/install-pinned-addons.sh"; then
  echo 'local-path installer defines a competing override field manager' >&2
  exit 1
fi
# shellcheck disable=SC2016
grep -Fq 'field-manager=k3s-lab-local-path -f "$local_path_merged"' "$repo_root/kubernetes/install-pinned-addons.sh"
grep -q -- '--force-conflicts' "$repo_root/kubernetes/install-pinned-addons.sh"
! rg -n --glob '!test_static.sh' 'aws_access_key_id|aws_secret_access_key|BEGIN .*PRIVATE KEY|ROUTE53.*SECRET' "$repo_root/ansible" "$repo_root/cloud-init" "$repo_root/kubernetes"
