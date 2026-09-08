#!/usr/bin/env bash

k3s_repo_root() { cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P; }
k3s_runtime_dir() { printf '%s' "$VM_POOL_DIR"; }

k3s_render_template() {
  local template="$1" output="$2" key value public_key
  public_key="$(<"$SSH_PUBLIC_KEY_FILE")"
  [[ "$(awk 'END { print NR }' "$SSH_PUBLIC_KEY_FILE")" == 1 ]] || k3s_die 'SSH public key file must contain exactly one line'
  [[ "$public_key" =~ ^(ssh-ed25519|sk-ssh-ed25519@openssh.com|ecdsa-sha2-nistp256|sk-ecdsa-sha2-nistp256@openssh.com)[[:space:]][A-Za-z0-9+/=]+([[:space:]].*)?$ ]] \
    || k3s_die 'SSH public key has an unsupported or malformed format'
  cp -- "$template" "$output"
  for key in LAB_NAME ADMIN_USER MGMT_MAC DMZ_MAC MGMT_INTERFACE DMZ_INTERFACE MGMT_IP MGMT_GATEWAY DMZ_IPV4 DMZ_IPV4_GATEWAY DMZ_IPV6 DMZ_IPV6_GATEWAY DOMAIN ACME_EMAIL ROUTE53_ZONE_ID AWS_REGION; do
    value="${!key}"; value="${value//\\/\\\\}"; value="${value//&/\\&}"; value="${value//|/\\|}"
    sed -i.bak "s|__${key}__|${value}|g" "$output"; rm -f -- "${output}.bak"
  done
  public_key="${public_key//\\/\\\\}"; public_key="${public_key//&/\\&}"; public_key="${public_key//|/\\|}"
  sed -i.bak "s|__SSH_PUBLIC_KEY__|${public_key}|g" "$output"; rm -f -- "${output}.bak"
  ! grep -q '__[A-Z].*__' "$output" || k3s_die "unrendered placeholder in $output"
}

k3s_write_ansible_vars() {
  local output="$1"
  cat > "$output" <<EOF
---
k3s_version: "${K3S_VERSION}"
k3s_binary_sha256: "${K3S_BINARY_SHA256}"
k3s_selinux_rpm_version: "${K3S_SELINUX_RPM_VERSION}"
k3s_selinux_rpm_sha256: "${K3S_SELINUX_RPM_SHA256}"
k3s_minor: "${K3S_MINOR}"
k3s_rpm_gpg_key_url: "${K3S_RPM_GPG_KEY_URL}"
k3s_rpm_gpg_key_sha256: "${K3S_RPM_GPG_KEY_SHA256}"
k3s_rpm_gpg_fingerprint: "${K3S_RPM_GPG_FINGERPRINT}"
k3s_node_name: "${LAB_NAME}"
k3s_admin_user: "${ADMIN_USER}"
k3s_management_interface: "${MGMT_INTERFACE}"
k3s_dmz_interface: "${DMZ_INTERFACE}"
k3s_node_ip: "${MGMT_IP}"
k3s_node_external_ip: "${DMZ_IPV4%/*}"
k3s_bind_address: "${MGMT_IP}"
k3s_advertise_address: "${MGMT_IP}"
k3s_tls_sans:
  - "${MGMT_IP}"
  - "${MGMT_FQDN}"
k3s_cluster_cidr: "${CLUSTER_CIDR}"
k3s_service_cidr: "${SERVICE_CIDR}"
k3s_cluster_dns: "${K3S_CLUSTER_DNS}"
k3s_management_cidr: "${MGMT_CIDR}"
k3s_data_device: "/dev/disk/by-id/virtio-k3s-data"
EOF
}

k3s_write_inventory() {
  local output="$1"
  cat > "$output" <<EOF
[k3s_servers]
${LAB_NAME} ansible_host=${MGMT_IP} ansible_user=${ADMIN_USER} ansible_ssh_private_key_file=${SSH_PRIVATE_KEY_FILE} ansible_ssh_common_args='-o StrictHostKeyChecking=yes -o UserKnownHostsFile=${SSH_KNOWN_HOSTS_FILE}'

[k3s_servers:vars]
ansible_python_interpreter=/usr/bin/python3
EOF
}

k3s_refresh_ansible_vars() {
  local target="$VM_POOL_DIR/ansible-vars.yml" staged="$VM_POOL_DIR/ansible-vars.yml.new.$$"
  [[ -d "$VM_POOL_DIR" ]] || k3s_die "VM pool is unavailable: $VM_POOL_DIR"
  umask 077
  k3s_write_ansible_vars "$staged"
  chmod 0600 "$staged"
  mv -- "$staged" "$target"
}

