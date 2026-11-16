#!/usr/bin/env bash
# Fake kubectl for the offline test of make-sandbox-config.sh: read-only answers only.
set -euo pipefail
case "$*" in
  "get servicecidrs -o json") echo '{"items":[{"metadata":{"name":"kubernetes"},"spec":{"cidrs":["10.43.0.0/16"]}}]}' ;;
  "get services -A -o json") echo "{\"items\":[{\"spec\":{\"clusterIP\":\"10.43.0.1\"}},{\"spec\":{\"clusterIP\":\"10.43.0.10\"}},{\"spec\":{\"clusterIP\":\"${FAKE_TAKEN:-10.43.99.1}\"}}]}" ;;
  "get ipaddresses.networking.k8s.io -o json") echo "{\"items\":[{\"metadata\":{\"name\":\"10.43.0.1\"}},{\"metadata\":{\"name\":\"${FAKE_TAKEN:-10.43.99.1}\"}}]}" ;;
  "get ipaddresses.networking.k8s.io "*)
    [[ $4 == "${FAKE_TAKEN:-10.43.99.1}" ]] && exit 0
    echo "NotFound" >&2
    exit 1
    ;;
  "-n default get endpointslices -l kubernetes.io/service-name=kubernetes -o json")
    echo '{"items":[{"addressType":"IPv4","endpoints":[{"addresses":["10.0.30.11"]}],"ports":[{"name":"https","port":6443}]}]}'
    ;;
  *)
    echo "fake kubectl: refusing $*" >&2
    exit 2
    ;;
esac
