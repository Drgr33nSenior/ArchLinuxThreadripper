#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Adapted from archlinux/archiso v90 configs/releng/profiledef.sh.
# shellcheck disable=SC2034
iso_name=arch-workstation
: "${SOURCE_DATE_EPOCH:?Use infrastructure/iso/build.sh with the reviewed release lock}"
iso_label="$(date -u --date="@$SOURCE_DATE_EPOCH" +ARCHLAB_%Y%m%d)"
iso_publisher='Private workstation project; not an official Arch Linux image'
iso_application='Workstation installer and recovery environment'
iso_version="$(date -u --date="@$SOURCE_DATE_EPOCH" +%Y.%m.%d)"
install_dir=arch
buildmodes=('iso')
bootmodes=('uefi.systemd-boot')
pacman_conf=pacman.conf
airootfs_image_type=squashfs
# Bound compression parallelism on the no-swap build host.
airootfs_image_tool_options=('-comp' 'xz' '-Xbcj' 'x86' '-b' '1M' '-processors' '4')
file_permissions=(
  ["/etc/shadow"]="0:0:400"
  ["/root"]="0:0:750"
  ["/root/.gnupg"]="0:0:700"
  ["/usr/local/bin/Installation_guide"]="0:0:755"
  ["/usr/local/bin/livecd-sound"]="0:0:755"
)
