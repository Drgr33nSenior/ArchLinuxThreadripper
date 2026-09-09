# Optional Spry.ai Bridge session adapter v1

The standalone `workstationctl session` interface remains supported. If an
owner installs `/etc/workstation/session-policy.conf` as root:root 0600, it
becomes the canonical session state and cluster identity policy for every
caller. The parser accepts only these literal keys:

```ini
SESSION_STATE_DIRECTORY=/var/lib/workstation-session
SESSION_KUBECONFIG=/etc/bridge/helper.kubeconfig
```

The kubeconfig must be an explicit root-owned mode 0600 dedicated identity. A
direct CLI call that selects another state directory is refused. Existing
scripts without this optional policy retain their explicit-directory behavior.

The separate Go Bridge host executor owns the canonical flock across its full
typed operation, passes that already-held file as FD3, and sets
`WORKSTATION_SESSION_LOCK_FD=3` in its controlled environment. Session code
verifies the descriptor against the canonical lock's device/inode, retains FD9,
and performs the existing qualification, capacity, boot identity, unmanaged Pod,
termination and DRM checks. It cannot skip a gate through the inherited lock.
The outer executor retains build inhibition until it commits the operation's
final disposition. Ordinary direct CLI callers continue to control their own
existing build-gate behavior.

Session state replacement now syncs the temporary file, renames it, and syncs
the state directory with Linux `sync -f`. This improves crash durability without
changing the saved baseline or failure/restore semantics. It does not make
Kubernetes actions atomic and does not restore AI automatically after failure.

`tests/test_session.sh` covers the optional policy's state-directory refusal and
invalid inherited descriptor alongside existing failure/recovery fixtures.
`make check` is the repository's required check target. macOS fixtures replace
Linux sync and hardware calls; they do not qualify physical handover. Before
installation, review the Bridge runtime artifact manifest and complete its
target-machine qualification checklist. No service or policy is installed by
these source changes.

The Qwen client catalog was reconciled separately with Bridge `deb6a93` during
[installer 67a5060 revalidation](validation/VALIDATION-67a5060.md). Both source exports now
select Qwen Code 0.23.2 / `f56de980b316cd5410f067fbb62357481ebd66b8`. The pinned
cross-repository test compares complete native bundles and retains rejection of
stale pins. This does not authorize changes to root-owned installed runtime
manifests or owner-reviewed executable hashes.
