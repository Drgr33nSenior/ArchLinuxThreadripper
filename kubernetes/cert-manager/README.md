# cert-manager and DNS-01 issuers

Install cert-manager only through `kubernetes/install-pinned-addons.sh`. It
verifies the release bytes against `versions.lock` (currently v1.21.1) before
applying them. On the bare-metal workstation cluster, add
`--component cert-manager`: the full add-on set also replaces the `local-path`
StorageClass, which K3s already provides there.

Two issuer templates exist. Neither contains a credential, and each is applied
only after its credential Secret exists:

| Template | Cluster | Credential Secret (`cert-manager` namespace) |
| --- | --- | --- |
| `route53-cluster-issuer.yaml.tmpl` | Separate VM lab | `route53-dns01-credentials` |
| `cloudflare-cluster-issuers.yaml.tmpl` | Workstation AI service | `cloudflare-dns01-api-token`, key `api-token` |

## Route53 (VM lab)

The Route53 access key is an interactive, manual operation. Create a dedicated
least-privilege IAM principal for the delegated `_acme-challenge` hosted zone,
then create its Kubernetes Secret using a terminal that does not log command
history. Do not commit a Secret manifest, a key, an account key, or a
kubeconfig. Apply a ClusterIssuer only after the Secret exists and ACME staging
has succeeded.

## Cloudflare (workstation AI service)

The template defines `letsencrypt-staging-cloudflare` and
`letsencrypt-production-cloudflare`. `workstationctl service render` writes the
matching Certificates. `certificate-staging.yaml` issues into
`<tls_secret>-staging`. `certificate.yaml` issues into `<tls_secret>`, the
Secret that the Ingresses serve. A staging certificate never reaches the serving
Secret. The procedure is in [AI-SERVICE.md](../../docs/AI-SERVICE.md#prepare-and-roll-out).

**Token.** In Cloudflare, create a custom API token with the permissions
*Zone → DNS → Edit* and *Zone → Zone → Read*. Set *Zone Resources* to
*Include → Specific zone → the owner's zone*. cert-manager uses Zone Read only
to look up the zone ID. Without it, issuance fails with a zone-list permission
error. Do not use the Global API Key. Do not reuse the NAS's Certbot
credential. The NAS keeps its own certificate and private key. Optionally limit
the token by client IP address and give it an expiry. A token expiry needs an
owner reminder to rotate it.

**Secret.** Write the token to a mode-0600 file. Use an editor or paste from the
clipboard, not `echo` and not a command argument. Then create the Secret from
that file:

```sh
umask 077
kubectl --context "$SERVICE_CONTEXT" -n cert-manager create secret generic cloudflare-dns01-api-token \
  --from-file=api-token=/private/cloudflare-dns01-api-token
```

The file must contain only the token, with no trailing newline. To check this
without printing any part of the token, run `tail -c1 FILE | wc -l`. The
command must print `0`. Remove the file after the Secret has been created, or keep it only in
the owner's encrypted credential store. Do not commit the file, a Secret
manifest or the ACME account keys.

**Issuers.** Render the issuers into a private path, check them and apply them:

```sh
sed -e 's/__ACME_EMAIL__/OWNER_ACME_EMAIL/' -e 's/__DNS_ZONE__/thespry.family/' \
  kubernetes/cert-manager/templates/cloudflare-cluster-issuers.yaml.tmpl > /private/cloudflare-cluster-issuers.yaml
! grep -n '__' /private/cloudflare-cluster-issuers.yaml
kubectl --context "$SERVICE_CONTEXT" apply -f /private/cloudflare-cluster-issuers.yaml
kubectl --context "$SERVICE_CONTEXT" wait --for=condition=Ready clusterissuer/letsencrypt-staging-cloudflare clusterissuer/letsencrypt-production-cloudflare --timeout=120s
```

**Renewal.** cert-manager renews each Certificate automatically. By default it
renews when two-thirds of the issued lifetime has passed, which is about day 60
of a 90-day Let's Encrypt certificate. `rotationPolicy: Always` creates a new
private key for each renewal. Traefik reloads the updated Secret without a
restart. Renewal uses the same Cloudflare token, so a revoked or expired token
causes renewal to fail.

**Failure behaviour.** A failed order is retried with exponential backoff. The
Secret keeps the current certificate until that certificate expires. Check
renewal state with these commands:

```sh
kubectl --context "$SERVICE_CONTEXT" -n ai-home-lab get certificate,certificaterequest,order,challenge
kubectl --context "$SERVICE_CONTEXT" -n ai-home-lab describe certificate ai-home-lab-tls
```

- A `zone list` or `403` error means the token lacks the correct permissions or
  is scoped to the wrong zone.
- `Waiting for DNS-01 challenge propagation` means the self-check cannot see
  the TXT record. Cluster DNS forwards to the site resolver. A split-horizon
  `lan.thespry.family` zone on that resolver can hide the public record. Fixing
  this is a separately reviewed change. Set cert-manager's
  `--dns01-recursive-nameservers-only` and `--dns01-recursive-nameservers`
  controller flags to public resolvers. Do not edit the locked upstream
  manifest in place.
- The rate limits for the production issuer are the reason that staging runs
  first. Repeated production failures can reach Let's Encrypt's limits for
  failed validations and for duplicate certificates.

If the production certificate expires, browsers and IDE clients reject HTTPS
and the service becomes unavailable. They must never fall back to insecure TLS.
cert-manager exports Certificate expiry and readiness metrics for alerting.
These metrics are not yet scraped by the workstation telemetry stack.
