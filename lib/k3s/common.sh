#!/usr/bin/env bash
# Strict configuration handling for the non-secret K3s lab configuration.

set -o pipefail

k3s_die() { printf 'k3s-lab: %s\n' "$*" >&2; exit 1; }
k3s_need_command() { command -v "$1" >/dev/null 2>&1 || k3s_die "required command is unavailable: $1"; }
k3s_require_root() { [ "${EUID}" -eq 0 ] || k3s_die 'this operation must run as root'; }

# GnuPG --with-colons: record type is field 1, fingerprint is field 10.
# Accept exactly one primary key, never an unrelated key in a bundled keyring.
k3s_gpg_primary_matches() {
  awk -F: -v expected="$1" '
    $1 == "pub" { pubs++; primary=1; next }
    $1 == "sub" { primary=0 }
    $1 == "fpr" && primary { if (toupper($10) == toupper(expected)) found=1; primary=0 }
    END { exit !(pubs == 1 && found) }
  '
}

k3s_allowed_key() {
  case "$1" in
    QWEN_CODE_VERSION|QWEN_CODE_COMMIT|DSH_COMMIT|HERMES_AGENT_COMMIT) return 0 ;;
    K3S_SELINUX_RPM_SHA256) return 0 ;;
    OPEN_WEBUI_VERSION|OPEN_WEBUI_COMMIT|OPEN_WEBUI_IMAGE|RAG_EMBEDDING_REPOSITORY|RAG_EMBEDDING_REVISION) return 0 ;;
    SGLANG_DUAL_MODEL_REPOSITORY|SGLANG_DUAL_MODEL_REVISION|SGLANG_SINGLE_MODEL_REPOSITORY|SGLANG_SINGLE_MODEL_REVISION) return 0 ;;
    STEAM_HEADLESS_BASE_IMAGE|STEAM_HEADLESS_REVIEWED_SOURCE|SUNSHINE_VERSION|SUNSHINE_COMMIT|SUNSHINE_DEBIAN_SHA256|SUNSHINE_MESA_DEBIAN_VERSION|KWIN_DEBIAN_VERSION) return 0 ;;
    ROCM_AUR_REPOSITORY|ROCM_AUR_PACKAGE|ROCM_AUR_PACKAGE_VERSION|ROCM_AUR_COMMIT|ROCM_AUR_PKGBUILD_SHA256|ROCM_AUR_SOURCE_URL|ROCM_AUR_SOURCE_SHA256|SGLANG_ROCM_IMAGE|SGLANG_ROCM_INDEX_DIGEST|SGLANG_ROCM_CONFIG_DIGEST) return 0 ;;
    ROCM_THEROCK_REPOSITORY|ROCM_THEROCK_COMMIT|ROCM_LLAMA_CPP_REPOSITORY|ROCM_LLAMA_CPP_COMMIT|K3S_BINARY_SHA256) return 0 ;;
    LAB_NAME|ADMIN_USER|SSH_PUBLIC_KEY_FILE|SSH_PRIVATE_KEY_FILE|SSH_KNOWN_HOSTS_FILE|VM_VCPUS|VM_MEMORY_MIB|VM_SYSTEM_DISK_GIB|VM_DATA_DISK_GIB|VM_POOL_DIR|MGMT_NETWORK_NAME|MGMT_BRIDGE|MGMT_CIDR|MGMT_GATEWAY|MGMT_IP|MGMT_FQDN|MGMT_MAC|DMZ_PARENT|DMZ_VLAN_ID|DMZ_VLAN_INTERFACE|DMZ_BRIDGE|DMZ_IPV4|DMZ_IPV4_GATEWAY|DMZ_IPV6|DMZ_IPV6_GATEWAY|DMZ_MAC|MGMT_INTERFACE|DMZ_INTERFACE|CLUSTER_CIDR|SERVICE_CIDR|DOMAIN|ACME_EMAIL|ROUTE53_ZONE_ID|AWS_REGION|RESTIC_BUCKET|LOCK_FORMAT|LOCK_DATE|ALMALINUX_IMAGE_NAME|ALMALINUX_IMAGE_URL|ALMALINUX_IMAGE_SHA256|ALMALINUX_CHECKSUM_URL|ALMALINUX_CHECKSUM_SIGNATURE_URL|ALMALINUX_SIGNING_FINGERPRINT|K3S_VERSION|K3S_MINOR|K3S_SELINUX_RPM_VERSION|K3S_RPM_GPG_KEY_URL|K3S_RPM_GPG_KEY_SHA256|K3S_RPM_GPG_FINGERPRINT|CERT_MANAGER_VERSION|CERT_MANAGER_MANIFEST_URL|CERT_MANAGER_MANIFEST_SHA256|LOCAL_PATH_PROVISIONER_VERSION|LOCAL_PATH_PROVISIONER_MANIFEST_URL|LOCAL_PATH_PROVISIONER_MANIFEST_SHA256|LOCAL_PATH_HELPER_IMAGE|LLM_SCALER_IMAGE|OMZ_REPOSITORY|OMZ_COMMIT|JETBRAINS_TOOLBOX_VERSION|JETBRAINS_TOOLBOX_URL|JETBRAINS_TOOLBOX_SHA256|PARU_AUR_REPOSITORY|PARU_AUR_COMMIT|LINUX_GIT_AUR_REPOSITORY|LINUX_GIT_AUR_COMMIT|LINUX_GIT_SOURCE_REPOSITORY|LINUX_GIT_SOURCE_COMMIT|AWS_SSM_AUR_REPOSITORY|AWS_SSM_AUR_COMMIT) return 0 ;;
    *) return 1 ;;
  esac
}

