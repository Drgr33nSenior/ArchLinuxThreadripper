#!/usr/bin/env bash
# Reuse the VM lab's tested address arithmetic without configuring any network.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
source "$root/lib/k3s/common.sh"
(($# == 5)) || { printf 'Usage: check-network.sh NODE-IP LAN-CIDR POD-CIDR SERVICE-CIDR DNS-IP\n' >&2; exit 2; }
node=$1 lan=$2 pods=$3 services=$4 dns=$5
for cidr in "$lan" "$pods" "$services"; do
  [[ $cidr == */* ]] || k3s_die 'network CIDR requires a prefix'
  k3s_cidr_range "$cidr" >/dev/null || k3s_die 'invalid network CIDR'
done
k3s_ip_is_host "$node" "$lan" || k3s_die 'node IP is not a usable LAN address'
k3s_ip_is_host "$dns" "$services" || k3s_die 'cluster DNS is not a usable service address'
if k3s_cidrs_overlap "$lan" "$pods" || k3s_cidrs_overlap "$lan" "$services" || k3s_cidrs_overlap "$pods" "$services"; then
  k3s_die 'LAN, pod and service CIDRs must not overlap'
fi
private=false
for cidr in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16; do
  if k3s_ip_in_cidr "$node" "$cidr"; then private=true; fi
done
[[ $private == true ]] || k3s_die 'node IP must be on an RFC1918 private LAN'
printf 'Private, non-overlapping network inputs passed\n'
