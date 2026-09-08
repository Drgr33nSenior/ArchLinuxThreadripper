#!/usr/bin/env bash
# Render/check are offline. Apply is opt-in and binds to an explicit context.
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
lock_file="$repo_root/versions.lock"
mode=render
kubeconfig=''
context=''
confirm_context=''

usage() {
  cat <<'EOF'
Usage: install-pinned-addons.sh [render|check|apply] [--kubeconfig FILE --context NAME --confirm-context NAME]

render (default) renders only repository manifests. check downloads locked
add-on manifests, verifies SHA-256 checksums, and performs bounded local shape
checks without contacting a cluster.
apply requires all three explicit Kubernetes target arguments and a matching
confirmation value.
EOF
}

read_lock() {
  local key="$1"
  awk -F= -v key="$key" '$1 == key { print $2; found=1 } END { if (!found) exit 1 }' "$lock_file"
}

need() { command -v "$1" >/dev/null 2>&1 || { printf '%s is required\n' "$1" >&2; exit 1; }; }

validate_addon_metadata() {
  local prefix=$1 key=$2 path=$3 version digest url expected
  version=$(read_lock "${key}_VERSION")
  digest=$(read_lock "${key}_MANIFEST_SHA256")
  url=$(read_lock "${key}_MANIFEST_URL")
  [[ $version =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ && $digest =~ ^[a-f0-9]{64}$ ]] || { echo 'invalid add-on release lock' >&2; exit 1; }
  expected="${prefix}-${version//./-}-${digest:0:12}"
  # These are intentionally literal, checked-in YAML fields, not generated
  # cluster state. A release change must create a new immutable ConfigMap name.
  if ! { grep -Fqx "  name: $expected" "$path" &&
    grep -Fqx 'immutable: true' "$path" &&
    grep -Fqx "  version: $version" "$path" &&
    grep -Fqx "  manifest-url: $url" "$path" &&
    grep -Fqx "  manifest-sha256: $digest" "$path"; }; then
    printf 'metadata/name differs from versions.lock: %s\n' "$path" >&2; exit 1
  fi
}

