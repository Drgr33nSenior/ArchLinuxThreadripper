# Primary references

This implementation was reviewed against these primary sources on the
`LOCK_DATE` in `versions.lock`. Recheck version-sensitive material before an
install or upgrade.

## Hardware and firmware

- [Gigabyte TRX50 AI TOP support and firmware](https://www.gigabyte.com/uk/Motherboard/TRX50-AI-TOP/support)
- [Gigabyte TRX50 AI TOP manual](https://download.gigabyte.com/FileList/Manual/mb_manual_trx50-ai-top_106_e.pdf)
- [AMD Threadripper 9960X specification](https://www.amd.com/en/products/processors/ryzen-threadripper/9000-series/amd-ryzen-threadripper-9960x.html)
- [Intel Arc Pro B70 specification](https://www.intel.com/content/www/us/en/products/sku/245797/intel-arc-pro-b70-graphics/specifications.html)
- [Intel Xe supported GPUs](https://dgpu-docs.intel.com/overview/supported-hardware/xe-driver-gpus.html)

## Arch, boot, and builds

- [Arch installation guide](https://wiki.archlinux.org/title/Installation_guide)
- [Arch mkinitcpio guidance](https://wiki.archlinux.org/title/Mkinitcpio)
- [Arch unified kernel images](https://wiki.archlinux.org/title/Unified_kernel_image)
- [Arch makepkg guidance](https://wiki.archlinux.org/title/Makepkg)
- [Arch clean-chroot builds](https://wiki.archlinux.org/title/DeveloperWiki:Building_in_a_clean_chroot)
- [Arch makechrootpkg manual](https://man.archlinux.org/man/makechrootpkg.1.en)
- [GCC x86 target options](https://gcc.gnu.org/onlinedocs/gcc/x86-Options.html)
- [systemd cryptenroll](https://www.freedesktop.org/software/systemd/man/latest/systemd-cryptenroll.html)
- [systemd credentials](https://www.freedesktop.org/software/systemd/man/latest/systemd-creds.html)

## Intel GPU and AI

- [Intel compute-runtime releases](https://github.com/intel/compute-runtime/releases)
- [PyTorch XPU getting started](https://docs.pytorch.org/docs/stable/notes/get_start_xpu.html)
- [Intel llm-scaler releases](https://github.com/intel/llm-scaler/releases)
- [Intel llm-scaler vLLM guidance](https://github.com/intel/llm-scaler/blob/main/vllm/README.md)
- [Podman run reference](https://docs.podman.io/en/latest/markdown/podman-run.1.html)

## AMD migration (inspected 2026-09-04)

- [Requested ROCm-X article](https://rocm.blogs.amd.com/ecosystems-and-partners/rocm-x-blog/README.html) — returned HTTP 429 during this audit; its contents were not assumed.
- [TheRock at the locked commit](https://github.com/ROCm/TheRock/tree/b927c1865f37fa7bbecf5c7e35dee41b02afbb4f)
- [Pinned TheRock environment and memory guidance](https://github.com/ROCm/TheRock/blob/b927c1865f37fa7bbecf5c7e35dee41b02afbb4f/docs/environment_setup_guide.md)
- [Pinned TheRock ccache setup](https://github.com/ROCm/TheRock/blob/b927c1865f37fa7bbecf5c7e35dee41b02afbb4f/build_tools/setup_ccache.py)
- [Pinned llama.cpp source](https://github.com/ggml-org/llama.cpp/tree/427291b5b34cd914a31b3fd3b61a68f6184f4b9f)
- [ccache manual](https://ccache.dev/manual/latest.html)
- [cryptsetup in-memory benchmark](https://man.archlinux.org/man/cryptsetup-benchmark.8.en)
- [Kernel x86 processor configuration](https://github.com/torvalds/linux/blob/master/arch/x86/Kconfig.cpu)
- [Arch ROCm PyTorch package](https://archlinux.org/packages/extra/x86_64/python-pytorch-opt-rocm/)

## KVM and K3s

- [libvirt domain XML](https://libvirt.org/formatdomain.html)
- [K3s requirements](https://docs.k3s.io/installation/requirements)
- [K3s networking](https://docs.k3s.io/networking/basic-network-options)
- [K3s hardening](https://docs.k3s.io/security/hardening-guide)
- [K3s backup and restore](https://docs.k3s.io/datastore/backup-restore)
- [K3s manual upgrades](https://docs.k3s.io/upgrades/manual)
- [AlmaLinux cloud images](https://wiki.almalinux.org/cloud/Generic-cloud.html)
- [Rancher Local Path Provisioner](https://github.com/rancher/local-path-provisioner)
- [cert-manager Route53 DNS-01](https://cert-manager.io/docs/configuration/acme/dns01/route53/)

## Shell and development clients

- [Oh My Zsh settings](https://github.com/ohmyzsh/ohmyzsh/wiki/Settings)
- [JetBrains Toolbox installation](https://www.jetbrains.com/help/toolbox-app/installation.html)
- [Codex CLI](https://learn.chatgpt.com/docs/codex/cli)
- [Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
- [AWS CLI installation and update guidance](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)