k3s_validate_value() {
  local key="$1" value="$2"
  case "$key" in
    QWEN_CODE_VERSION) [[ "$value" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || k3s_die "$key must be an exact Qwen Code release" ;;
    QWEN_CODE_COMMIT|DSH_COMMIT|HERMES_AGENT_COMMIT) [[ "$value" =~ ^[a-f0-9]{40}$ ]] || k3s_die "$key must be an exact agent source revision" ;;
    K3S_VERSION) [[ "$value" =~ ^v1\.35\.[0-9]+\+k3s[0-9]+$ ]] || k3s_die "$key must be an exact K3s release" ;;
    OPEN_WEBUI_VERSION) [[ "$value" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || k3s_die "$key must be an exact Open WebUI release" ;;
    OPEN_WEBUI_COMMIT|RAG_EMBEDDING_REVISION|SGLANG_DUAL_MODEL_REVISION|SGLANG_SINGLE_MODEL_REVISION) [[ "$value" =~ ^[a-f0-9]{40}$ ]] || k3s_die "$key must be an exact source revision" ;;
    SGLANG_DUAL_MODEL_REPOSITORY) [[ "$value" == Qwen/Qwen3.8-27B-FP8 ]] || k3s_die "$key must select the reviewed dual-GPU checkpoint" ;;
    SGLANG_SINGLE_MODEL_REPOSITORY) [[ "$value" == Qwen/Qwen3.5-9B ]] || k3s_die "$key must select the reviewed single-GPU checkpoint" ;;
    OPEN_WEBUI_IMAGE) [[ "$value" =~ ^ghcr\.io/open-webui/open-webui:v[0-9]+\.[0-9]+\.[0-9]+@sha256:[a-f0-9]{64}$ ]] || k3s_die "$key must be a digest-pinned Open WebUI image" ;;
    RAG_EMBEDDING_REPOSITORY) [[ "$value" == Qwen/Qwen3-Embedding-0.6B ]] || k3s_die "$key must select the reviewed Qwen embedding model" ;;
    STEAM_HEADLESS_BASE_IMAGE) [[ "$value" =~ ^docker\.io/josh5/steam-headless@sha256:[a-f0-9]{64}$ ]] || k3s_die "$key must be a digest-pinned Steam-Headless base" ;;
    STEAM_HEADLESS_REVIEWED_SOURCE|SUNSHINE_COMMIT) [[ "$value" =~ ^[a-f0-9]{40}$ ]] || k3s_die "$key must be an exact source commit" ;;
    SUNSHINE_VERSION) [[ "$value" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || k3s_die "$key must be an exact Sunshine release" ;;
    SUNSHINE_DEBIAN_SHA256) [[ "$value" =~ ^[a-f0-9]{64}$ ]] || k3s_die "$key must be a SHA-256 digest" ;;
    SUNSHINE_MESA_DEBIAN_VERSION|KWIN_DEBIAN_VERSION) [[ "$value" =~ ^[0-9][a-zA-Z0-9.+~:-]+$ ]] || k3s_die "$key must be a Debian package version" ;;
    K3S_BINARY_SHA256|K3S_SELINUX_RPM_SHA256) [[ "$value" =~ ^[a-f0-9]{64}$ ]] || k3s_die "$key must be a SHA-256 digest" ;;
    ROCM_AUR_PKGBUILD_SHA256|ROCM_AUR_SOURCE_SHA256) [[ "$value" =~ ^[a-f0-9]{64}$ ]] || k3s_die "$key must be a SHA-256 digest" ;;
    ROCM_AUR_COMMIT) [[ "$value" =~ ^[a-f0-9]{40}$ ]] || k3s_die "$key must be an exact AUR commit" ;;
    SGLANG_ROCM_INDEX_DIGEST|SGLANG_ROCM_CONFIG_DIGEST) [[ "$value" =~ ^sha256:[a-f0-9]{64}$ ]] || k3s_die "$key must be an OCI SHA-256 digest" ;;
    SGLANG_ROCM_IMAGE) [[ "$value" =~ ^docker\.io/rocm/sgl-dev:[A-Za-z0-9._-]+@sha256:[a-f0-9]{64}$ ]] || k3s_die "$key must be a digest-pinned AMD SGLang image" ;;
    LAB_NAME) [[ "$value" =~ ^[a-z][a-z0-9-]{0,62}$ ]] || k3s_die "$key must be a DNS label" ;;
    ADMIN_USER) [[ "$value" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || k3s_die "$key must be a local user name" ;;
    SSH_PUBLIC_KEY_FILE|SSH_PRIVATE_KEY_FILE|SSH_KNOWN_HOSTS_FILE) [[ "$value" == /* && "$value" != *..* ]] || k3s_die "$key must be a safe absolute path" ;;
    MGMT_NETWORK_NAME) [[ "$value" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$ ]] || k3s_die "$key is invalid" ;;
    MGMT_BRIDGE|DMZ_PARENT|DMZ_VLAN_INTERFACE|DMZ_BRIDGE|MGMT_INTERFACE|DMZ_INTERFACE) [[ "$value" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,14}$ ]] || k3s_die "$key must be a Linux interface name" ;;
    VM_VCPUS|VM_MEMORY_MIB|VM_SYSTEM_DISK_GIB|VM_DATA_DISK_GIB) [[ "$value" =~ ^[1-9][0-9]*$ ]] || k3s_die "$key must be a positive integer" ;;
    MGMT_MAC|DMZ_MAC) [[ "$value" =~ ^[[:xdigit:]]{2}(:[[:xdigit:]]{2}){5}$ ]] || k3s_die "$key must be a MAC address" ;;
    MGMT_CIDR|CLUSTER_CIDR|SERVICE_CIDR|DMZ_IPV4) [[ "$value" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/[0-9]{1,2}$ ]] || k3s_die "$key must be an IPv4 CIDR" ;;
    MGMT_GATEWAY|MGMT_IP|DMZ_IPV4_GATEWAY) [[ "$value" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || k3s_die "$key must be an IPv4 address" ;;
    DMZ_IPV6) [[ "$value" =~ ^[0-9a-fA-F:]+/64$ && "$value" != *:::* ]] || k3s_die "$key must be an IPv6 /64 without an invalid triple colon" ;;
    DMZ_IPV6_GATEWAY) [[ "$value" =~ ^[0-9a-fA-F:]+$ && "$value" != *:::* ]] || k3s_die "$key must be an IPv6 address" ;;
    DMZ_VLAN_ID) [[ "$value" =~ ^([1-9]|[1-9][0-9]{1,3}|[1-3][0-9]{3}|40[0-8][0-9]|409[0-4])$ ]] || k3s_die "$key must be a VLAN ID from 1 to 4094" ;;
    K3S_MINOR) [[ "$value" =~ ^[0-9]+\.[0-9]+$ ]] || k3s_die "$key must be a K3s major.minor value" ;;
    K3S_SELINUX_RPM_VERSION) [[ "$value" =~ ^[0-9]+\.[0-9]+-[0-9]+\.el9$ ]] || k3s_die "$key must be an exact EL9 SELinux policy RPM version" ;;
    ALMALINUX_IMAGE_NAME) [[ "$value" =~ ^AlmaLinux-9-GenericCloud-[A-Za-z0-9._-]+\.x86_64\.qcow2$ && "$value" != *..* ]] || k3s_die "$key must be a safe AlmaLinux 9 GenericCloud qcow2 basename" ;;
    ALMALINUX_IMAGE_SHA256|K3S_RPM_GPG_KEY_SHA256|CERT_MANAGER_MANIFEST_SHA256|LOCAL_PATH_PROVISIONER_MANIFEST_SHA256) [[ "$value" =~ ^[[:xdigit:]]{64}$ ]] || k3s_die "$key must be a SHA-256 digest" ;;
    ALMALINUX_SIGNING_FINGERPRINT|K3S_RPM_GPG_FINGERPRINT) [[ "$value" =~ ^[[:xdigit:]]{40}$ ]] || k3s_die "$key must be a 40-character signing fingerprint" ;;
    MGMT_FQDN|DOMAIN) [[ "$value" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]$ ]] || k3s_die "$key must be a DNS name" ;;
    ACME_EMAIL) [[ "$value" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] || k3s_die "$key must be an email address" ;;
    ROUTE53_ZONE_ID) [[ "$value" =~ ^Z[A-Z0-9]{10,31}$ ]] || k3s_die "$key must be a Route53 hosted-zone ID" ;;
    AWS_REGION) [[ "$value" =~ ^[a-z]{2}(-gov)?-[a-z0-9-]+-[0-9]+$ ]] || k3s_die "$key must be an AWS region" ;;
    RESTIC_BUCKET) [[ "$value" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] || k3s_die "$key must be a DNS-compatible S3 bucket name" ;;
  esac
}

k3s_ipv4_to_int() {
  local address="$1" a b c d extra
  IFS=. read -r a b c d extra <<< "$address"
  [[ -n "$a" && -n "$b" && -n "$c" && -n "$d" && -z "${extra:-}" ]] || return 1
  for octet in "$a" "$b" "$c" "$d"; do
    [[ "$octet" =~ ^[0-9]{1,3}$ ]] || return 1
    ((10#$octet <= 255)) || return 1
  done
  printf '%u\n' "$(((10#$a << 24) | (10#$b << 16) | (10#$c << 8) | 10#$d))"
}

k3s_cidr_range() {
  local cidr="$1" address prefix integer mask network broadcast
  address=${cidr%/*}; prefix=${cidr#*/}
  [[ "$prefix" =~ ^[0-9]+$ && "$prefix" -le 32 ]] || return 1
  integer=$(k3s_ipv4_to_int "$address") || return 1
  if ((prefix == 0)); then mask=0; else mask=$(((0xffffffff << (32 - prefix)) & 0xffffffff)); fi
  network=$((integer & mask)); broadcast=$((network | (0xffffffff ^ mask)))
  printf '%u %u\n' "$network" "$broadcast"
}

k3s_cidrs_overlap() {
  local first_start first_end second_start second_end
  read -r first_start first_end <<< "$(k3s_cidr_range "$1")" || return 2
  read -r second_start second_end <<< "$(k3s_cidr_range "$2")" || return 2
  ((first_start <= second_end && second_start <= first_end))
}

k3s_ip_in_cidr() {
  local integer start end
  integer=$(k3s_ipv4_to_int "$1") || return 1
  read -r start end <<< "$(k3s_cidr_range "$2")" || return 1
  ((integer >= start && integer <= end))
}

k3s_ip_is_host() {
  local integer start end
  integer=$(k3s_ipv4_to_int "$1") || return 1
  read -r start end <<< "$(k3s_cidr_range "$2")" || return 1
  ((integer > start && integer < end))
}

k3s_service_dns() {
  local start end address
  read -r start end <<< "$(k3s_cidr_range "$1")" || return 1
  address=$((start + 10))
  ((address < end)) || return 1
  printf '%d.%d.%d.%d\n' "$(((address >> 24) & 255))" "$(((address >> 16) & 255))" "$(((address >> 8) & 255))" "$((address & 255))"
}

# Parse simple KEY=VALUE files without evaluating them. Values are deliberately
# unquoted: shell expansions, backticks, whitespace, and duplicates are errors.
k3s_load_kv_file() {
  local file="$1" line key value lineno=0 safe_value_pattern='^[A-Za-z0-9._/@:+?=&%~-]+$'
  [ -r "$file" ] || k3s_die "cannot read configuration: $file"
  while IFS= read -r line || [ -n "$line" ]; do
    lineno=$((lineno + 1))
    case "$line" in ''|'#'*) continue ;; esac
    [[ "$line" =~ ^([A-Z0-9_]+)=([^[:space:]]+)$ ]] || k3s_die "invalid configuration line $lineno in $file"
    key="${BASH_REMATCH[1]}"; value="${BASH_REMATCH[2]}"
    k3s_allowed_key "$key" || k3s_die "unrecognised configuration key $key in $file"
    [[ "$value" =~ $safe_value_pattern ]] || k3s_die "unsafe value for $key in $file"
    [ -z "${!key+x}" ] || k3s_die "duplicate configuration key: $key"
    k3s_validate_value "$key" "$value"
    printf -v "$key" '%s' "$value"
  done < "$file"
}

