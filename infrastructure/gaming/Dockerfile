# Build via workstationctl sunshine image-build. No host mounts or driver install.
# The base and Sunshine asset are pinned; apt dependency versions are recorded,
# not claimed to be reproducible. Promote the resulting manifest digest, not a tag.
ARG BASE_IMAGE=scratch
FROM ${BASE_IMAGE} AS graphics
ARG SUNSHINE_SHA256
ARG MESA_VERSION
USER root
COPY sunshine.deb /tmp/workstation-sunshine.deb
COPY infrastructure/gaming/trixie-backports.sources /etc/apt/sources.list.d/workstation-backports.sources
RUN printf '%s  %s\n' "$SUNSHINE_SHA256" /tmp/workstation-sunshine.deb | sha256sum -c - \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends -t trixie-backports \
       /tmp/workstation-sunshine.deb jq vainfo ffmpeg vulkan-tools \
       "mesa-vulkan-drivers:amd64=$MESA_VERSION" "mesa-vulkan-drivers:i386=$MESA_VERSION" \
       "mesa-libgallium:amd64=$MESA_VERSION" "mesa-libgallium:i386=$MESA_VERSION" \
       "libgl1-mesa-dri:amd64=$MESA_VERSION" "libgl1-mesa-dri:i386=$MESA_VERSION" \
       "libegl-mesa0:amd64=$MESA_VERSION" "libegl-mesa0:i386=$MESA_VERSION" \
       "libglx-mesa0:amd64=$MESA_VERSION" "libglx-mesa0:i386=$MESA_VERSION" \
       "libgbm1:amd64=$MESA_VERSION" "libgbm1:i386=$MESA_VERSION" \
    && mkdir -p /usr/lib/workstation /usr/share/workstation \
    && dpkg-query -W '-f=${binary:Package}\t${Version}\n' > /usr/share/workstation/gaming-packages.tsv \
    && mv /usr/bin/sunshine /usr/lib/workstation/sunshine.real \
    && if getcap /usr/lib/workstation/sunshine.real | grep -q .; then setcap -r /usr/lib/workstation/sunshine.real; fi \
    && rm /tmp/workstation-sunshine.deb
COPY bin /opt/workstation/bin
COPY lib /opt/workstation/lib
COPY templates /opt/workstation/templates
COPY versions.lock /opt/workstation/versions.lock
RUN chmod -R a+rX /opt/workstation
COPY --chmod=755 infrastructure/gaming/sunshine-wrapper.sh /usr/bin/sunshine
COPY --chmod=755 infrastructure/gaming/60-configure_gpu_driver.sh /etc/cont-init.d/60-configure_gpu_driver.sh
# Explicit rollback only. This retains the old rootful Xorg/XFCE entrypoint.
FROM graphics AS x11
ENV SUNSHINE_CAPTURE=x11 SUNSHINE_ENCODER=vaapi
LABEL workstation.ai.qualification="pending-display-input-and-security"

# Default: keep the Steam runtime, but replace its entire session lifecycle.
# KWin's virtual backend uses the sole accessible render node; it needs no DRM
# master, Xorg server, host display socket, systemd or privileged container.
FROM graphics AS wayland
ARG KWIN_DEBIAN_VERSION
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
       "kwin-wayland=$KWIN_DEBIAN_VERSION" xwayland wayland-utils \
       pipewire pipewire-pulse wireplumber dbus-user-session \
       xdg-desktop-portal xdg-desktop-portal-kde qt6-wayland \
    && install -d -m 1777 /tmp/.X11-unix \
    && test "$(id -u default)" = 1000 && test "$(id -g default)" = 1000 \
    && if getcap /usr/bin/kwin_wayland | grep -q .; then setcap -r /usr/bin/kwin_wayland; fi \
    && sed -i 's|^Exec=.*|Exec=/usr/lib/workstation/sunshine.real|' /usr/share/applications/dev.lizardbyte.app.Sunshine.kwin.desktop \
    && dpkg-query -W '-f=${binary:Package}\t${Version}\n' > /usr/share/workstation/gaming-packages.tsv
COPY --chmod=755 infrastructure/gaming/wayland-session.sh /usr/lib/workstation/wayland-session
COPY infrastructure/gaming/wayland-apps.json /usr/share/workstation/wayland-apps.json
ENV HOME=/home/default USER=default USER_HOME=/home/default \
    XDG_CONFIG_HOME=/home/default/.config XDG_CACHE_HOME=/home/default/.cache \
    XDG_SESSION_TYPE=wayland XDG_CURRENT_DESKTOP=KDE \
    SUNSHINE_CAPTURE=kwin SUNSHINE_ENCODER=vulkan
USER 1000:1000
WORKDIR /home/default
ENTRYPOINT ["/usr/bin/dumb-init", "--", "/usr/lib/workstation/wayland-session"]
CMD []
LABEL workstation.ai.session="kwin-wayland" \
      workstation.ai.qualification="pending-wayland-gpu-input-and-stream"