k3s_require_ansible_access() {
  k3s_need_command ssh-keygen
  [[ -r "$SSH_PRIVATE_KEY_FILE" ]] || k3s_die "SSH private-key handle is unreadable: $SSH_PRIVATE_KEY_FILE"
  [[ -r "$SSH_KNOWN_HOSTS_FILE" ]] || k3s_die "dedicated K3s known-hosts file is unreadable: $SSH_KNOWN_HOSTS_FILE"
  ssh-keygen -F "$MGMT_IP" -f "$SSH_KNOWN_HOSTS_FILE" >/dev/null \
    || k3s_die 'dedicated known-hosts file does not contain the management IP; verify the guest host key out of band first'
}

k3s_ansible_extra_vars() {
  local vars_file="$VM_POOL_DIR/ansible-vars.yml"
  [ -r "$vars_file" ] || k3s_die "missing generated Ansible variables: $vars_file; run create first"
  printf '%s' "$vars_file"
}

k3s_download_base_image() {
  local base_dir image checksum signature keyring keyfile expected
  base_dir="$VM_POOL_DIR/base"; image="$base_dir/$ALMALINUX_IMAGE_NAME"
  # QEMU needs search permission on the pool and backing-image directory. The
  # directory remains non-listable, while generated configuration stays 0700.
  install -d -m 0711 "$VM_POOL_DIR" "$base_dir"
  if [ -f "$image" ] && printf '%s  %s\n' "$ALMALINUX_IMAGE_SHA256" "$image" | sha256sum --check --status; then
    chmod 0444 "$image"
    return 0
  fi
  k3s_need_command curl; k3s_need_command gpg; k3s_need_command sha256sum
  keyring="$(mktemp -d)"
  checksum="$keyring/CHECKSUM"; signature="$keyring/CHECKSUM.asc"; keyfile="$keyring/almalinux.key"
  curl --fail --location --proto '=https' --tlsv1.2 -o "$checksum" "$ALMALINUX_CHECKSUM_URL"
  curl --fail --location --proto '=https' --tlsv1.2 -o "$signature" "$ALMALINUX_CHECKSUM_SIGNATURE_URL"
  curl --fail --location --proto '=https' --tlsv1.2 -o "$keyfile" 'https://repo.almalinux.org/almalinux/RPM-GPG-KEY-AlmaLinux-9'
  gpg --homedir "$keyring" --batch --import "$keyfile" >/dev/null 2>&1
  gpg --homedir "$keyring" --batch --with-colons --fingerprint | k3s_gpg_primary_matches "$ALMALINUX_SIGNING_FINGERPRINT" || k3s_die 'AlmaLinux signing fingerprint did not match versions.lock'
  gpg --homedir "$keyring" --batch --verify "$signature" "$checksum" >/dev/null 2>&1 || k3s_die 'AlmaLinux CHECKSUM signature verification failed'
  expected="$(awk -v image="$ALMALINUX_IMAGE_NAME" '$2 == image { print $1; exit }' "$checksum")"
  [ "$expected" = "$ALMALINUX_IMAGE_SHA256" ] || k3s_die 'AlmaLinux checksum file does not match versions.lock'
  curl --fail --location --proto '=https' --tlsv1.2 -o "$image.partial" "$ALMALINUX_IMAGE_URL"
  printf '%s  %s\n' "$ALMALINUX_IMAGE_SHA256" "$image.partial" | sha256sum --check --status || k3s_die 'AlmaLinux image SHA-256 verification failed'
  mv -- "$image.partial" "$image"
  chmod 0444 "$image"
  rm -rf -- "$keyring"
}