k3s_load_config() {
  local config_file="$1" lock_file="$2"
  unset QWEN_CODE_VERSION QWEN_CODE_COMMIT DSH_COMMIT HERMES_AGENT_COMMIT
  unset K3S_SELINUX_RPM_SHA256
  unset OPEN_WEBUI_VERSION OPEN_WEBUI_COMMIT OPEN_WEBUI_IMAGE RAG_EMBEDDING_REPOSITORY RAG_EMBEDDING_REVISION
  unset SGLANG_DUAL_MODEL_REPOSITORY SGLANG_DUAL_MODEL_REVISION SGLANG_SINGLE_MODEL_REPOSITORY SGLANG_SINGLE_MODEL_REVISION
  unset STEAM_HEADLESS_BASE_IMAGE STEAM_HEADLESS_REVIEWED_SOURCE SUNSHINE_VERSION SUNSHINE_COMMIT SUNSHINE_DEBIAN_SHA256 SUNSHINE_MESA_DEBIAN_VERSION KWIN_DEBIAN_VERSION
  unset ROCM_AUR_REPOSITORY ROCM_AUR_PACKAGE ROCM_AUR_PACKAGE_VERSION ROCM_AUR_COMMIT ROCM_AUR_PKGBUILD_SHA256 ROCM_AUR_SOURCE_URL ROCM_AUR_SOURCE_SHA256 SGLANG_ROCM_IMAGE SGLANG_ROCM_INDEX_DIGEST SGLANG_ROCM_CONFIG_DIGEST
  unset ROCM_THEROCK_REPOSITORY ROCM_THEROCK_COMMIT ROCM_LLAMA_CPP_REPOSITORY ROCM_LLAMA_CPP_COMMIT K3S_BINARY_SHA256
  unset LAB_NAME ADMIN_USER SSH_PUBLIC_KEY_FILE SSH_PRIVATE_KEY_FILE SSH_KNOWN_HOSTS_FILE VM_VCPUS VM_MEMORY_MIB VM_SYSTEM_DISK_GIB VM_DATA_DISK_GIB VM_POOL_DIR MGMT_NETWORK_NAME MGMT_BRIDGE MGMT_CIDR MGMT_GATEWAY MGMT_IP MGMT_FQDN MGMT_MAC DMZ_PARENT DMZ_VLAN_ID DMZ_VLAN_INTERFACE DMZ_BRIDGE DMZ_IPV4 DMZ_IPV4_GATEWAY DMZ_IPV6 DMZ_IPV6_GATEWAY DMZ_MAC MGMT_INTERFACE DMZ_INTERFACE CLUSTER_CIDR SERVICE_CIDR DOMAIN ACME_EMAIL ROUTE53_ZONE_ID AWS_REGION RESTIC_BUCKET LOCK_FORMAT LOCK_DATE ALMALINUX_IMAGE_NAME ALMALINUX_IMAGE_URL ALMALINUX_IMAGE_SHA256 ALMALINUX_CHECKSUM_URL ALMALINUX_CHECKSUM_SIGNATURE_URL ALMALINUX_SIGNING_FINGERPRINT K3S_VERSION K3S_BINARY_SHA256 K3S_MINOR K3S_SELINUX_RPM_VERSION K3S_RPM_GPG_KEY_URL K3S_RPM_GPG_KEY_SHA256 K3S_RPM_GPG_FINGERPRINT CERT_MANAGER_VERSION CERT_MANAGER_MANIFEST_URL CERT_MANAGER_MANIFEST_SHA256 LOCAL_PATH_PROVISIONER_VERSION LOCAL_PATH_PROVISIONER_MANIFEST_URL LOCAL_PATH_PROVISIONER_MANIFEST_SHA256 LOCAL_PATH_HELPER_IMAGE LLM_SCALER_IMAGE OMZ_REPOSITORY OMZ_COMMIT JETBRAINS_TOOLBOX_VERSION JETBRAINS_TOOLBOX_URL JETBRAINS_TOOLBOX_SHA256 PARU_AUR_REPOSITORY PARU_AUR_COMMIT LINUX_GIT_AUR_REPOSITORY LINUX_GIT_AUR_COMMIT LINUX_GIT_SOURCE_REPOSITORY LINUX_GIT_SOURCE_COMMIT AWS_SSM_AUR_REPOSITORY AWS_SSM_AUR_COMMIT
  k3s_load_kv_file "$config_file"
  k3s_load_kv_file "$lock_file"
  K3S_CONFIG_SOURCE="$(cd -- "$(dirname -- "$config_file")" && pwd -P)/$(basename -- "$config_file")"
  K3S_LOCK_SOURCE="$(cd -- "$(dirname -- "$lock_file")" && pwd -P)/$(basename -- "$lock_file")"
  # These values are consumed by the separate lifecycle module.
  : "$K3S_CONFIG_SOURCE" "$K3S_LOCK_SOURCE"
  : "${VM_VCPUS:=8}"; : "${VM_MEMORY_MIB:=16384}"; : "${VM_SYSTEM_DISK_GIB:=80}"; : "${VM_DATA_DISK_GIB:=120}"
  : "${MGMT_NETWORK_NAME:=k3s-mgmt}"; : "${CLUSTER_CIDR:=10.42.0.0/16}"; : "${SERVICE_CIDR:=10.43.0.0/16}"
  k3s_require_config_values
}

