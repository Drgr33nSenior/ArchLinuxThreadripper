#!/usr/bin/env bash
# The gaming seccomp profile is exactly the pinned upstream default plus the reviewed
# namespace syscalls, and the Deployment and Ansible role use that file.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
dir=$root/infrastructure/gaming/seccomp
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}
python3 -I "$dir/make-profile.py" "$dir/moby-default-v0.3.0.json" "$work/profile.json"
cmp -s "$work/profile.json" "$dir/workstation-gaming-userns.json" || fail 'committed profile is not the generated one; rerun make-profile.py'
profile=$dir/workstation-gaming-userns.json
[[ $(jq -r .defaultAction "$profile") == SCMP_ACT_ERRNO ]] || fail 'default action must stay deny'
# Plain OCI form: Kubernetes Localhost profiles never see Docker's archMap/includes/excludes.
jq -e '(has("archMap") | not) and all(.syscalls[]; (has("includes") or has("excludes") or has("comment")) | not)' "$profile" >/dev/null ||
  fail 'profile must be resolved OCI form'
jq -e '.architectures == ["SCMP_ARCH_X86_64","SCMP_ARCH_X86","SCMP_ARCH_X32"]' "$profile" >/dev/null || fail 'x86_64 with its 32-bit sub-architectures'
# Exactly one added rule, unconditional, with the reviewed names.
jq -e '[.syscalls[] | select(.names | index("unshare"))] | length == 1' "$profile" >/dev/null || fail 'expected one namespace rule'
jq -e '.syscalls[] | select(.names | index("unshare")) | .action == "SCMP_ACT_ALLOW" and (has("args") | not) and
  .names == ["clone","clone3","fsconfig","fsmount","fsopen","fspick","mount","mount_setattr","move_mount","open_tree","pivot_root","setdomainname","sethostname","setns","umount","umount2","unshare"]' \
  "$profile" >/dev/null || fail 'added rule changed'
# Capability-gated calls are absent (the container holds no capabilities), so the default denies them.
for name in bpf perf_event_open syslog quotactl fanotify_init lookup_dcookie reboot kexec_load init_module; do
  jq -e --arg n "$name" '[.syscalls[] | select(.names | index($n))] | length == 0' "$profile" >/dev/null ||
    fail "$name is reachable"
done
# Ordinary calls stay allowed, including 32-bit statx (Steam's ld-linux.so.2).
for name in statx openat read execve; do
  jq -e --arg n "$name" '[.syscalls[] | select(.action == "SCMP_ACT_ALLOW" and (.names | index($n)))] | length >= 1' "$profile" >/dev/null ||
    fail "$name is denied"
done
grep -Fq 'seccompProfile: {type: Localhost, localhostProfile: profiles/workstation-gaming-userns.json}' "$root/apps/base/steam-headless/deployment.yaml" ||
  fail 'gaming container does not use the profile'
grep -Fq 'dest: /var/lib/kubelet/seccomp/profiles/workstation-gaming-userns.json' "$root/infrastructure/ansible/roles/k3s_baremetal/tasks/main.yml" ||
  fail 'Ansible does not install the profile'
printf 'PASS: gaming seccomp profile is the pinned default plus the reviewed namespace syscalls\n'
