# Official archlinux:base-20260830.0.582275, linux/amd64 manifest.
# https://github.com/docker-library/repo-info/blob/master/repos/archlinux/remote/base-20260830.0.582275.md
FROM docker.io/library/archlinux@sha256:818793c894d94534c22f2149154a39ebaee57e4e67321023b0866a1d5722036c

COPY versions.lock docker/setup.sh docker/entrypoint.sh /opt/arch-workstation-builder/
COPY --chmod=0755 docker/pacstrap.sh /opt/arch-workstation-builder/bin/pacstrap
COPY docker/bridge-build.sh /opt/arch-workstation-builder/bridge-build.sh
# Pacman's temporary local trust key must never enter an image layer.
RUN --mount=type=tmpfs,target=/etc/pacman.d/gnupg \
    bash /opt/arch-workstation-builder/setup.sh

USER 1000:1000
WORKDIR /work
ENTRYPOINT ["/usr/bin/bash", "/opt/arch-workstation-builder/entrypoint.sh"]
CMD ["check"]
