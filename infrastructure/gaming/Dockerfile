# Build via workstationctl sunshine image-build. No host mounts or driver install.
# The base and Sunshine asset are pinned; apt dependency versions are recorded,
# not claimed to be reproducible. Promote the resulting manifest digest, not a tag.
ARG BASE_IMAGE=scratch
FROM ${BASE_IMAGE}
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
# Keep upstream session/supervisor lifecycle. This rootful entrypoint is NOT
# qualified for the project's baseline namespace; do not add privileges to run it.
LABEL workstation.ai.qualification="pending-display-input-and-security"
