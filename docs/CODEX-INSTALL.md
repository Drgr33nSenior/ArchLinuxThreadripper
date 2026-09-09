# Codex at the installation console

This is an optional assistant for the existing installer, not an autonomous
installation mode. Disk erasure, secrets, firmware trust and reboots remain
owner-run. No Kubernetes, Bridge, desktop keyring or Git checkout is required.

## Recorded software and package boundary

The [ISO lock](../infrastructure/iso/versions.lock) selects the official Arch
`openai-codex` package from the same dated snapshot as the ISO. On 2026-09-09,
the 2026-09-04 extra database supplied 0.153.2-1; its downloaded package matched
SHA-256 `a01a7705edb14181f00b70562f367c229e53cd7a552cecb54a45652e0a1131a4`.
The package declares bubblewrap, bzip2, glibc, libcap, libgcc, oniguruma, OpenSSL,
SQLite, xz, zlib and zstd dependencies. Pacman resolves these from that snapshot
and verifies Arch package signatures. Ripgrep is included; experimental Node/JS
tools are not required by this workflow. No npm/AUR installer runs at boot.

The builder checks the exact package version, cached package checksum and
`codex --version`. A changed snapshot needs a reviewed lock update and new build,
not an unpinned substitution. The bootstrap package depends on that exact Codex
package version and ships the launcher, skill, references and canonical
guardrails. Its generated `AGENTS.md` is a copy of those guardrails, not a second
policy. `SOURCE-MANIFEST.sha256` identifies packaged content; `BUILD-IDENTITY`
records the source revision (or explicitly `unknown`). Local changes are
identified by the manifest, not by the revision alone.

Use [ISO.md](ISO.md) unchanged in order: new package run, owner signing, ISO
assembly, artifact verification, disposable boot tests, then guarded USB writing.
Old signed packages do not acquire this workflow by retrying ISO assembly.

## 1. Connect without Codex

Boot the custom ISO in UEFI mode. At its local console, inspect interfaces and
connect with iwd. Substitute the discovered station and your SSID:

```sh
iwctl
device list
station wlan0 scan
station wlan0 get-networks
station wlan0 connect "YOUR SSID"
exit
timedatectl set-ntp true
timedatectl status
arch-workstation-network mirrors
```

`wlan0` is an example, not a selected hardware identity. Enter the password only
at iwctl's local password prompt. Do not use `--passphrase`, shell tracing,
terminal recording or paste credentials into Codex. If the radio is blocked,
inspect `rfkill list` and resolve the appropriate radio locally. Wired networking
is also usable. Captive portals, enterprise Wi-Fi and proxy setup require the
owner's network-specific procedure; the assistant does not bypass them.

The mirror check reads the effective `pacman-conf` repositories and servers.
It requires synchronized time, DNS, a route to the resolved host and an actual
readable repository database over certificate-verified HTTPS (or a configured
local repository). It tries alternative configured servers per repository.
HTTP-only/credential-bearing URLs are refused; no URL or password is logged.
It does not update pacman's database. It does not prove every package download
will finish or replace signature verification. The same check is now part of
installer preflight, including dry-run and execute mode before erase confirmation.
Failures stop installation; there is no skip-network flag.

## 2. Choose authentication explicitly

Choose **one** launcher command at the local console, outside an agent tool call:

```sh
arch-workstation-codex api-key
# Alternative, not an automatic fallback:
arch-workstation-codex device-code
```