k3s_define_management_network() {
  local network_xml="$1/k3s-mgmt.xml" existing_xml network_info dhcp_start dhcp_end mgmt_integer start_integer end_integer
  if network_info="$(virsh -c qemu:///system net-info "$MGMT_NETWORK_NAME" 2>/dev/null)"; then
    grep -q '^Autostart:.*no$' <<< "$network_info" \
      || k3s_die 'existing management network must have autostart disabled'
    existing_xml="$(virsh -c qemu:///system net-dumpxml "$MGMT_NETWORK_NAME")"
    { grep -Fq "<forward mode='nat'" <<< "$existing_xml" || grep -Fq '<forward mode="nat"' <<< "$existing_xml"; } || k3s_die 'existing management network does not use NAT'
    { grep -Fq "<bridge name='${MGMT_BRIDGE}'" <<< "$existing_xml" || grep -Fq "<bridge name=\"${MGMT_BRIDGE}\"" <<< "$existing_xml"; } || k3s_die 'existing management network bridge differs from configuration'
    { grep -Fq "<ip address='${MGMT_GATEWAY}' netmask='255.255.255.0'" <<< "$existing_xml" || grep -Fq "<ip address=\"${MGMT_GATEWAY}\" netmask=\"255.255.255.0\"" <<< "$existing_xml"; } || k3s_die 'existing management network gateway differs from configuration'
    dhcp_start="$(sed -nE "s/.*start=['\"]([0-9.]+)['\"].*/\1/p" <<< "$existing_xml" | head -n1)"
    dhcp_end="$(sed -nE "s/.*end=['\"]([0-9.]+)['\"].*/\1/p" <<< "$existing_xml" | head -n1)"
    if [[ -n "$dhcp_start" || -n "$dhcp_end" ]]; then
      [[ -n "$dhcp_start" && -n "$dhcp_end" ]] || k3s_die 'existing management network has an incomplete DHCP range'
      mgmt_integer="$(k3s_ipv4_to_int "$MGMT_IP")"; start_integer="$(k3s_ipv4_to_int "$dhcp_start")"; end_integer="$(k3s_ipv4_to_int "$dhcp_end")" \
        || k3s_die 'existing management network has an invalid DHCP range'
      ((mgmt_integer < start_integer || mgmt_integer > end_integer)) \
        || k3s_die 'MGMT_IP overlaps the existing libvirt DHCP range'
    fi
    return 0
  fi
  cat > "$network_xml" <<EOF
<network>
  <name>${MGMT_NETWORK_NAME}</name>
  <forward mode='nat'/>
  <bridge name='${MGMT_BRIDGE}' stp='on' delay='0'/>
  <ip address='${MGMT_GATEWAY}' netmask='255.255.255.0'>
    <dhcp><range start='${MGMT_GATEWAY%.*}.100' end='${MGMT_GATEWAY%.*}.200'/></dhcp>
  </ip>
</network>
EOF
  virsh -c qemu:///system net-define "$network_xml"
}

k3s_start_management_network() {
  virsh -c qemu:///system net-info "$MGMT_NETWORK_NAME" | grep -q '^Active:.*yes$' || virsh -c qemu:///system net-start "$MGMT_NETWORK_NAME"
}

k3s_validate_dmz_bridge() {
  k3s_need_command ip
  ip link show dev "$DMZ_BRIDGE" >/dev/null 2>&1 || k3s_die "DMZ bridge does not exist: $DMZ_BRIDGE"
  ! ip -o addr show dev "$DMZ_BRIDGE" | grep -Eq ' (inet|inet6) ' || k3s_die "DMZ bridge must not have a host IP address: $DMZ_BRIDGE"
  ip link show dev "$DMZ_PARENT" >/dev/null 2>&1 || k3s_die "DMZ parent does not exist: $DMZ_PARENT"
  ip -d link show dev "$DMZ_VLAN_INTERFACE" | grep -Eq "vlan .* id ${DMZ_VLAN_ID}( |$)" || k3s_die 'DMZ VLAN interface or VLAN ID does not match configuration'
  ip -o link show dev "$DMZ_VLAN_INTERFACE" | grep -Fq "master $DMZ_BRIDGE" || k3s_die 'DMZ VLAN interface is not attached to the configured bridge'
}

k3s_network_create() {
  local bridge_connection="k3s-${LAB_NAME}-dmz-bridge" vlan_connection="k3s-${LAB_NAME}-dmz-vlan" response
  k3s_require_root; k3s_need_command nmcli; k3s_need_command ip
  ip link show dev "$DMZ_PARENT" >/dev/null 2>&1 || k3s_die "DMZ parent does not exist: $DMZ_PARENT"
  if ip link show dev "$DMZ_BRIDGE" >/dev/null 2>&1; then k3s_validate_dmz_bridge; return 0; fi
  ! nmcli -t -f NAME connection show | grep -Fxq "$bridge_connection" || k3s_die "NetworkManager connection already exists: $bridge_connection"
  ! nmcli -t -f NAME connection show | grep -Fxq "$vlan_connection" || k3s_die "NetworkManager connection already exists: $vlan_connection"
  ! ip -o addr show dev "$DMZ_PARENT" | grep -Eq ' (inet|inet6) ' || k3s_die 'DMZ parent has a host IP; use a dedicated unnumbered NIC'
  [[ -t 0 ]] || k3s_die 'network creation requires an interactive terminal'
  printf 'Type %s:%s to create the unnumbered DMZ bridge: ' "$DMZ_PARENT" "$DMZ_VLAN_ID" >&2
  IFS= read -r response
  [[ "$response" == "$DMZ_PARENT:$DMZ_VLAN_ID" ]] || k3s_die 'DMZ network confirmation did not match'
  nmcli connection add type bridge ifname "$DMZ_BRIDGE" con-name "$bridge_connection" \
    ipv4.method disabled ipv6.method disabled bridge.stp yes connection.autoconnect no
  if ! nmcli connection add type vlan ifname "$DMZ_VLAN_INTERFACE" con-name "$vlan_connection" dev "$DMZ_PARENT" id "$DMZ_VLAN_ID" \
    master "$DMZ_BRIDGE" slave-type bridge connection.autoconnect no; then
    nmcli connection delete "$bridge_connection" >/dev/null 2>&1 || true
    k3s_die 'failed to create the DMZ VLAN connection; the new bridge connection was removed'
  fi
  nmcli connection up "$bridge_connection"
  nmcli connection up "$vlan_connection"
  k3s_validate_dmz_bridge
}

