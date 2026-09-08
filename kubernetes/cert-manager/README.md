# Route53 DNS-01

Install cert-manager only through `kubernetes/install-pinned-addons.sh`. It
verifies the release bytes against `versions.lock` before applying them.

The Route53 access key is an interactive, manual operation. Create a dedicated
least-privilege IAM principal for the delegated `_acme-challenge` hosted zone,
then create its Kubernetes Secret using a terminal that does not log command
history. Do not commit a Secret manifest, a key, an account key, or a
kubeconfig. Apply a ClusterIssuer only after the Secret exists and ACME staging
has succeeded.
