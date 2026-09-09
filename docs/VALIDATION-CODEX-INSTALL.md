# Codex installation-console validation

Date: 2026-09-09. Checkout HEAD:
`67a506090e8ecf696190e0be55f865e3ce054d0e`, with preserved earlier uncommitted
changes and this installation-console increment. This is not a claim that the
committed revision alone contains the implementation. No commit or push was made.

## Scope and results

The change adds the locked Arch Codex package, a packaged installation skill,
an isolated owner-console launcher, a pre-erasure mirror/network check and
first-boot NetworkManager instructions. The installer, signing, disk confirmations
and recovery procedures remain authoritative. See [CODEX-INSTALL.md](CODEX-INSTALL.md)
for the complete owner sequence and disposable live-ISO acceptance procedure.

| Check | Observed result and boundary |
| --- | --- |
| `make check-strict` in the prepared private environment | Exit 0: 49 shell test files discovered, 42 Python cases, six Bats cases and three Ansible syntax checks; the three allowed skips below remain |
| Shell syntax, ShellCheck, shfmt, YAML and Kubernetes rendering | Passed through the strict suite; no live cluster access |
| Console PTY regressions | Five Python cases with subcases: API/device branches, login/client failures, INT/TERM/HUP, wrong platform, non-tmpfs, active swap, connectivity refusal and owned-session cleanup |
| Dummy-secret checks | No sentinel in captured terminal output or recorded arguments; no target copy; existing credentials preserved; login-status output suppressed |
| Network regressions | DNS, routes, time, TLS/mirror/archive failures refused; actual installer call path stopped before confirmation/erasure; first-boot NetworkManager and selected SSH behavior checked with mocks |
| Package fixtures | Explicit allowlist, executable modes, manifest, instruction adapter, exact dependency, source identity and no-Git skill layout passed |
| Skill checks | Metadata validator passed; 18 relative links resolved in both checkout and exported package tree |
| Actual pinned Linux CLI | `codex-cli 0.153.2`; official snapshot package checksum matched the lock; package staging and manifest verification passed |
| Actual credential lifecycle | Dummy key accepted by `login --with-api-key`; a separate `login status` process found the same private file store. This is not real account authentication |
| Actual packaged discovery | `app-server --strict-config --stdio` returned enabled, repository-scoped `workstation-install` from `/usr/lib/arch-workstation-bootstrap` under the staging prefix, without `.git` |

The strict suite ran on macOS with its existing prepared environment:

```sh
source test-results/validation-e5281734.JEFO5V/environment-ansible.sh
make check-strict
```

`HOME_LAB_PYTHON` selected that private environment's Python with Jinja2 and PyYAML.
Logs are private, ignored evidence at
`test-results/codex-install-research/check-strict-final.log` and
`test-results/strict-check.C18yxR/`. The strict run includes earlier uncommitted
regressions; not all of its test cases were added by this increment.

All skipped checks:

- Gaming image smoke: no `HOME_LAB_GAME_IMAGE` supplied.
- Isolated Wayland process tests: no `HOME_LAB_WAYLAND_RUNTIME_IMAGE` supplied.
- `systemd-analyze verify`: requires the Linux service environment.

The Ansible checks warned about an empty hosts list. They were syntax checks,
not deployments. The first strict attempt found two outdated test-fixture
assumptions (a shell array declaration and Docker package mocks); these were
corrected before the successful complete rerun. Its failed evidence was retained.

## Real CLI probe and sandbox limit

The pinned Arch package was extracted, not installed on the development host.
The actual package recipe staged files in a disposable, non-root amd64 Docker
container with networking disabled, dropped capabilities, read-only root and
private tmpfs. It used the existing image identity
`sha256:678881a88873e5d8ee4d365d489f63fd77a82d3587735e80428f1d33c0a93b66`.
The tested source archive hash was
`6cd3b58fa73b13132345abbf077a09e01e850964e0df15881bf52984fa70dd0e`.

The pinned CLI rejected the initially considered `untrusted` policy. The shipped
configuration instead uses supported `on-request` approval with `read-only`
sandboxing. Actual strict-config parsing and discovery accepted this configuration.

The restricted container reported that bubblewrap needs access to create user
namespaces. Therefore successful discovery does **not** qualify sandboxed tool
execution or interactive startup. Container restrictions were not relaxed.
Run the documented disposable live-ISO check; if its sandbox fails, stop instead
of disabling it. Probe output is retained in
`test-results/codex-install-research/actual-package-smoke-reviewed.log`.

## Continuity walkthroughs and remaining acceptance

Source/fixture walkthrough: wrong environment, mismatched serials, missing
signatures and failed preflight block the owner installation handoff. For an
interrupted installation, inspect actual disk/boot state and use the recovery
runbook; there is no resume command. Changed configuration or source identity
invalidates affected journal checks. Secret-bearing output stays owner-local.
A successful verification command records only its checked scope, not physical
recovery or workload qualification. Journal continuity is an instruction workflow,
not a new executable resume engine.

NOT RUN: real API-key authentication, ChatGPT device authorization, paid/model
requests, interactive live-ISO Codex sandbox use, complete newly signed ISO
assembly, disposable installation/UEFI recovery boots, physical Wi-Fi and
first-boot reconnection, Secure Boot/key enrolment, or workstation measurements.
No real credentials, host installation, disk erasure, firmware change or reboot
were used for these checks. The package checksum check is not a substitute for
the existing pacman and owner package-signature gates.

Backup/restore, signed ISO/UEFI acceptance and gaming input acceptance remain
separate deliverables. Use the numbered smoke procedure in CODEX-INSTALL for
the next owner-run checks; promote no hardware status based on these fixtures.