k3s_require_config_values() {
  local key expected_minor mgmt_host_octet
  [[ -n ${K3S_BINARY_SHA256:-} && -n ${K3S_SELINUX_RPM_SHA256:-} ]] || k3s_die 'K3s binary and SELinux RPM checksums are required'
  for key in LAB_NAME ADMIN_USER SSH_PUBLIC_KEY_FILE SSH_PRIVATE_KEY_FILE SSH_KNOWN_HOSTS_FILE VM_POOL_DIR MGMT_NETWORK_NAME MGMT_BRIDGE MGMT_CIDR MGMT_GATEWAY MGMT_IP MGMT_FQDN MGMT_MAC DMZ_PARENT DMZ_VLAN_ID DMZ_VLAN_INTERFACE DMZ_BRIDGE DMZ_IPV4 DMZ_IPV4_GATEWAY DMZ_IPV6 DMZ_IPV6_GATEWAY DMZ_MAC MGMT_INTERFACE DMZ_INTERFACE CLUSTER_CIDR SERVICE_CIDR DOMAIN ACME_EMAIL ROUTE53_ZONE_ID AWS_REGION RESTIC_BUCKET ALMALINUX_IMAGE_NAME ALMALINUX_IMAGE_URL ALMALINUX_IMAGE_SHA256 ALMALINUX_CHECKSUM_URL ALMALINUX_CHECKSUM_SIGNATURE_URL ALMALINUX_SIGNING_FINGERPRINT K3S_VERSION K3S_MINOR K3S_SELINUX_RPM_VERSION K3S_RPM_GPG_KEY_URL K3S_RPM_GPG_KEY_SHA256 K3S_RPM_GPG_FINGERPRINT CERT_MANAGER_VERSION CERT_MANAGER_MANIFEST_URL CERT_MANAGER_MANIFEST_SHA256 LOCAL_PATH_PROVISIONER_VERSION LOCAL_PATH_PROVISIONER_MANIFEST_URL LOCAL_PATH_PROVISIONER_MANIFEST_SHA256; do
    [ -n "${!key:-}" ] || k3s_die "configuration value is required: $key"
  done
  [ "$LOCK_FORMAT" = 1 ] || k3s_die 'unsupported versions.lock format'
  [[ "$LOCK_DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || k3s_die 'LOCK_DATE is invalid'
  [[ "$MGMT_CIDR" =~ /24$ ]] || k3s_die 'MGMT_CIDR currently must be an IPv4 /24 for the libvirt NAT network'
  [[ "$SSH_PUBLIC_KEY_FILE" != "$SSH_PRIVATE_KEY_FILE" ]] || k3s_die 'public and private SSH key paths must differ'
  [[ "$VM_POOL_DIR" == "/var/lib/libvirt/images/$LAB_NAME" ]] || k3s_die 'VM_POOL_DIR must be /var/lib/libvirt/images/LAB_NAME'
  [[ "$MGMT_IP" != */* && "$MGMT_GATEWAY" != */* ]] || k3s_die 'MGMT_IP and MGMT_GATEWAY must be plain IPv4 addresses'
  [[ "$VM_VCPUS" == 8 && "$VM_MEMORY_MIB" == 16384 && "$VM_SYSTEM_DISK_GIB" == 80 && "$VM_DATA_DISK_GIB" == 120 ]] \
    || k3s_die 'the initial lab shape must remain 8 vCPU, 16 GiB RAM, and 80/120 GiB disks'
  [ -r "$SSH_PUBLIC_KEY_FILE" ] || k3s_die "SSH public key is unreadable: $SSH_PUBLIC_KEY_FILE"
  expected_minor="${K3S_VERSION#v}"; expected_minor="${expected_minor%.*}"; expected_minor="${expected_minor%%+*}"
  [ "$K3S_MINOR" = "$expected_minor" ] || k3s_die 'K3S_MINOR does not match K3S_VERSION'
  [[ ${#MGMT_BRIDGE} -le 15 && ${#DMZ_BRIDGE} -le 15 && ${#DMZ_VLAN_INTERFACE} -le 15 ]] || k3s_die 'host interface names must not exceed 15 characters'
  k3s_ip_is_host "$MGMT_IP" "$MGMT_CIDR" || k3s_die 'MGMT_IP is not a usable host address in MGMT_CIDR'
  k3s_ip_is_host "$MGMT_GATEWAY" "$MGMT_CIDR" || k3s_die 'MGMT_GATEWAY is not a usable host address in MGMT_CIDR'
  mgmt_host_octet=${MGMT_IP##*.}
  ((10#$mgmt_host_octet < 100 || 10#$mgmt_host_octet > 200)) \
    || k3s_die 'MGMT_IP must remain outside the managed libvirt DHCP range (.100-.200)'
  k3s_ip_is_host "${DMZ_IPV4%/*}" "$DMZ_IPV4" || k3s_die 'DMZ_IPV4 is not a usable host address'
  k3s_ip_is_host "$DMZ_IPV4_GATEWAY" "$DMZ_IPV4" || k3s_die 'DMZ_IPV4_GATEWAY is not a usable host address in DMZ_IPV4'
  [ "$MGMT_IP" != "$MGMT_GATEWAY" ] || k3s_die 'management node and gateway addresses must differ'
  [ "${DMZ_IPV4%/*}" != "$DMZ_IPV4_GATEWAY" ] || k3s_die 'DMZ node and gateway addresses must differ'
  [ "${DMZ_IPV6%/*}" != "$DMZ_IPV6_GATEWAY" ] || k3s_die 'DMZ IPv6 node and gateway addresses must differ'
  [ "$MGMT_MAC" != "$DMZ_MAC" ] || k3s_die 'management and DMZ MAC addresses must differ'
  [ "$MGMT_INTERFACE" != "$DMZ_INTERFACE" ] || k3s_die 'guest interface names must differ'
  for first in "$MGMT_CIDR" "$CLUSTER_CIDR" "$SERVICE_CIDR" "$DMZ_IPV4"; do
    k3s_cidr_range "$first" >/dev/null || k3s_die "invalid IPv4 CIDR: $first"
  done
  K3S_CLUSTER_DNS=$(k3s_service_dns "$SERVICE_CIDR") || k3s_die 'SERVICE_CIDR must contain a usable network-plus-ten DNS address'
  : "$K3S_CLUSTER_DNS" # consumed by k3s_write_ansible_vars in lifecycle.sh
  k3s_cidrs_overlap "$MGMT_CIDR" "$CLUSTER_CIDR" && k3s_die 'management and Pod CIDRs overlap'
  k3s_cidrs_overlap "$MGMT_CIDR" "$SERVICE_CIDR" && k3s_die 'management and Service CIDRs overlap'
  k3s_cidrs_overlap "$CLUSTER_CIDR" "$SERVICE_CIDR" && k3s_die 'Pod and Service CIDRs overlap'
  k3s_cidrs_overlap "$MGMT_CIDR" "$DMZ_IPV4" && k3s_die 'management and DMZ CIDRs overlap'
  k3s_cidrs_overlap "$DMZ_IPV4" "$CLUSTER_CIDR" && k3s_die 'DMZ and Pod CIDRs overlap'
  k3s_cidrs_overlap "$DMZ_IPV4" "$SERVICE_CIDR" && k3s_die 'DMZ and Service CIDRs overlap'
  return 0
}
