# K3s cluster source

This tree is source-only. Render it locally with `kubectl kustomize
kubernetes/`; do not run tests against a real cluster.

`public` has restricted Pod Security Admission and a default deny ingress and
egress policy. `dev` is deliberately baseline-only for local development.

`install-pinned-addons.sh` defaults to local `render`; `check` verifies the
locked upstream bytes without mutating a cluster. `apply` requires a readable
explicit kubeconfig, its explicit context, and a matching confirmation value.
It downloads the cert-manager and local-path manifests from the URL and
SHA-256 values in the root `versions.lock`. No Route53 credential is
represented in this repository.

Each immutable install-lock ConfigMap includes the release version and manifest
checksum prefix in its name. `render`, `check`, and `apply` reject metadata that
does not match `versions.lock`. For an upgrade, update the lock, metadata data,
and metadata name together. Applying the new release creates a new audit record;
it does not mutate the old immutable ConfigMap. Existing fixed-name records are
left intact. Remove obsolete records only through a separate reviewed operation.

The local-path override constrains volumes to the dedicated data-disk mount,
sets `Retain`, and removes it as a default StorageClass. Its dedicated system
namespace has a narrowly scoped privileged PSA enforcement label because its
helper Pods require node-local hostPath access; audit and warn remain
restricted. Application namespaces do not inherit that exception. The public
namespace defaults to deny; the included policies permit DNS and Traefik
ingress only.

The upstream local-path objects and the bounded overrides use distinct
server-side apply managers. The override takes ownership only of its declared
fields, so upstream setup and teardown data are not pruned. The helper Pod
template is deliberately owned by the override because its BusyBox image is
replaced with the digest-pinned reference in `versions.lock`.

The checksum-locked upstream manifests still name their release controller
images by exact version tags. Record the resolved image IDs after the first
pull and compare them during every add-on promotion; do not treat the tag alone
as immutable provenance.