API-key mode prompts with input hidden and pipes the unexported key to the pinned
CLI's `login --with-api-key`. API usage is billed through OpenAI Platform,
separately from a ChatGPT subscription. Device-code mode runs `login --device-auth`;
complete the displayed URL/code in a browser on another device. Account/workspace
settings may disable that flow. Failure does not switch billing methods.
See [OpenAI authentication](https://learn.chatgpt.com/docs/auth).

The launcher probes API or authentication-server connectivity without credentials
or inference. An expected API HTTP 401 establishes reachability, not valid login.
Transport/DNS/TLS errors are connectivity failures. Login failure is separate.
`login status` checks reuse by another process, but an API key can be stored
without proving model entitlement. The first successful owner-requested response
establishes API/model access and may incur usage charges. Report 401/403, quota,
rate-limit and model-access errors separately; never change the account/provider
to conceal one. No automated smoke test makes a paid request.

The same fresh `CODEX_HOME` and config serve login and the interactive client.
File credentials, logs, sessions and caches stay in a private 0700 directory on
`/run` (root) or `/run/user/UID` (named user), checked for tmpfs; configuration is
0600 and child files inherit umask 077. The runtime directory must exist and be
writable; no home-directory fallback is used. Active
swap is refused and core dumps are disabled. This is deliberate file persistence
between processes, not `ephemeral` authentication, which lasts only within one
process. Existing user credentials and the desktop configuration are untouched.
See [credential configuration](https://learn.chatgpt.com/docs/config-file/config-reference).

The client uses read-only sandboxing and `on-request` approval, including
when the live console is root. No bypass flag, automatic approval or new root
service is added. The pinned CLI rejects the older `untrusted` policy; it is not
used as a presumed safety setting. Do not approve destructive tool calls: execute the installer
yourself in another local console. If sandbox startup fails, stop; do not disable
it to proceed. Root still has broad read access: never ask the agent to inspect
the private session directory, iwd profiles, password prompts or recovery data.

Normal exit and INT/TERM/HUP clean the owned RAM session after stopping the
owned client. Reboot requires fresh authentication. SIGKILL/power loss cannot
run cleanup; RAM state is not a durable journal. After an abnormal launcher kill,
close remaining Codex processes and have the owner remove only that verified
session directory, or reboot when otherwise safe. Never copy a user's home,
`auth.json`, raw Codex sessions or login logs into the target or a journal.

## 3. Discover the skill and install

The launcher starts in `/usr/lib/arch-workstation-bootstrap`. This package tree
contains `.agents/skills/workstation-install/SKILL.md`; it needs no `.git` directory.
Open `/skills` and confirm `workstation-install` appears. Then enter:

```text
$workstation-install plan my rebuild using /run/install.conf
```

The owner first creates that nonsecret config from the packaged example:

```sh
cp /usr/lib/arch-workstation-bootstrap/config/install.conf.example /run/install.conf
nano /run/install.conf
bootstrap-arch --config /run/install.conf preflight
bootstrap-arch --config /run/install.conf --dry-run install
```

Use the home-lab example instead only if selecting its different unlock/host
policy. Resolve both by-id identities/serials locally. Review the complete layout,
components, network/signature prerequisites and backup plan. In a separate
owner console, after all prerequisites pass:

```sh
bootstrap-arch --config /run/install.conf --execute install
```

This command destroys the two confirmed disks. Keep its local confirmation and
password prompts out of Codex. Follow [INSTALLATION.md](INSTALLATION.md) for
offline headers, owner keys, manual enrolment, first boot and recovery testing.
There is no resume action. A partial install needs actual-state inspection and
the existing recovery procedure, not a repeated execute command.

## 4. First boot and optional local continuation

Wi-Fi credentials are **not** migrated from iwd. The installed system enables
NetworkManager; reconnect locally before expecting remote access:

```sh
nmcli device status
nmcli device wifi list
sudo nmcli --ask device wifi connect "YOUR SSID" ifname wlan0
timedatectl status
```

Use the newly discovered interface, not necessarily the live ISO's name. `--ask`
keeps the password out of arguments/history. NetworkManager stores its own
protected connection profile. It is not a copy of an iwd profile. If SSH was
selected with `ENABLE_SSH=true`, verify networking, `systemctl status sshd`, the
named user's access and host-key fingerprint locally before connecting remotely.
Root SSH remains prohibited. Live SSH remains masked regardless of that option.

You can continue using the controller's skill and sanitized local results without
installing Codex on the target. For optional target-console Codex, preserve the
same signed bootstrap package and signature from the ISO build on separate media.
Use the retained snapshot for `sudo pacman -Syu openai-codex`. Verify the exact
locked version with `pacman -Q openai-codex`. Have the owner inspect the bootstrap
signer's public fingerprint and establish its pacman trust explicitly, then run
`sudo pacman -U /path/to/arch-workstation-bootstrap-VERSION-any.pkg.tar.zst` with
its adjacent signature. Do not bypass `LocalFileSigLevel`. This reuses the signed
package; it does not run an installer scriptlet or copy live credentials.

Run `arch-workstation-codex api-key` or `device-code` as the named user, with fresh
authentication. Reintroduce only your nonsecret config/journal. Use:

```text
$workstation-install verify this installation using my external config and journal
```

The owner runs `sudo bootstrap-arch --config /path/to/install.conf verify` on the
installed root. Packaged helpers do not include the full post-install
`workstationctl` toolchain; use the reviewed full checkout for those optional
commands. Keep stable/LTS/recovery qualification separate from a verify exit code.
Before moving to rolling mirrors, review the optional bootstrap package's exact
Codex dependency: update/rebuild it coherently, or remove that optional bootstrap
package. Never remove `arch-workstation-boot`, which owns the UKI recovery hooks,
merely to upgrade Codex. Follow the existing full-upgrade/recovery policy.

## Continuity and private evidence

Use the skill's small nonsecret journal on the controller or separate storage,
outside both target disks and Git. Save the config there separately; `/run` is
lost on reboot. Record source/build identities, config fingerprint, artifact
references, checks and exit status, manual handoffs and the next safe action.
No credential/session copying or journal export automation is supplied.
After reconnecting, compare those records with the actual target and configuration.
Changed inputs invalidate affected checks. Unknown outcomes stay unknown. Do not
rebuild/resign an old run to regain conversational context.

## Disposable live-ISO smoke and acceptance

Follow ISO-REFERENCE's two file-backed disk VM procedure with outbound networking;
never pass host disks through. On the booted ISO, before installation:

1. Run `codex --version`, `pacman -Q openai-codex arch-workstation-bootstrap`, and
   inspect `BUILD-IDENTITY`. Match the release lock; verify the packaged manifest.
2. Run `arch-workstation-network mirrors` and the selected `codex-api` or
   `codex-device` probe. A wired VM does not qualify physical Wi-Fi.
3. Run the selected launcher locally. Owner authentication is optional/manual;
   no real credentials belong in automation. Confirm actual `/skills` discovery,
   interactive startup, read-only sandbox and approval policy. Request a harmless
   response only if you explicitly accept the account's usage charge.
4. Exit, repeat with INT/TERM, and verify that only the owned RAM session is removed
   without printing its contents. Confirm unrelated user auth remains unchanged.
5. Exercise disconnected DNS/mirrors and prove preflight refuses before erasure.
6. For the separately authorized disposable install, follow exact disk confirmations,
   then first-boot NetworkManager connection, fresh auth, verification and recovery
   tests. Do not infer that a VM Ethernet connection qualifies target Wi-Fi.

Record separately: source/fixture checks; actual package/CLI execution; login;
model access; ISO boot; Wi-Fi; installation; unlock/firmware/recovery. None of these
is a performance measurement. Remaining physical acceptance is owner-run.
