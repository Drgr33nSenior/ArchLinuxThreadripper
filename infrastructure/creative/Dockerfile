# Context is produced by prepare.py; all package resolution happens in staging.
# Build with --network=none. No online resolution or installation on service boot.
FROM mcr.microsoft.com/dotnet/sdk:8.0.426-bookworm-slim@sha256:d344d0c2551d7ebab37feeaf88fa170bc5f343241a5271fdbc06abde515e9290 AS swarm
WORKDIR /SwarmUI
COPY SwarmUI/ ./
COPY nuget/ /nuget/
RUN dotnet restore src/SwarmUI.csproj --locked-mode --source /nuget --runtime linux-x64 -p:SelfContained=true \
    && dotnet build src/SwarmUI.csproj --no-restore --configuration Release --runtime linux-x64 --self-contained true -o /published -m:2

FROM docker.io/rocm/pytorch@sha256:1d3ebef7da1df35f0c103ed038521933ebaeaf6f8c8d710168d8c294efdc59d9
USER root
WORKDIR /SwarmUI
COPY --from=swarm /SwarmUI/ /SwarmUI/
COPY --from=swarm /published/ /SwarmUI/src/bin/live_release/
COPY ComfyUI/ /SwarmUI/dlbackend/ComfyUI/
COPY wheels/ /wheels/
COPY requirements.lock /requirements.lock
COPY runtime.json staged-sha256.json verify_runtime.py /usr/share/workstation-creative/
COPY launch.sh /usr/local/bin/workstation-creative
ENV PATH=/SwarmUI/dlbackend/ComfyUI/venv/bin:/usr/bin:/bin \
    LD_LIBRARY_PATH="" LD_PRELOAD="" PYTHONPATH="" \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PIP_NO_INDEX=1 PYTHONDONTWRITEBYTECODE=1 HOME=/tmp
RUN cd /wheels/runtime-debs && sha256sum --check --strict SHA256SUMS \
    && for package in ./*.deb; do dpkg-deb --extract "$package" /; done \
    && /usr/bin/python3.14 -m venv --without-pip /SwarmUI/dlbackend/ComfyUI/venv \
    && test "$(find /wheels -maxdepth 1 -name 'pip-*.whl' | wc -l)" -eq 1 \
    && PYTHONPATH="$(find /wheels -maxdepth 1 -name 'pip-*.whl')" \
       /SwarmUI/dlbackend/ComfyUI/venv/bin/python -m pip install --no-index --find-links=/wheels --require-hashes -r /requirements.lock \
    && /SwarmUI/dlbackend/ComfyUI/venv/bin/python -m pip check \
    && /SwarmUI/dlbackend/ComfyUI/venv/bin/python /usr/share/workstation-creative/verify_runtime.py /usr/share/workstation-creative/runtime.json \
    && /SwarmUI/dlbackend/ComfyUI/venv/bin/python -c 'import cv2, onnxruntime' \
    && /SwarmUI/dlbackend/ComfyUI/venv/bin/python -m pip freeze > /usr/share/workstation-creative/python-freeze.txt \
    && chmod 755 /usr/local/bin/workstation-creative \
    && mkdir -p /SwarmUI/Data /SwarmUI/Output /SwarmUI/Models /SwarmUI/src/BuiltinExtensions/ComfyUIBackend/DLNodes \
    && chown -R 1000:1000 /SwarmUI/Data /SwarmUI/Output /SwarmUI/Models
USER 1000:1000
ENTRYPOINT ["/usr/local/bin/workstation-creative"]
LABEL workstation.ai.qualification="pending-offline-build-and-r9700"
