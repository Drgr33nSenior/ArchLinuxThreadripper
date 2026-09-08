#!/usr/bin/env bash
set -o errexit -o nounset -o pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
runtime="$repo_root/lib/workstation/runtime.sh"
lock="$repo_root/versions.lock"

grep -Eq '^LLM_SCALER_IMAGE=.*@sha256:[a-f0-9]{64}$' "$lock"
grep -Fq -- '--network=slirp4netns:allow_host_loopback=false' "$runtime"
grep -Fq -- '--cap-drop=all' "$runtime"
grep -Fq -- '--security-opt=no-new-privileges' "$runtime"
grep -Fq -- '--read-only' "$runtime"
grep -Fq -- '--userns=keep-id' "$runtime"
grep -Fq -- '--pull=never' "$runtime"
grep -Fq -- 'podman run -d --name workstation-b70-llm --replace --rm' "$runtime"
grep -Fq -- 'dst=/models,ro,rbind=false' "$runtime"
grep -Fq -- 'WORKSTATION_LLM_SHM_SIZE:-8g' "$runtime"
grep -Fq -- 'Speed 32GT/s.*Width x16' "$runtime"
grep -Fq -- 'expected 32 GiB large BAR' "$runtime"
grep -Fq -- "ws_gpu_validate \"\$bdf\" >/dev/null" "$runtime"
grep -Fq -- 'source /opt/intel/oneapi/setvars.sh --force' "$runtime"
grep -Fq -- '--workdir /llm --entrypoint /bin/bash' "$runtime"
grep -Fq -- 'LoadCredentialEncrypted=restic_repository:' "$repo_root/templates/workstation/restic/workstation-restic@.service"
grep -Fq -- 'LoadCredentialEncrypted=restic_password:' "$repo_root/templates/workstation/restic/workstation-restic@.service"
grep -Fq -- 'LoadCredentialEncrypted=aws_credentials:' "$repo_root/templates/workstation/restic/workstation-restic@.service"
grep -Fq -- 'forget --tag workstation --group-by host,paths,tags' "$repo_root/templates/workstation/restic/workstation-restic"
grep -Fxq -- '/var/lib/sbctl' "$repo_root/templates/workstation/restic/include"
grep -Fq -- 'LLM_LISTEN must remain 127.0.0.1:8000' "$runtime"
grep -Fq -- 'makechrootpkg -c -n -r' "$runtime"
grep -Fq -- 'clean-chroot directory must resolve below /var/lib/archbuild' "$runtime"
grep -Fq -- 'mkinitcpio -p workstation-linux-git' "$runtime"
grep -Fq -- 'git-kernel UKI is not signed by the configured Secure Boot db certificate' "$runtime"
grep -Fq -- 'libvirtd.socket' "$runtime"
grep -Fq -- 'qemu:///system' "$runtime"
grep -Fq -- 'iommu=pt' "$repo_root/templates/arch/kernel.cmdline"
if grep -q 'amd_iommu=on' "$repo_root/templates/arch/kernel.cmdline"; then exit 1; fi
grep -Fq -- 'status --porcelain=v1 --untracked-files=all' "$runtime"
grep -Eq '^LINUX_GIT_SOURCE_COMMIT=[a-f0-9]{40}$' "$lock"
if rg -n 'curl[^\n]*\|[[:space:]]*(sh|bash)|paru[^\n]*--noconfirm' "$repo_root/bin/workstationctl" "$runtime"; then
  printf '%s\n' 'unsafe bootstrap pattern found' >&2
  exit 1
fi