while (($#)); do
  case "$1" in
    render|check|apply) mode="$1"; shift ;;
    --kubeconfig) (($# >= 2)) || { usage >&2; exit 2; }; kubeconfig=$2; shift 2 ;;
    --context) (($# >= 2)) || { usage >&2; exit 2; }; context=$2; shift 2 ;;
    --confirm-context) (($# >= 2)) || { usage >&2; exit 2; }; confirm_context=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

validate_addon_metadata cert-manager-install-lock CERT_MANAGER "$repo_root/kubernetes/cert-manager/install-metadata.yaml"
validate_addon_metadata local-path-provisioner-install-lock LOCAL_PATH_PROVISIONER "$repo_root/kubernetes/local-path/install-metadata.yaml"
need kubectl
case "$mode" in
  render)
    kubectl kustomize "$repo_root/kubernetes"
    exit 0
    ;;
  check|apply)
    need curl
    command -v sha256sum >/dev/null 2>&1 || command -v shasum >/dev/null 2>&1 \
      || { printf '%s\n' 'sha256sum or shasum is required' >&2; exit 1; }
    ;;
esac

if [ "$mode" = apply ]; then
  [ -r "$kubeconfig" ] || { echo 'apply requires a readable --kubeconfig file' >&2; exit 2; }
  [ -n "$context" ] && [ "$context" = "$confirm_context" ] || { echo 'apply requires matching --context and --confirm-context values' >&2; exit 2; }
  actual_context="$(kubectl --kubeconfig "$kubeconfig" config current-context)"
  [ "$actual_context" = "$context" ] || { echo 'kubeconfig current context does not match --context' >&2; exit 2; }
fi

download_locked_manifest() {
  local name="$1" url="$2" digest="$3" output="$4" actual
  curl --fail --location --proto '=https' --tlsv1.2 -o "$output" "$url"
  if command -v sha256sum >/dev/null 2>&1; then
    actual="$(sha256sum "$output" | awk '{ print $1 }')"
  else
    actual="$(shasum -a 256 "$output" | awk '{ print $1 }')"
  fi
  [ "$actual" = "$digest" ] || { printf 'SHA-256 mismatch for %s\n' "$name" >&2; exit 1; }
  printf 'verified %s\n' "$name"
}

temp_dir="$(mktemp -d)"
trap 'rm -rf -- "$temp_dir"' EXIT
cert_manager="$temp_dir/cert-manager.yaml"
local_path="$temp_dir/local-path.yaml"
local_path_merge_dir="$temp_dir/local-path-merge"
local_path_merged="$temp_dir/local-path-merged.yaml"
local_path_helper_image="$(read_lock LOCAL_PATH_HELPER_IMAGE)"
[[ "$local_path_helper_image" =~ ^[a-z0-9./:_-]+@sha256:[a-f0-9]{64}$ ]] \
  || { printf '%s\n' 'LOCAL_PATH_HELPER_IMAGE must use an immutable SHA-256 digest' >&2; exit 1; }
download_locked_manifest cert-manager "$(read_lock CERT_MANAGER_MANIFEST_URL)" "$(read_lock CERT_MANAGER_MANIFEST_SHA256)" "$cert_manager"
download_locked_manifest local-path-provisioner "$(read_lock LOCAL_PATH_PROVISIONER_MANIFEST_URL)" "$(read_lock LOCAL_PATH_PROVISIONER_MANIFEST_SHA256)" "$local_path"
mkdir -p "$local_path_merge_dir"
cp -- "$local_path" "$local_path_merge_dir/upstream.yaml"
cp -- "$repo_root/kubernetes/local-path/merge/kustomization.yaml" "$local_path_merge_dir/kustomization.yaml"
cp -- "$repo_root/kubernetes/local-path/namespace.yaml" "$local_path_merge_dir/namespace.yaml"
cp -- "$repo_root/kubernetes/local-path/configmap.yaml" "$local_path_merge_dir/configmap.yaml"
cp -- "$repo_root/kubernetes/local-path/storageclass.yaml" "$local_path_merge_dir/storageclass.yaml"
kubectl kustomize "$local_path_merge_dir" > "$local_path_merged"
kubectl kustomize "$repo_root/kubernetes" > "$temp_dir/workstation.yaml"
grep -Fq "image: $local_path_helper_image" "$local_path_merged" \
  || { printf '%s\n' 'rendered local-path helper image differs from versions.lock' >&2; exit 1; }

if [ "$mode" = check ]; then
  # Even client-side kubectl apply can perform REST discovery through an
  # ambient kubeconfig. Keep this mode genuinely offline: immutable checksums
  # establish the downloaded bytes and these assertions catch empty/wrong
  # release assets. Server-side validation belongs only to explicit apply.
  grep -Fq 'kind: CustomResourceDefinition' "$cert_manager"
  grep -Fq 'name: cert-manager' "$cert_manager"
  grep -Fq 'kind: Deployment' "$local_path"
  grep -Fq 'name: local-path-provisioner' "$local_path"
  grep -Fq 'reclaimPolicy: Retain' "$local_path_merged"
  grep -Fq 'kind: NetworkPolicy' "$temp_dir/workstation.yaml"
  printf 'check passed; no cluster mutation was requested\n'
  exit 0
fi

kubectl --kubeconfig "$kubeconfig" --context "$context" apply --server-side --force-conflicts \
  --field-manager=k3s-lab-local-path -f "$repo_root/kubernetes/local-path/namespace.yaml"
kubectl --kubeconfig "$kubeconfig" --context "$context" apply --server-side --field-manager=k3s-lab-cert-manager -f "$cert_manager"
kubectl --kubeconfig "$kubeconfig" --context "$context" apply --server-side --force-conflicts \
  --field-manager=k3s-lab-local-path -f "$local_path_merged"
kubectl --kubeconfig "$kubeconfig" --context "$context" apply --server-side --force-conflicts \
  --field-manager=k3s-lab-local-path -f "$repo_root/kubernetes/local-path/install-metadata.yaml"
kubectl --kubeconfig "$kubeconfig" --context "$context" apply --server-side --field-manager=k3s-lab-cert-manager -k "$repo_root/kubernetes/cert-manager"
kubectl --kubeconfig "$kubeconfig" --context "$context" apply --server-side --field-manager=k3s-lab-workstation -k "$repo_root/kubernetes/base"