k3s_activate_dmz_bridge() {
  local bridge_connection="k3s-${LAB_NAME}-dmz-bridge" vlan_connection="k3s-${LAB_NAME}-dmz-vlan"
  k3s_need_command nmcli
  nmcli connection up "$bridge_connection" >/dev/null
  nmcli connection up "$vlan_connection" >/dev/null
  k3s_validate_dmz_bridge
}

k3s_dry_run_plan() {
  local operation="$1"; shift
  case "$operation" in
    network-create)
      [[ $# -eq 0 ]] || k3s_die 'dry-run network-create accepts no argument'
      printf 'PLAN: create non-autostart bridge %s over %s VLAN %s; assign no host IP\n' "$DMZ_BRIDGE" "$DMZ_PARENT" "$DMZ_VLAN_ID"
      ;;
    create)
      [[ $# -eq 0 ]] || k3s_die 'dry-run create accepts no argument'
      printf 'PLAN: verify %s; create powered-off %s with 8 vCPU, 16 GiB, 80/120 GiB disks, NAT+DMZ NICs; no autostart\n' "$ALMALINUX_IMAGE_NAME" "$LAB_NAME"
      ;;
    provision)
      [[ $# -eq 0 ]] || k3s_die 'dry-run provision accepts no argument'
      printf 'PLAN: run bounded Ansible inventory %s against %s, then detach and remove the cloud-init seed ISO\n' "$(k3s_inventory)" "$LAB_NAME"
      ;;
    up|status|backup) [[ $# -eq 0 ]] || k3s_die "dry-run $operation accepts no argument"; printf 'PLAN: %s development VM %s only\n' "$operation" "$LAB_NAME" ;;
    down)
      [[ $# -eq 0 || ( $# -eq 1 && ${1:-} == --skip-etcd-snapshot ) ]] \
        || k3s_die 'dry-run down accepts only --skip-etcd-snapshot'
      printf 'PLAN: down development VM %s only%s\n' "$LAB_NAME" "${1:+ without a new etcd snapshot}"
      ;;
    backup-init)
      [[ $# -eq 0 ]] || k3s_die 'dry-run backup-init accepts no argument'
      printf 'PLAN: initialize the exact encrypted Restic prefix for %s in bucket %s\n' "$LAB_NAME" "$RESTIC_BUCKET"
      ;;
    upgrade) [[ ${1:-} == "$K3S_VERSION" ]] || k3s_die 'dry-run upgrade version must match versions.lock'; printf 'PLAN: snapshot, cold-backup, and upgrade %s to %s\n' "$LAB_NAME" "$K3S_VERSION" ;;
    restore-test)
      [[ ${1:-} =~ ^/[^[:space:]]+$ ]] || k3s_die 'dry-run restore-test requires an absolute snapshot path in the detached clone'
      printf 'PLAN: restore %s only through a separately supplied detached-clone inventory\n' "$1"
      ;;
    *) k3s_die "unknown operation for dry-run: $operation" ;;
  esac
}

k3s_create() {
  local root dir cloud_iso system_disk data_disk xml base_image
  root="$(k3s_repo_root)"; k3s_require_root
  for cmd in virsh qemu-img cloud-localds virt-install sha256sum; do k3s_need_command "$cmd"; done
  k3s_validate_dmz_bridge
  k3s_download_base_image
  dir="$(k3s_runtime_dir)"; base_image="$dir/base/$ALMALINUX_IMAGE_NAME"
  [ ! -e "$dir/cloud-init.iso" ] && [ ! -e "$dir/system.qcow2" ] && [ ! -e "$dir/data.qcow2" ] || k3s_die "refusing to reuse VM storage: $dir"
  ! virsh -c qemu:///system dominfo "$LAB_NAME" >/dev/null 2>&1 || k3s_die "domain already exists: $LAB_NAME"
  install -d -m 0700 "$dir/cloud-init" "$dir/generated"
  umask 077
  [[ ! -e $dir/create.conf && ! -e $dir/create.lock ]] || k3s_die 'creation provenance already exists; inspect the partial VM before retrying'
  install -m 0600 "$K3S_CONFIG_SOURCE" "$dir/create.conf"
  install -m 0600 "$K3S_LOCK_SOURCE" "$dir/create.lock"
  k3s_render_template "$root/cloud-init/user-data.yaml.tmpl" "$dir/cloud-init/user-data"
  k3s_render_template "$root/cloud-init/meta-data.tmpl" "$dir/cloud-init/meta-data"
  k3s_render_template "$root/cloud-init/network-config.yaml.tmpl" "$dir/cloud-init/network-config"
  k3s_render_template "$root/kubernetes/cert-manager/templates/route53-cluster-issuer.yaml.tmpl" "$dir/generated/route53-cluster-issuer.yaml"
  k3s_write_ansible_vars "$dir/ansible-vars.yml"
  k3s_write_inventory "$dir/inventory.ini"
  cloud_iso="$dir/cloud-init.iso"; system_disk="$dir/system.qcow2"; data_disk="$dir/data.qcow2"
  cloud-localds --network-config="$dir/cloud-init/network-config" "$cloud_iso" "$dir/cloud-init/user-data" "$dir/cloud-init/meta-data"
  qemu-img create -f qcow2 -F qcow2 -b "$base_image" "$system_disk" "${VM_SYSTEM_DISK_GIB}G"
  qemu-img create -f qcow2 "$data_disk" "${VM_DATA_DISK_GIB}G"
  k3s_define_management_network "$dir"; xml="$dir/domain.xml"
  virt-install --connect qemu:///system --name "$LAB_NAME" --machine q35 --memory "$VM_MEMORY_MIB" --vcpus "$VM_VCPUS",sockets=1,cores="$VM_VCPUS",threads=1 --cpu host-passthrough --boot uefi --import --osinfo detect=on,require=off --disk "path=$system_disk,format=qcow2,bus=virtio,cache=none,io=native,discard=unmap,serial=k3s-system" --disk "path=$data_disk,format=qcow2,bus=virtio,cache=none,io=native,discard=unmap,serial=k3s-data" --disk "path=$cloud_iso,device=cdrom,readonly=on" --network "network=$MGMT_NETWORK_NAME,mac=$MGMT_MAC,model=virtio" --network "bridge=$DMZ_BRIDGE,mac=$DMZ_MAC,model=virtio" --channel unix,target.type=virtio,target.name=org.qemu.guest_agent.0 --graphics none --console pty,target_type=serial --noautoconsole --print-xml > "$xml"
  virsh -c qemu:///system define "$xml"; virsh -c qemu:///system autostart "$LAB_NAME" --disable
  printf 'Created %s; it is intentionally powered off.\n' "$LAB_NAME"
}

k3s_up() { k3s_require_root; virsh -c qemu:///system dominfo "$LAB_NAME" >/dev/null || k3s_die 'domain does not exist'; k3s_activate_dmz_bridge; k3s_start_management_network; virsh -c qemu:///system domstate "$LAB_NAME" | grep -qx running || virsh -c qemu:///system start "$LAB_NAME"; }
k3s_inventory() { printf '%s' "${K3S_ANSIBLE_INVENTORY:-$VM_POOL_DIR/inventory.ini}"; }

k3s_guest_snapshot() {
  local root
  k3s_require_ansible_access
  root="$(k3s_repo_root)"
  ansible-playbook -i "$(k3s_inventory)" "$root/ansible/k3s.yml" --limit "$LAB_NAME" \
    --ask-become-pass --extra-vars "@$(k3s_ansible_extra_vars)" \
    --extra-vars k3s_take_lifecycle_snapshot=true --tags etcd_snapshot
}

k3s_provision() {
  local root seed_target
  root="$(k3s_repo_root)"; k3s_require_root; k3s_need_command ansible-playbook
  k3s_require_ansible_access
  virsh -c qemu:///system domstate "$LAB_NAME" | grep -qx running || k3s_die 'start the VM before provisioning'
  # Refresh mutable release locks immediately before applying them. The cold
  # backup path deliberately retains the previous file for rollback.
  k3s_refresh_ansible_vars
  ansible-playbook -i "$(k3s_inventory)" "$root/ansible/k3s.yml" --limit "$LAB_NAME" \
    --ask-become-pass --extra-vars "@$(k3s_ansible_extra_vars)"
  seed_target="$(virsh -c qemu:///system domblklist "$LAB_NAME" --details | awk -v source="$VM_POOL_DIR/cloud-init.iso" '$4 == source { print $3; exit }')"
  if [[ -n "$seed_target" ]]; then
    virsh -c qemu:///system detach-disk "$LAB_NAME" "$seed_target" --live --config
  fi
  rm -f -- "$VM_POOL_DIR/cloud-init.iso"
  printf 'Provisioned %s and removed its cloud-init seed ISO.\n' "$LAB_NAME"
}

k3s_down() {
  local skip_snapshot="${1:-}"
  [[ -z "$skip_snapshot" || "$skip_snapshot" == --skip-etcd-snapshot ]] \
    || k3s_die 'down accepts only --skip-etcd-snapshot'
  k3s_require_root
  if virsh -c qemu:///system domstate "$LAB_NAME" | grep -qx running; then
    [ "$skip_snapshot" = '--skip-etcd-snapshot' ] || k3s_guest_snapshot
    virsh -c qemu:///system shutdown "$LAB_NAME"
    for _ in $(seq 1 60); do virsh -c qemu:///system domstate "$LAB_NAME" | grep -qx 'shut off' && return 0; sleep 2; done
    k3s_die 'guest did not shut down cleanly; inspect it rather than destroying it'
  fi
}

k3s_status() { virsh -c qemu:///system dominfo "$LAB_NAME"; virsh -c qemu:///system domifaddr "$LAB_NAME" --source agent || true; }

k3s_backup_regular_file() {
  [[ -f $1 && ! -L $1 ]] || k3s_die "backup input must be a regular, non-symlink file: $1"
}

k3s_backup_check_chain() {
  local disk=$1 expected_base=${2:-} chain
  chain=$(qemu-img info --backing-chain --output=json "$disk") || k3s_die 'cannot inspect cold qcow2 backing chain'
  # Reject external data files as well as unexpected backing images. These can
  # otherwise make an apparently complete two-file backup depend on another disk.
  jq -e --arg disk "$disk" --arg base "$expected_base" '
    type == "array" and .[0].filename == $disk and
    all(.[]; .format == "qcow2") and
    ([.. | objects | select(has("data-file"))] | length == 0) and
    (if $base == "" then length == 1 and (.[0] | has("backing-filename") | not)
     else length == 2 and .[0]["full-backing-filename"] == $base and
       .[1].filename == $base and (.[1] | has("backing-filename") | not) end)
  ' <<< "$chain" >/dev/null || k3s_die 'foreign or unsupported qcow2 backing chain; no backup was started'
}

k3s_backup_prepare() {
  local metadata xml base_name base_hash base_image loader nvram nvram_template path resolved count cmd
  for cmd in virsh qemu-img jq xmllint sha256sum readlink; do k3s_need_command "$cmd"; done
  for path in create.lock create.conf system.qcow2 data.qcow2 ansible-vars.yml inventory.ini; do
    k3s_backup_regular_file "$VM_POOL_DIR/$path"
  done
  base_name=$(awk -F= '$1 == "ALMALINUX_IMAGE_NAME" { print $2; n++ } END { if (n != 1) exit 1 }' "$VM_POOL_DIR/create.lock") || k3s_die 'invalid creation image lock'
  base_hash=$(awk -F= '$1 == "ALMALINUX_IMAGE_SHA256" { print $2; n++ } END { if (n != 1) exit 1 }' "$VM_POOL_DIR/create.lock") || k3s_die 'invalid creation image checksum'
  k3s_validate_value ALMALINUX_IMAGE_NAME "$base_name"
  k3s_validate_value ALMALINUX_IMAGE_SHA256 "$base_hash"
  base_image="$VM_POOL_DIR/base/$base_name"
  k3s_backup_regular_file "$base_image"
  printf '%s  %s\n' "$base_hash" "$base_image" | sha256sum --check --status || k3s_die 'backing image no longer matches creation provenance'
  k3s_backup_check_chain "$VM_POOL_DIR/system.qcow2" "$base_image"
  k3s_backup_check_chain "$VM_POOL_DIR/data.qcow2"
  umask 077
  metadata=$(mktemp -d "$VM_POOL_DIR/backup-metadata.XXXXXX")
  xml="$metadata/domain.xml"
  virsh -c qemu:///system dumpxml --inactive "$LAB_NAME" > "$xml" || k3s_die 'cannot record inactive domain XML'
  [[ $(xmllint --nonet --xpath 'string(/domain/name)' "$xml") == "$LAB_NAME" ]] || k3s_die 'domain XML identity mismatch'
  count=$(xmllint --nonet --xpath 'count(/domain/devices/disk[@device="disk"])' "$xml")
  [[ $count == 2 && $(xmllint --nonet --xpath 'count(/domain/devices/hostdev | /domain/devices/filesystem | /domain/devices/tpm)' "$xml") == 0 ]] || k3s_die 'foreign disk, passthrough or TPM state in backup domain'
  for path in system.qcow2 data.qcow2; do
    count=$(xmllint --nonet --xpath "count(/domain/devices/disk[@device='disk' and @type='file']/source[@file='$VM_POOL_DIR/$path'])" "$xml")
    [[ $count == 1 ]] || k3s_die 'domain disks differ from the configured cold backup inputs'
  done
  [[ $(xmllint --nonet --xpath 'count(/domain/os/loader)' "$xml") == 1 && $(xmllint --nonet --xpath 'count(/domain/os/nvram)' "$xml") == 1 ]] || k3s_die 'backup requires the expected file-backed UEFI loader and NVRAM'
  loader=$(xmllint --nonet --xpath 'normalize-space(/domain/os/loader)' "$xml")
  nvram=$(xmllint --nonet --xpath 'normalize-space(/domain/os/nvram/text())' "$xml")
  [[ -n $nvram ]] || nvram=$(xmllint --nonet --xpath 'string(/domain/os/nvram/source/@file)' "$xml")
  nvram_template=$(xmllint --nonet --xpath 'string(/domain/os/nvram/@template)' "$xml")
  [[ $nvram == "/var/lib/libvirt/qemu/nvram/${LAB_NAME}_VARS.fd" || $nvram == "/var/lib/libvirt/qemu/nvram/${LAB_NAME}_VARS.qcow2" ]] || k3s_die 'NVRAM path is not owned by the selected VM'
  k3s_backup_regular_file "$nvram"
  [[ $nvram != *.qcow2 ]] || k3s_backup_check_chain "$nvram"
  K3S_BACKUP_INPUTS=("$VM_POOL_DIR/system.qcow2" "$VM_POOL_DIR/data.qcow2" "$base_image" "$nvram" "$metadata"
    "$VM_POOL_DIR/create.lock" "$VM_POOL_DIR/create.conf" "$VM_POOL_DIR/ansible-vars.yml" "$VM_POOL_DIR/inventory.ini")
  for path in "$loader" "$nvram_template"; do
    [[ -n $path ]] || continue
    [[ $path == /usr/share/edk2/* || $path == /usr/share/edk2-ovmf/* ]] || k3s_die 'firmware is outside the supported Arch edk2 package paths'
    resolved=$(readlink -f -- "$path") || k3s_die 'cannot resolve the firmware file'
    [[ $resolved == /usr/share/edk2/* || $resolved == /usr/share/edk2-ovmf/* ]] || k3s_die 'firmware symlink leaves the supported package paths'
    k3s_backup_regular_file "$resolved"
    # Restic preserves symlinks, so also capture the resolved firmware bytes.
    K3S_BACKUP_INPUTS+=("$path" "$resolved")
  done
  for path in cloud-init generated; do
    [[ -d $VM_POOL_DIR/$path && ! -L $VM_POOL_DIR/$path ]] || k3s_die "missing VM provisioning provenance: $path"
    K3S_BACKUP_INPUTS+=("$VM_POOL_DIR/$path")
  done
  count=$(xmllint --nonet --xpath 'count(/domain/devices/disk[not(@device="disk")]/source)' "$xml")
  if [[ $count != 0 ]]; then
    [[ $count == 1 && $(xmllint --nonet --xpath "count(/domain/devices/disk[@device='cdrom' and @type='file']/source[@file='$VM_POOL_DIR/cloud-init.iso'])" "$xml") == 1 ]] || k3s_die 'unknown removable media referenced by the domain'
    k3s_backup_regular_file "$VM_POOL_DIR/cloud-init.iso"
    K3S_BACKUP_INPUTS+=("$VM_POOL_DIR/cloud-init.iso")
  fi
  virsh -c qemu:///system net-dumpxml "$MGMT_NETWORK_NAME" > "$metadata/management-network.xml"
  cp -- "$K3S_LOCK_SOURCE" "$metadata/backup-request.lock"
  qemu-img --version > "$metadata/qemu-version.txt"
  virsh --version > "$metadata/libvirt-version.txt"
  printf '%s\n' "${K3S_BACKUP_INPUTS[@]}" | jq -Rsc --arg name "$LAB_NAME" --arg pool "$VM_POOL_DIR" --arg base "$base_hash" \
    '{schema:1,domain:$name,original_pool:$pool,base_sha256:$base,inputs:(split("\n")[:-1]),restore_requires_path_review:true}' > "$metadata/backup-manifest.json"
}

k3s_restic() {
  local action="$1" repository="s3:s3.${AWS_REGION}.amazonaws.com/${RESTIC_BUCKET}/k3s/${LAB_NAME}"
  local password_credential=/etc/credstore.encrypted/workstation-restic-password.cred
  local aws_credential=/etc/credstore.encrypted/workstation-restic-aws.cred
  local credential_dir
  k3s_need_command restic; k3s_need_command systemd-creds
  [[ -r "$password_credential" && -r "$aws_credential" ]] \
    || k3s_die 'encrypted Restic password or AWS credential is missing; create it with workstationctl'
  credential_dir="$(mktemp -d /run/k3s-lab-restic.XXXXXX)"
  (
    trap 'rm -rf -- "$credential_dir"' EXIT
    umask 077
    systemd-creds decrypt --name=restic_password "$password_credential" "$credential_dir/password"
    systemd-creds decrypt --name=aws_credentials "$aws_credential" "$credential_dir/aws"
    export RESTIC_PASSWORD_FILE="$credential_dir/password"
    export AWS_SHARED_CREDENTIALS_FILE="$credential_dir/aws"
    export AWS_EC2_METADATA_DISABLED=true
    case "$action" in
      init) restic --repository "$repository" init ;;
      backup)
        restic --repository "$repository" backup --tag k3s-lab --tag "$LAB_NAME" \
          "${K3S_BACKUP_INPUTS[@]}"
        ;;
      *) k3s_die 'internal Restic action is invalid' ;;
    esac
  )
}

k3s_backup_init() {
  local expected="${RESTIC_BUCKET}/k3s/${LAB_NAME}" response
  k3s_require_root
  [[ -t 0 ]] || k3s_die 'backup initialization requires an interactive terminal'
  printf 'Type %s to initialize the new Restic repository prefix: ' "$expected" >&2
  IFS= read -r response
  [[ "$response" == "$expected" ]] || k3s_die 'Restic repository confirmation did not match'
  k3s_restic init
}

k3s_backup() {
  k3s_require_root
  virsh -c qemu:///system domstate "$LAB_NAME" | grep -qx 'shut off' \
    || k3s_die 'cold overlay backup requires a shut-off VM; use down first'
  k3s_backup_prepare
  virsh -c qemu:///system domstate "$LAB_NAME" | grep -qx 'shut off' || k3s_die 'VM state changed during backup preparation'
  k3s_restic backup
}

k3s_upgrade() {
  local requested="$1" root
  [ "$requested" = "$K3S_VERSION" ] || k3s_die 'upgrade version must exactly equal K3S_VERSION in versions.lock'
  k3s_require_root; k3s_need_command restic
  virsh -c qemu:///system domstate "$LAB_NAME" | grep -qx running || k3s_die 'upgrade requires a running VM so it can make a tagged etcd snapshot'
  k3s_down
  k3s_backup
  k3s_refresh_ansible_vars
  k3s_up
  root="$(k3s_repo_root)"
  ansible-playbook -i "$(k3s_inventory)" "$root/ansible/k3s.yml" --limit "$LAB_NAME" \
    --ask-become-pass --extra-vars "@$(k3s_ansible_extra_vars)"
}

k3s_restore_test() {
  local snapshot="$1" root inventory clone_name
  [[ "$snapshot" =~ ^/[^[:space:]]+$ ]] || k3s_die 'restore-test snapshot must be an absolute path in the detached clone'
  inventory="${K3S_RESTORE_TEST_INVENTORY:-}"; clone_name="${K3S_RESTORE_TEST_NAME:-}"
  [ -r "$inventory" ] || k3s_die 'set K3S_RESTORE_TEST_INVENTORY to a readable detached clone inventory'
  [[ "$clone_name" =~ ^[a-z][a-z0-9-]{0,62}$ && "$clone_name" != "$LAB_NAME" ]] || k3s_die 'set K3S_RESTORE_TEST_NAME to a detached clone hostname distinct from the primary node'
  root="$(k3s_repo_root)"
  ansible-playbook -i "$inventory" "$root/ansible/k3s-restore-test.yml" --limit "$clone_name" --ask-become-pass \
    --extra-vars "k3s_restore_snapshot=$snapshot" --extra-vars "k3s_restore_clone_name=$clone_name" \
    --extra-vars "k3s_restore_clone_confirm=true" --extra-vars "k3s_primary_node_name=$LAB_NAME" \
    --extra-vars "k3s_node_name=$clone_name" --tags restore_test
}
